#!/usr/bin/env python3
"""
SanMar Product Search with Warehouse Inventory Filtering
=========================================================
A local web tool that queries SanMar's PromoStandards SOAP APIs
to show product details, images, pricing, and per-warehouse inventory.

Usage:
    pip install flask requests
    python sanmar_search.py

Then open http://localhost:5000 in your browser.
"""

import json
import os
import re
import threading
import time
import requests
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, render_template_string
from concurrent.futures import ThreadPoolExecutor, as_completed
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

app = Flask(__name__)

# Path to local product catalog cache (next to this script)
CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'product_catalog.json')

# Indexing status tracking
_index_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'indexed': 0,
    'errors': 0,
    'message': '',
    'last_built': None,
}
_catalog = None  # In-memory catalog once loaded
_product_cache = {}  # In-memory cache for full product fetches {productId: {data, timestamp}}

# ─── Configuration ───────────────────────────────────────────────────────────
# Credentials from environment variables (with fallbacks for local dev)
CONFIG = {
    'username': os.environ.get('SANMAR_USERNAME', 'marczi'),
    'password': os.environ.get('SANMAR_PASSWORD', 'LUKE12@grant'),
    'customer_id': os.environ.get('SANMAR_CUSTOMER_ID', '140741'),
    'favorite_warehouses': ['Seattle'],
    'highlight_warehouses': ['Seattle', 'Reno'],
    'endpoints': {
        'product':   'https://ws.sanmar.com:8080/promostandards/ProductDataServiceBindingV2',
        'inventory':  'https://ws.sanmar.com:8080/promostandards/InventoryServiceBindingV2',
        'media':      'https://ws.sanmar.com:8080/promostandards/MediaContentServiceBinding',
        'pricing':    'https://ws.sanmar.com:8080/promostandards/PricingAndConfigurationServiceBinding',
    }
}

# ─── SOAP Envelope Templates ────────────────────────────────────────────────

def soap_get_product(product_id):
    """Build SOAP request for Product Data Service v2.0.0 getProduct"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:ns="http://www.promostandards.org/WSDL/ProductDataService/2.0.0/"
    xmlns:shar="http://www.promostandards.org/WSDL/ProductDataService/2.0.0/SharedObjects/">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:GetProductRequest>
      <shar:wsVersion>2.0.0</shar:wsVersion>
      <shar:id>{CONFIG['username']}</shar:id>
      <shar:password>{CONFIG['password']}</shar:password>
      <shar:localizationCountry>US</shar:localizationCountry>
      <shar:localizationLanguage>en</shar:localizationLanguage>
      <shar:productId>{product_id}</shar:productId>
    </ns:GetProductRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def soap_get_pricing(product_id):
    """Build SOAP request for Pricing and Configuration Service v1.0.0"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:ns="http://www.promostandards.org/WSDL/PricingAndConfiguration/1.0.0/"
    xmlns:shar="http://www.promostandards.org/WSDL/PricingAndConfiguration/1.0.0/SharedObjects/">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:GetConfigurationAndPricingRequest>
      <shar:wsVersion>1.0.0</shar:wsVersion>
      <shar:id>{CONFIG['username']}</shar:id>
      <shar:password>{CONFIG['password']}</shar:password>
      <shar:productId>{product_id}</shar:productId>
      <shar:currency>USD</shar:currency>
      <shar:fobId>1</shar:fobId>
      <shar:priceType>Net</shar:priceType>
      <shar:localizationCountry>US</shar:localizationCountry>
      <shar:localizationLanguage>en</shar:localizationLanguage>
      <shar:configurationType>Blank</shar:configurationType>
    </ns:GetConfigurationAndPricingRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def soap_get_product_sellable():
    """Build SOAP request for Product Data Service v2.0.0 getProductSellable"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:ns="http://www.promostandards.org/WSDL/ProductDataService/2.0.0/"
    xmlns:shar="http://www.promostandards.org/WSDL/ProductDataService/2.0.0/SharedObjects/">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:GetProductSellableRequest>
      <shar:wsVersion>2.0.0</shar:wsVersion>
      <shar:id>{CONFIG['username']}</shar:id>
      <shar:password>{CONFIG['password']}</shar:password>
      <shar:localizationCountry>US</shar:localizationCountry>
      <shar:localizationLanguage>en</shar:localizationLanguage>
      <shar:isSellable>true</shar:isSellable>
    </ns:GetProductSellableRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def soap_get_inventory(product_id):
    """Build SOAP request for Inventory Service v2.0.0 getInventoryLevels"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:ns="http://www.promostandards.org/WSDL/Inventory/2.0.0/"
    xmlns:shar="http://www.promostandards.org/WSDL/Inventory/2.0.0/SharedObjects/">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:GetInventoryLevelsRequest>
      <shar:wsVersion>2.0.0</shar:wsVersion>
      <shar:id>{CONFIG['username']}</shar:id>
      <shar:password>{CONFIG['password']}</shar:password>
      <shar:productId>{product_id}</shar:productId>
    </ns:GetInventoryLevelsRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def soap_get_media(product_id):
    """Build SOAP request for Media Content Service v1.1.0 getMediaContent"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:ns="http://www.promostandards.org/WSDL/MediaService/1.0.0/"
    xmlns:shar="http://www.promostandards.org/WSDL/MediaService/1.0.0/SharedObjects/">
  <soapenv:Header/>
  <soapenv:Body>
    <ns:GetMediaContentRequest>
      <shar:wsVersion>1.1.0</shar:wsVersion>
      <shar:id>{CONFIG['username']}</shar:id>
      <shar:password>{CONFIG['password']}</shar:password>
      <shar:cultureName>en-us</shar:cultureName>
      <shar:productId>{product_id}</shar:productId>
      <shar:mediaType>Image</shar:mediaType>
    </ns:GetMediaContentRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


# ─── SOAP Call Helpers ───────────────────────────────────────────────────────

def soap_call(endpoint, body, soap_action=''):
    """Make a SOAP call and return the parsed XML root, or None on error."""
    headers = {
        'Content-Type': 'text/xml; charset=utf-8',
        'SOAPAction': soap_action,
    }
    try:
        resp = requests.post(endpoint, data=body, headers=headers, timeout=30, verify=True)
        resp.raise_for_status()
        return ET.fromstring(resp.text)
    except requests.exceptions.RequestException as e:
        print(f"[SOAP ERROR] {endpoint}: {e}")
        return None
    except ET.ParseError as e:
        print(f"[XML PARSE ERROR] {endpoint}: {e}")
        return None


# ─── Response Parsers ────────────────────────────────────────────────────────

def find_text(elem, paths):
    """Try multiple tag name patterns to find text in XML (namespace-agnostic)."""
    if elem is None:
        return ''
    for path in paths:
        for child in elem.iter():
            local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
            if local == path:
                return (child.text or '').strip()
    return ''


def find_all_local(elem, local_name):
    """Find all descendant elements matching a local tag name (ignoring namespace)."""
    results = []
    if elem is None:
        return results
    for child in elem.iter():
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag == local_name:
            results.append(child)
    return results


def find_first_local(elem, local_name):
    """Find first descendant matching local tag name."""
    matches = find_all_local(elem, local_name)
    return matches[0] if matches else None


def parse_product_response(root):
    """Parse getProduct SOAP response into a clean dict."""
    if root is None:
        return None

    product = {}

    prod_elem = find_first_local(root, 'Product')
    if prod_elem is None:
        err = find_text(root, ['ErrorMessage', 'errorMessage', 'faultstring'])
        return {'error': err or 'Product not found'}

    product['productId'] = find_text(prod_elem, ['productId'])
    product['productName'] = find_text(prod_elem, ['productName'])
    product['description'] = find_text(prod_elem, ['description'])
    product['productBrand'] = find_text(prod_elem, ['productBrand'])

    # Parse categories
    categories = []
    for cat in find_all_local(prod_elem, 'ProductCategory'):
        cat_name = find_text(cat, ['category'])
        if cat_name:
            categories.append(cat_name)
    product['categories'] = categories

    # Parse parts (colors, sizes, pricing)
    parts = []
    for part_elem in find_all_local(prod_elem, 'ProductPart'):
        part = {}
        part['partId'] = find_text(part_elem, ['partId'])
        part['partDescription'] = find_text(part_elem, ['description', 'partDescription'])

        colors = []
        for color_elem in find_all_local(part_elem, 'Color'):
            color_name = find_text(color_elem, ['colorName'])
            color_code = find_text(color_elem, ['colorCode'])
            if color_name:
                colors.append({'name': color_name, 'code': color_code})
        part['colors'] = colors

        sizes = []
        for size_elem in find_all_local(part_elem, 'Size'):
            size_code = find_text(size_elem, ['sizeCode'])
            if size_code:
                sizes.append(size_code)
        part['sizes'] = sizes

        prices = []
        for price_elem in find_all_local(part_elem, 'ProductPrice'):
            price = {}
            price['price'] = find_text(price_elem, ['price'])
            price['minQty'] = find_text(price_elem, ['minQuantity'])
            price['priceType'] = find_text(price_elem, ['priceType'])
            price['description'] = find_text(price_elem, ['description'])
            if price['price']:
                prices.append(price)
        part['prices'] = prices

        if part['partId']:
            parts.append(part)

    product['parts'] = parts

    all_colors = {}
    all_sizes = set()
    for p in parts:
        for c in p.get('colors', []):
            all_colors[c['name']] = c.get('code', '')
        for s in p.get('sizes', []):
            all_sizes.add(s)

    product['allColors'] = [{'name': k, 'code': v} for k, v in all_colors.items()]
    product['allSizes'] = sorted(list(all_sizes), key=size_sort_key)

    for p in parts:
        if p.get('prices'):
            product['basePrice'] = p['prices'][0].get('price', '')
            product['priceTiers'] = p['prices']
            break

    return product


def size_sort_key(size):
    """Sort sizes in logical order: XS, S, M, L, XL, 2XL, etc."""
    order = {
        'XS': 1, 'S': 2, 'M': 3, 'L': 4, 'XL': 5,
        '2XL': 6, '2X': 6, 'XXL': 6,
        '3XL': 7, '3X': 7, 'XXXL': 7,
        '4XL': 8, '4X': 8,
        '5XL': 9, '5X': 9,
        '6XL': 10, '6X': 10,
        'YXS': -5, 'YS': -4, 'YM': -3, 'YL': -2, 'YXL': -1,
    }
    return order.get(size.upper(), 50)


def parse_inventory_response(root):
    """Parse getInventoryLevels SOAP response into structured inventory data."""
    if root is None:
        return None

    inventory = {'productId': '', 'parts': [], 'warehouses': set()}

    inv_elem = find_first_local(root, 'Inventory')
    if inv_elem is None:
        err = find_text(root, ['ErrorMessage', 'errorMessage', 'faultstring'])
        return {'error': err or 'Inventory data not found'}

    inventory['productId'] = find_text(inv_elem, ['productId'])

    for part_inv in find_all_local(inv_elem, 'PartInventory'):
        part = {}
        part['partId'] = find_text(part_inv, ['partId'])
        part['partDescription'] = find_text(part_inv, ['partDescription'])
        part['partColor'] = find_text(part_inv, ['partColor'])
        part['labelSize'] = find_text(part_inv, ['labelSize'])

        qty_elem = find_first_local(part_inv, 'quantityAvailable')
        if qty_elem is not None:
            part['totalQty'] = find_text(qty_elem, ['value'])
        else:
            part['totalQty'] = '0'

        locations = []
        for loc in find_all_local(part_inv, 'InventoryLocation'):
            location = {}
            location['id'] = find_text(loc, ['inventoryLocationId'])
            location['name'] = find_text(loc, ['inventoryLocationName'])
            location['postalCode'] = find_text(loc, ['postalCode'])

            loc_qty = find_first_local(loc, 'inventoryLocationQuantity')
            if loc_qty is not None:
                location['qty'] = find_text(loc_qty, ['value'])
            else:
                location['qty'] = find_text(loc, ['value', 'quantity'])

            if location['name']:
                locations.append(location)
                inventory['warehouses'].add(location['name'])

        part['locations'] = locations

        if part['partId']:
            inventory['parts'].append(part)

    inventory['warehouses'] = sorted(list(inventory['warehouses']))
    return inventory


def parse_media_response(root):
    """Parse getMediaContent SOAP response to extract image URLs."""
    if root is None:
        return []

    images = []
    for content in find_all_local(root, 'MediaContent'):
        media = {}
        media['url'] = find_text(content, ['url', 'Url', 'fileUrl'])
        media['mediaType'] = find_text(content, ['mediaType'])
        media['classType'] = find_text(content, ['classType', 'ClassType', 'classTypeId'])
        media['fileSize'] = find_text(content, ['fileSize'])
        media['width'] = find_text(content, ['width'])
        media['height'] = find_text(content, ['height'])
        media['color'] = find_text(content, ['color', 'colorName'])
        media['partId'] = find_text(content, ['partId'])

        if media['url']:
            images.append(media)

    images.sort(key=lambda x: (
        -int(x.get('width') or '0'),
        x.get('classType', '') != 'Front'
    ))

    return images


def parse_pricing_response(root):
    """Parse getConfigurationAndPricing SOAP response to extract pricing."""
    if root is None:
        return {}

    result = {'basePrice': '', 'priceTiers': []}

    # Find PartPrice or Price elements within Configuration/Part
    prices_found = []
    for part in find_all_local(root, 'Part'):
        for pp in find_all_local(part, 'PartPrice'):
            price_val = find_text(pp, ['price', 'Price'])
            min_qty = find_text(pp, ['minQuantity', 'MinQuantity', 'minQty'])
            price_uom = find_text(pp, ['priceUom', 'PriceUom'])
            desc = find_text(pp, ['description', 'Description'])
            if price_val:
                prices_found.append({
                    'price': price_val,
                    'minQty': min_qty or '1',
                    'priceUom': price_uom or 'Each',
                    'description': desc or '',
                })

    # Deduplicate by minQty (keep first occurrence at each tier)
    seen_qtys = set()
    unique_tiers = []
    for p in prices_found:
        key = p.get('minQty', '1')
        if key not in seen_qtys:
            seen_qtys.add(key)
            unique_tiers.append(p)

    if unique_tiers:
        unique_tiers.sort(key=lambda x: int(x.get('minQty', '1') or '1'))
        result['basePrice'] = unique_tiers[0].get('price', '')
        result['priceTiers'] = unique_tiers

    return result


def parse_sellable_response(root):
    """Parse getProductSellable response to get list of sellable product IDs."""
    if root is None:
        return []

    product_ids = []
    for prod in find_all_local(root, 'ProductSellable'):
        pid = find_text(prod, ['productId'])
        if pid:
            product_ids.append(pid)

    if not product_ids:
        for prod in find_all_local(root, 'productId'):
            if prod.text and prod.text.strip():
                product_ids.append(prod.text.strip())

    return sorted(set(product_ids))


# ─── Product Catalog Cache ──────────────────────────────────────────────────

_sellable_cache = None

def get_sellable_products():
    """Fetch and cache all sellable product IDs from SanMar."""
    global _sellable_cache
    if _sellable_cache is not None:
        return _sellable_cache

    body = soap_get_product_sellable()
    root = soap_call(CONFIG['endpoints']['product'], body, 'getProductSellable')
    _sellable_cache = parse_sellable_response(root)
    return _sellable_cache


# ─── Full Product Catalog (Local Index) ─────────────────────────────────────

def load_catalog():
    """Load product catalog from disk if it exists."""
    global _catalog
    if _catalog is not None:
        return _catalog
    if os.path.exists(CATALOG_PATH):
        try:
            with open(CATALOG_PATH, 'r') as f:
                data = json.load(f)
            _catalog = data.get('products', [])
            _index_status['last_built'] = data.get('built_at', 'Unknown')
            _index_status['message'] = f'Catalog loaded: {len(_catalog)} products'
            print(f"[CATALOG] Loaded {len(_catalog)} products from {CATALOG_PATH}")
            return _catalog
        except Exception as e:
            print(f"[CATALOG] Error loading: {e}")
    return None


def search_catalog(query):
    """Search the local product catalog by keyword."""
    catalog = load_catalog()
    if not catalog:
        return None

    query_lower = query.lower()
    terms = query_lower.split()

    results = []
    for prod in catalog:
        # Build searchable text from all product fields
        searchable = ' '.join([
            prod.get('productId', ''),
            prod.get('productName', ''),
            prod.get('description', ''),
            prod.get('productBrand', ''),
            ' '.join(prod.get('categories', [])),
            ' '.join(prod.get('colorNames', [])),
        ]).lower()

        # All search terms must appear somewhere in the product text
        if all(term in searchable for term in terms):
            results.append(prod)

    return results


def build_catalog_background():
    """Background task: fetch all sellable products and build local catalog."""
    global _catalog

    _index_status['running'] = True
    _index_status['progress'] = 0
    _index_status['indexed'] = 0
    _index_status['errors'] = 0
    _index_status['message'] = 'Fetching sellable product list...'

    try:
        product_ids = get_sellable_products()
    except Exception as e:
        _index_status['running'] = False
        _index_status['message'] = f'Failed to get product list: {e}'
        return

    total = len(product_ids)
    _index_status['total'] = total
    _index_status['message'] = f'Found {total} products. Fetching details...'

    catalog = []
    batch_size = 15  # concurrent requests at a time

    for i in range(0, total, batch_size):
        batch = product_ids[i:i+batch_size]
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            futures = {executor.submit(fetch_product_basic, pid): pid for pid in batch}
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    result = future.result()
                    if result and not result.get('error'):
                        catalog.append(result)
                        _index_status['indexed'] += 1
                    else:
                        _index_status['errors'] += 1
                except Exception:
                    _index_status['errors'] += 1

        _index_status['progress'] = min(i + batch_size, total)
        _index_status['message'] = f'Indexed {_index_status["indexed"]} of {total} products...'

    # Save to disk
    built_at = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(CATALOG_PATH, 'w') as f:
            json.dump({'products': catalog, 'built_at': built_at}, f)
        _index_status['message'] = f'Done! {len(catalog)} products indexed.'
        _index_status['last_built'] = built_at
    except Exception as e:
        _index_status['message'] = f'Index built but failed to save: {e}'

    _catalog = catalog
    _index_status['running'] = False
    _index_status['progress'] = total
    print(f"[CATALOG] Built catalog with {len(catalog)} products")


def fetch_product_basic(product_id):
    """Fetch just the essential product info for the catalog index."""
    body = soap_get_product(product_id)
    root = soap_call(CONFIG['endpoints']['product'], body, 'getProduct')
    if root is None:
        return None

    prod_elem = find_first_local(root, 'Product')
    if prod_elem is None:
        return None

    product = {
        'productId': find_text(prod_elem, ['productId']),
        'productName': find_text(prod_elem, ['productName']),
        'description': find_text(prod_elem, ['description']),
        'productBrand': find_text(prod_elem, ['productBrand']),
    }

    # Categories
    categories = []
    for cat in find_all_local(prod_elem, 'ProductCategory'):
        cat_name = find_text(cat, ['category'])
        if cat_name:
            categories.append(cat_name)
    product['categories'] = categories

    # Collect unique color names
    color_names = set()
    for color_elem in find_all_local(prod_elem, 'Color'):
        cn = find_text(color_elem, ['colorName'])
        if cn:
            color_names.add(cn)
    product['colorNames'] = sorted(color_names)

    # Get base price: prefer pricing service (wholesale/net), fall back to product XML (MSRP)
    base_price = ''
    try:
        pricing = fetch_pricing(product_id)
        if pricing and pricing.get('basePrice'):
            base_price = pricing['basePrice']
    except Exception:
        pass
    if not base_price:
        for price_elem in find_all_local(prod_elem, 'ProductPrice'):
            p = find_text(price_elem, ['price'])
            if p:
                base_price = p
                break
    product['basePrice'] = base_price

    # Infer gender from product name/description/ID
    product['gender'] = infer_gender(product)

    return product


def infer_gender(product):
    """Infer gender from product name, description, and style ID."""
    text = ' '.join([
        product.get('productName', ''),
        product.get('description', ''),
        product.get('productId', ''),
    ]).lower()

    # Ladies/Women indicators
    women_terms = ['ladies', 'women', 'lady', "women's", 'womens', 'female']
    # Style prefixes that are typically ladies
    ladies_prefixes = ['l', 'lk', 'lpc', 'lst', 'lw', 'log1']

    # Youth indicators
    youth_terms = ['youth', 'kids', 'child', 'boy', 'girl', 'toddler']
    youth_prefixes = ['y', 'yst', 'ypc']

    pid = product.get('productId', '').upper()

    for term in youth_terms:
        if term in text:
            return 'Youth'

    for prefix in youth_prefixes:
        if pid.startswith(prefix.upper()):
            return 'Youth'

    for term in women_terms:
        if term in text:
            return 'Women'

    for prefix in ladies_prefixes:
        if pid.startswith(prefix.upper()) and len(pid) > len(prefix):
            # Make sure it's actually a ladies prefix, not just starting with L
            next_char = pid[len(prefix)] if len(pid) > len(prefix) else ''
            if next_char.isdigit() or next_char in ('K', 'S', 'P', 'W', 'O'):
                return 'Women'

    # If we see 'men' but not 'women', it's men's
    if 'men' in text and 'women' not in text:
        return 'Men'

    # Check for tall (often men's)
    if 'tall' in text or pid.startswith('TL'):
        return 'Men'

    return 'Unisex'


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=CONFIG)


# ─── Product Category / Keyword Mapping ──────────────────────────────────────
# Maps common search terms to Sanmar style numbers for natural language search.
KEYWORD_STYLES = {
    # ── Category searches (multi-brand) ──
    'polo': [
        'K500', 'K420', 'K540', 'K540P', 'K572', 'K110', 'K100', 'K398',   # Port Authority
        'CS410', 'CS412', 'CS418',                                            # CornerStone
        'TLK500', 'K500P', 'K500LS',                                          # Port Authority Tall/Pocket
        'L500', 'L540', 'LK110', 'LK500',                                    # Port Authority Ladies
        'NKAH6260', 'NKDC1963', 'NKDX6684', 'NKFB6444',                     # Nike
        '1376844', '1376904',                                                  # Under Armour
        'ST640', 'ST650', 'ST680', 'LST650',                                  # Sport-Tek
        'OG101', 'OG105', 'LOG101',                                           # OGIO
        'EB102', 'EB100',                                                      # Eddie Bauer
        'TM1MU410', 'TM1MU412',                                               # TravisMathew
        'MM1000', 'MM1001',                                                    # Mercer+Mettle
        'BB18200', 'BB18208',                                                  # Brooks Brothers
        'G948', 'G880', '8800',                                               # Gildan/DryBlend
        'NF0A47FD',                                                            # The North Face
        'CT102537',                                                            # Carhartt
    ],
    'tee': [
        'PC54', 'PC61', 'PC55', 'PC380', 'PC450',                            # Port & Company
        'DT6000', 'DT104', 'DT1350', 'DT6500',                               # District
        'NL6210', 'NL3600', 'NL6010',                                         # Next Level
        'BC3001', 'BC3413', 'BC3501',                                          # Bella+Canvas
        'G500', 'G200', 'G800', 'G640',                                       # Gildan
        '5000', '2000', '5170',                                               # Gildan/Hanes numeric
        'C9018', '1717', '1566',                                               # Comfort Colors
        'AA1070',                                                              # Alternative Apparel
        '29M', '29MP',                                                         # Jerzees
        'NKBQ5231', 'NKBQ5233',                                               # Nike
        'ST350', 'YST350',                                                     # Sport-Tek
        'CTK87',                                                               # Carhartt
        'DM130',                                                               # District Made
    ],
    't-shirt': [
        'PC54', 'PC61', 'DT6000', 'DT104', 'NL6210', 'NL3600', 'BC3001',
        'G500', 'G200', '5000', '2000', '1717', 'C9018', 'ST350', 'CTK87',
        'NKBQ5231', 'AA1070', 'DM130',
    ],
    'tshirt': [
        'PC54', 'PC61', 'DT6000', 'NL6210', 'NL3600', 'BC3001', 'G500',
        '5000', '1717', 'ST350', 'CTK87',
    ],
    'fleece': [
        'F217', 'F218', 'F232', 'F233', 'F904',                              # Port Authority
        'ST850', 'ST240', 'ST241', 'ST253', 'ST283',                          # Sport-Tek
        'J317', 'JP56', 'JP54',                                               # Port Authority
        'L217', 'L232',                                                        # Port Authority Ladies
        'NF0A3LH5', 'NF0A47FC',                                               # The North Face
        'EB224', 'EB226',                                                      # Eddie Bauer
        'G998',                                                                # Gildan
        '993M',                                                                # Jerzees
    ],
    'jacket': [
        'J317', 'J328', 'J331', 'J332', 'J754', 'J790',                      # Port Authority
        'J318', 'J900', 'J764',                                               # Port Authority
        'TLJ317',                                                              # Port Authority Tall
        'L317', 'L790',                                                        # Port Authority Ladies
        'NF0A3LH5', 'NF0A47FI', 'NF0A3LGX', 'NF0A7V6K',                     # The North Face
        'EB530', 'EB534', 'EB550', 'EB554',                                   # Eddie Bauer
        'NKAA1854', 'NKAA1855', 'NKFB6447',                                  # Nike
        'CT102208', 'CT104616',                                                # Carhartt
        'OG727', 'LOG726',                                                     # OGIO
        'JST72', 'LST76',                                                      # Sport-Tek
        '1376844',                                                             # Under Armour
    ],
    'hoodie': [
        'PC78H', 'PC90H', 'PC850H', 'LPC78ZH',                               # Port & Company
        'DT6100', 'DT810', 'DT1300',                                          # District
        'ST254', 'ST272',                                                      # Sport-Tek
        'F244',                                                                # Port Authority
        'G185', '18500',                                                       # Gildan
        '996M',                                                                # Jerzees
        'BC3719',                                                              # Bella+Canvas
        'NL9300',                                                              # Next Level
        'CTK121', 'CTK128',                                                    # Carhartt
        'NKDR1499', 'NKDR1513',                                               # Nike
        '1370379',                                                             # Under Armour
    ],
    'sweatshirt': [
        'PC78', 'PC90', 'PC78H', 'PC90H', 'PC850',                           # Port & Company
        'DT6100', 'DT810',                                                     # District
        'F260', 'P160',                                                        # Hanes
        'ST254',                                                               # Sport-Tek
        'G180', '18000', '18500',                                              # Gildan
        '562M',                                                                # Jerzees
        'BC3901', 'BC3945',                                                    # Bella+Canvas
        '1566',                                                                # Comfort Colors
        'CTK126',                                                              # Carhartt
    ],
    'hat': [
        'C112', 'C130', 'C914', 'C922', 'C112P', 'C868', 'C867',            # Port Authority
        'NE1000', 'NE200', 'NE1020', 'NE1091', 'NE400', 'NE1122',           # New Era
        'STC26', 'STC17', 'STC10',                                            # Sport-Tek
        'CP80', 'CP86', 'CP89',                                               # Port & Company
        'CT103056',                                                            # Carhartt
        'NKFB5677', 'NKAA1859',                                               # Nike
        'OG600', 'OG601',                                                      # OGIO
        'RH112',                                                               # Richardson
    ],
    'cap': [
        'C112', 'C130', 'C914', 'C922', 'C112P', 'C868',
        'NE1000', 'NE200', 'NE1020', 'NE400',
        'STC26', 'STC17', 'CP80', 'CP86',
        'CT103056', 'NKFB5677', 'OG600', 'RH112',
    ],
    'beanie': ['CP91', 'CP91L', 'CP95', 'C939', 'STC31', 'NE907', 'CP90', 'CT104597', 'NE905'],
    'quarter zip': [
        'F218', 'K805', 'K584',                                               # Port Authority
        'ST850', 'ST860', 'ST357',                                             # Sport-Tek
        'EB226', 'EB224',                                                      # Eddie Bauer
        'NF0A47FC',                                                            # The North Face
        'NKDX6702', 'NKAH6254',                                               # Nike
        '1376844',                                                             # Under Armour
        'OG152', 'LOG132',                                                     # OGIO
    ],
    'tank': ['PC54TT', 'DT5301', 'DT6301', 'NL3633', 'G520', '2200', 'AA6006', 'LPC54TT', 'BC3480', 'BC6003'],
    'long sleeve': [
        'PC54LS', 'PC61LS',                                                    # Port & Company
        'DT6200',                                                              # District
        'K500LS', 'K540LS',                                                    # Port Authority
        'G540', 'G240', '5400',                                               # Gildan/Hanes
        'ST657', 'ST350LS',                                                    # Sport-Tek
        'L500LS',                                                              # Port Authority Ladies
        'BC3501',                                                              # Bella+Canvas
        'NL3601',                                                              # Next Level
        'CTK126',                                                              # Carhartt
    ],
    'dress shirt': ['S608', 'S638', 'W100', 'W808', 'TLS608', 'RH370', 'RH240', 'LS508', 'LW100', 'BB18000', 'BB18006'],
    'button down': ['S608', 'S638', 'W100', 'W808', 'TLS608', 'LS508', 'LW100', 'BB18000', 'BB18006'],
    'vest': [
        'J325', 'J709', 'J851', 'F219', 'F226',                              # Port Authority
        'L325', 'L709',                                                        # Port Authority Ladies
        'NF0A3LH1',                                                            # The North Face
        'EB650',                                                               # Eddie Bauer
        'NKAA1856',                                                            # Nike
        'CT102286',                                                            # Carhartt
    ],
    'shorts': ['ST304', 'ST312', 'ST355', 'PC54YS', 'ST515', 'LST304', 'NKBV6855'],
    'pants': ['PT88', 'PT38', 'PC78P', 'J307', 'CTB290', 'NKBV6876'],
    'bag': ['BG400', 'BG100', 'BG401', 'BG406', 'BG407', 'C815', 'C817', 'BG410', 'OG032', 'NKDC4068'],
    'tote': ['BG400', 'BG401', 'BG406', 'B5000', 'BG407', 'BG410'],
    'backpack': ['BG204', 'BG205', 'BG208', 'BG210', 'CSB100', 'OG032', 'NKDC4068'],
    'towel': ['PT42', 'PT43', 'PT44', 'TW540'],
    'blanket': ['BP20', 'BP25', 'BP30', 'BP31', 'TB850'],
    'apron': ['A700', 'A701', 'A704', 'A706'],
    'youth': ['PC54Y', 'PC61Y', 'Y500', 'YST350', 'DT6000Y', 'YST640'],
    'women': ['L500', 'L540', 'LK110', 'LK500', 'LPC54', 'LPC380', 'LST850', 'L217', 'L317', 'LOG101', 'LST650'],
    'ladies': ['L500', 'L540', 'LK110', 'LK500', 'LPC54', 'LPC380', 'LST850', 'L217', 'L317', 'LOG101', 'LST650'],
    'tall': ['TLK500', 'TLJ317', 'TLS608', 'TLPC54', 'TLK540'],
    'performance': ['ST350', 'ST650', 'ST850', 'K540', 'K572', 'K110', 'LST350', 'LST850', 'NKAH6260', '1376844'],
    'dri fit': ['ST350', 'ST650', 'ST850', 'K540', 'K572', 'LST350', 'NKAH6260', 'NKDC1963'],
    'moisture wicking': ['ST350', 'ST650', 'K540', 'K572', 'K110', 'LST350', 'NKAH6260'],
    'cotton': ['PC54', 'PC61', 'G500', 'G200', '5000', '2000', 'G800', 'BC3001', 'NL3600', '1717', 'CTK87'],
    'polyester': ['ST350', 'ST650', 'ST850', 'K540', 'K572', 'NKAH6260'],
    'tri blend': ['DT6000', 'DM130', 'DM1350', 'AA1070', 'BC3413', 'NL6010'],
    # ── Brand searches ──
    'north face': ['NF0A3LH5', 'NF0A3LH1', 'NF0A47FI', 'NF0A47FC', 'NF0A3LGX', 'NF0A7V6K', 'NF0A47FD'],
    'nike': ['NKAH6260', 'NKAH6254', 'NKDC1963', 'NKDC2103', 'NKFB6444', 'NKFB6447', 'NKAA1854', 'NKAA1855', 'NKDX6684', 'NKFN9418', 'NKBQ5231', 'NKDR1499', 'NKFB5677', 'NKDX6702', 'NKBV6855'],
    'eddie bauer': ['EB530', 'EB534', 'EB226', 'EB200', 'EB650', 'EB550', 'EB554', 'EB224', 'EB100', 'EB102'],
    'port authority': ['K500', 'K540', 'K110', 'J317', 'J331', 'S608', 'L500', 'L540', 'LK110', 'F218', 'C112', 'K805'],
    'port company': ['PC54', 'PC61', 'PC78H', 'PC90H', 'PC380', 'LPC54', 'LPC78ZH', 'PC54LS', 'PC850', 'CP80'],
    'sport tek': ['ST350', 'ST650', 'ST850', 'ST240', 'ST860', 'LST350', 'LST850', 'ST640', 'ST680', 'STC26'],
    'district': ['DT6000', 'DT104', 'DT1350', 'DT6100', 'DT810', 'DT6500', 'DT6200', 'DM130', 'DT6301'],
    'gildan': ['G500', 'G200', 'G800', 'G180', 'G185', 'G240', 'G520', 'G540', '5000', '2000', '18000', '18500', 'G640', 'G880', 'G998', '8800'],
    'hanes': ['5170', '5250', '5280', '5286', '5290', 'P160', 'P170', 'P180', 'F260', '5186'],
    'comfort colors': ['1717', '6030', 'C9018', '1566', '6014'],
    'bella canvas': ['BC3001', 'BC3501', 'BC6400', 'BC6405', 'BC3480', 'BC3413', 'BC6415', 'BC3719', 'BC3901', 'BC6003'],
    'next level': ['NL3600', 'NL6210', 'NL3633', 'NL6710', 'NL9000', 'NL6051', 'NL6010', 'NL3601', 'NL9300'],
    'carhartt': ['CT100393', 'CT102208', 'CTK87', 'CTK126', 'CTK128', 'CT104616', 'CT105294', 'CT102537', 'CT103056', 'CTB290', 'CT104597', 'CT102286', 'CTK121'],
    'ogio': ['OG101', 'OG105', 'OG126', 'OG116', 'LOG101', 'LOG126', 'OG600', 'OG601', 'OG727', 'LOG726', 'OG152'],
    'under armour': ['1376844', '1376847', '1376842', '1376904', '1376907', '1370379'],
    'new era': ['NE1000', 'NE200', 'NE1020', 'NE1091', 'NE400', 'NE1122', 'NE907', 'NE905'],
    'travis mathew': ['TM1MU410', 'TM1MU412'],
    'travismathew': ['TM1MU410', 'TM1MU412'],
    'brooks brothers': ['BB18200', 'BB18208', 'BB18000', 'BB18006'],
    'mercer mettle': ['MM1000', 'MM1001'],
    'jerzees': ['29M', '29MP', '996M', '562M', '993M'],
    'richardson': ['RH112', 'RH370', 'RH240'],
    'cornerstone': ['CS410', 'CS412', 'CS418'],
    'alternative': ['AA1070', 'AA6006'],
}

# Common color terms for parsing natural language queries
COLOR_TERMS = [
    'red', 'blue', 'navy', 'black', 'white', 'green', 'grey', 'gray',
    'charcoal', 'royal', 'orange', 'yellow', 'purple', 'pink', 'brown',
    'tan', 'khaki', 'maroon', 'burgundy', 'teal', 'coral', 'heather',
    'forest', 'lime', 'gold', 'silver', 'cream', 'ivory', 'stone',
    'smoke', 'steel', 'graphite', 'iron', 'sand', 'deep', 'bright',
    'light', 'dark', 'neon', 'camo', 'olive', 'cardinal', 'scarlet',
    'cyan', 'aqua', 'magenta', 'wine', 'plum', 'mint', 'sage',
]


def parse_natural_query(query):
    """Parse a natural language query into category keywords and color terms."""
    words = query.lower().split()
    colors_found = []
    category_words = []

    # Pull out color terms
    i = 0
    while i < len(words):
        # Check two-word color combos like "light blue", "deep red"
        if i + 1 < len(words) and f"{words[i]} {words[i+1]}" in COLOR_TERMS:
            colors_found.append(f"{words[i]} {words[i+1]}")
            i += 2
            continue
        if words[i] in COLOR_TERMS:
            colors_found.append(words[i])
        else:
            category_words.append(words[i])
        i += 1

    return category_words, colors_found


@app.route('/api/search')
def api_search():
    """Search for products by style number, keyword, or natural language."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Please enter a search term'}), 400

    query_upper = query.upper()

    # 1) Check if it looks like an exact style number
    style_pattern = re.match(r'^[A-Z0-9][A-Z0-9\-\.]*$', query_upper)
    if style_pattern:
        result = fetch_product_full(query_upper)
        if result and not result.get('error'):
            return jsonify({'products': [result], 'searchType': 'style'})

    # 2) Search the local catalog if it exists (has ALL products)
    catalog_results = search_catalog(query)
    if catalog_results is not None and len(catalog_results) > 0:
        _, colors_found = parse_natural_query(query)
        # Always return lightweight browse cards — fast response, user clicks for full details
        return jsonify({
            'products': [{'productId': r['productId'], 'productName': r.get('productName', ''),
                          'productBrand': r.get('productBrand', ''), 'description': r.get('description', '')[:100],
                          'basePrice': r.get('basePrice', ''), 'gender': r.get('gender', 'Unisex'),
                          'colorNames': r.get('colorNames', []),
                          'categories': r.get('categories', [])}
                         for r in catalog_results[:500]],
            'totalMatches': len(catalog_results),
            'searchType': 'catalog_browse',
            'autoFilterColors': colors_found,
            'message': f'Found {len(catalog_results)} products for "{query}". Click any product for full details.'
        })

    # 3) No catalog — fall back to keyword mapping
    category_words, colors_found = parse_natural_query(query)

    matched_styles = set()
    for word in category_words:
        if word in KEYWORD_STYLES:
            matched_styles.update(KEYWORD_STYLES[word])
        for other in category_words:
            combo = f"{word} {other}"
            if combo in KEYWORD_STYLES:
                matched_styles.update(KEYWORD_STYLES[combo])

    query_lower = query.lower().strip()
    for keyword, styles in KEYWORD_STYLES.items():
        if keyword in query_lower:
            matched_styles.update(styles)

    if matched_styles:
        style_list = sorted(matched_styles)
        # Return lightweight browse cards for keyword matches too
        products = [{'productId': sid, 'productName': '', 'productBrand': '',
                     'description': '', 'basePrice': '', 'gender': 'Unisex',
                     'colorNames': [], 'categories': []} for sid in style_list]
        return jsonify({
            'products': products,
            'searchType': 'catalog_browse',
            'autoFilterColors': colors_found,
            'message': f'Found {len(products)} products for "{query}" (build catalog for richer results)'
        })

    # 4) Fall back to searching sellable product IDs
    return search_by_keyword(query_upper)


def search_by_keyword(keyword):
    """Search sellable products by keyword matching against product IDs."""
    try:
        all_products = get_sellable_products()
    except Exception as e:
        return jsonify({'error': f'Failed to load product catalog: {str(e)}'}), 500

    if not all_products:
        return jsonify({'error': 'Product catalog is empty or unavailable'}), 500

    keyword_upper = keyword.upper()
    matches = [pid for pid in all_products if keyword_upper in pid.upper()]
    matches = matches[:50]

    return jsonify({
        'products': [{'productId': pid} for pid in matches],
        'totalMatches': len(matches),
        'searchType': 'keyword',
        'message': f'Found {len(matches)} matching product IDs. Click one to load full details.'
    })


def fetch_product_full(product_id):
    """Fetch complete product data: details + inventory + images + pricing, in parallel.
    Results are cached in memory for 10 minutes to speed up repeat lookups."""
    # Check cache first (10 min TTL)
    cached = _product_cache.get(product_id)
    if cached and (time.time() - cached['timestamp']) < 600:
        return cached['data']

    results = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_product_details, product_id): 'product',
            executor.submit(fetch_inventory, product_id): 'inventory',
            executor.submit(fetch_media, product_id): 'media',
            executor.submit(fetch_pricing, product_id): 'pricing',
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as e:
                results[key] = {'error': str(e)}

    product = results.get('product', {})
    if product and not product.get('error'):
        product['inventory'] = results.get('inventory', {})
        product['images'] = results.get('media', [])
        # Merge pricing — prefer dedicated pricing service over product XML pricing
        pricing = results.get('pricing', {})
        if pricing and not pricing.get('error') and pricing.get('basePrice'):
            product['basePrice'] = pricing['basePrice']
            product['priceTiers'] = pricing.get('priceTiers', [])
        # Cache the result
        _product_cache[product_id] = {'data': product, 'timestamp': time.time()}
        # Keep cache from growing unbounded (max 500 products)
        if len(_product_cache) > 500:
            oldest = min(_product_cache, key=lambda k: _product_cache[k]['timestamp'])
            del _product_cache[oldest]
    return product


def fetch_product_details(product_id):
    """Fetch product details from Product Data Service."""
    body = soap_get_product(product_id)
    root = soap_call(CONFIG['endpoints']['product'], body, 'getProduct')
    return parse_product_response(root)


def fetch_inventory(product_id):
    """Fetch inventory levels from Inventory Service."""
    body = soap_get_inventory(product_id)
    root = soap_call(CONFIG['endpoints']['inventory'], body, 'getInventoryLevels')
    return parse_inventory_response(root)


def fetch_media(product_id):
    """Fetch product images from Media Content Service."""
    body = soap_get_media(product_id)
    root = soap_call(CONFIG['endpoints']['media'], body, 'getMediaContent')
    return parse_media_response(root)


def fetch_pricing(product_id):
    """Fetch pricing from Pricing and Configuration Service."""
    body = soap_get_pricing(product_id)
    root = soap_call(CONFIG['endpoints']['pricing'], body, 'getConfigurationAndPricing')
    return parse_pricing_response(root)


@app.route('/api/product/<product_id>')
def api_product(product_id):
    """Get full product details including inventory and images."""
    result = fetch_product_full(product_id.strip().upper())
    if result and not result.get('error'):
        return jsonify(result)
    else:
        return jsonify(result or {'error': 'Failed to fetch product data'}), 404


@app.route('/api/inventory/<product_id>')
def api_inventory(product_id):
    """Get inventory levels for a product."""
    result = fetch_inventory(product_id.strip().upper())
    return jsonify(result or {'error': 'Failed to fetch inventory'})


@app.route('/api/sellable')
def api_sellable():
    """Get list of all sellable product IDs (cached)."""
    try:
        products = get_sellable_products()
        return jsonify({'products': products, 'count': len(products)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/catalog/build', methods=['POST'])
def api_build_catalog():
    """Start building the product catalog in the background."""
    if _index_status['running']:
        return jsonify({'message': 'Catalog build already in progress', 'status': _index_status})
    thread = threading.Thread(target=build_catalog_background, daemon=True)
    thread.start()
    return jsonify({'message': 'Catalog build started', 'status': _index_status})


@app.route('/api/catalog/status')
def api_catalog_status():
    """Get current catalog build status."""
    catalog = load_catalog()
    return jsonify({
        **_index_status,
        'catalog_exists': catalog is not None,
        'catalog_size': len(catalog) if catalog else 0,
    })


@app.route('/api/healthcheck')
def api_healthcheck():
    """Test connectivity to all SanMar API endpoints, auto-discover correct URLs."""
    results = {}

    # URL variants to try for each service that might 404
    url_variants = {
        'media': [
            'https://ws.sanmar.com:8080/promostandards/MediaContentServiceBinding',
            'https://ws.sanmar.com:8080/promostandards/MediaContentServiceBindingV2',
            'https://ws.sanmar.com:8080/promostandards/MediaService',
            'https://ws.sanmar.com:8080/promostandards/MediaServiceBinding',
            'https://ws.sanmar.com:8080/promostandards/MediaContentService',
        ],
        'pricing': [
            'https://ws.sanmar.com:8080/promostandards/PricingAndConfigurationServiceBinding',
            'https://ws.sanmar.com:8080/promostandards/PricingAndConfigurationServiceBindingV2',
            'https://ws.sanmar.com:8080/promostandards/ProductPricingAndConfigurationServiceBinding',
            'https://ws.sanmar.com:8080/promostandards/PricingServiceBinding',
            'https://ws.sanmar.com:8080/promostandards/PricingService',
        ],
    }

    for name, url in CONFIG['endpoints'].items():
        try:
            r = requests.get(url + '?WSDL', timeout=10)
            if r.status_code == 200:
                results[name] = {
                    'status': 200,
                    'ok': True,
                    'message': f'Connected ({url})',
                    'wsdl_size': len(r.text),
                }
            else:
                # Try variants if available
                found = False
                if name in url_variants:
                    for variant_url in url_variants[name]:
                        if variant_url == url:
                            continue
                        try:
                            rv = requests.get(variant_url + '?WSDL', timeout=10)
                            if rv.status_code == 200:
                                CONFIG['endpoints'][name] = variant_url
                                results[name] = {
                                    'status': 200,
                                    'ok': True,
                                    'message': f'Found at {variant_url}',
                                }
                                found = True
                                break
                        except:
                            continue
                if not found:
                    results[name] = {
                        'status': r.status_code,
                        'ok': False,
                        'message': f'Not found ({r.status_code}) — tried {len(url_variants.get(name, []))+1} URL variants',
                    }
        except requests.exceptions.SSLError as e:
            results[name] = {'ok': False, 'message': f'SSL Error: {str(e)[:100]}'}
        except requests.exceptions.ConnectionError as e:
            results[name] = {'ok': False, 'message': f'Connection failed: {str(e)[:100]}'}
        except Exception as e:
            results[name] = {'ok': False, 'message': str(e)[:100]}

    # Quick auth test
    try:
        body = soap_get_product('PC54')
        root = soap_call(CONFIG['endpoints']['product'], body, 'getProduct')
        if root is not None:
            fault = find_text(root, ['faultstring', 'Fault', 'ErrorMessage'])
            if fault:
                results['auth_test'] = {'ok': False, 'message': f'Auth error: {fault}'}
            else:
                prod_name = find_text(root, ['productName'])
                results['auth_test'] = {
                    'ok': True,
                    'message': f'Authenticated OK — found: {prod_name}' if prod_name else 'Got response (check debug for details)'
                }
        else:
            results['auth_test'] = {'ok': False, 'message': 'No response from product service'}
    except Exception as e:
        results['auth_test'] = {'ok': False, 'message': str(e)[:100]}

    return jsonify(results)


@app.route('/api/debug/raw/<service>/<product_id>')
def api_debug_raw(service, product_id):
    """Debug endpoint: return raw XML response from a service."""
    product_id = product_id.strip().upper()
    builders = {
        'product': (soap_get_product, CONFIG['endpoints']['product'], 'getProduct'),
        'inventory': (soap_get_inventory, CONFIG['endpoints']['inventory'], 'getInventoryLevels'),
        'media': (soap_get_media, CONFIG['endpoints']['media'], 'getMediaContent'),
    }
    if service not in builders:
        return jsonify({'error': f'Unknown service: {service}'}), 400

    builder_fn, endpoint, action = builders[service]
    body = builder_fn(product_id)
    headers = {'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': action}

    try:
        resp = requests.post(endpoint, data=body, headers=headers, timeout=30)
        return resp.text, resp.status_code, {'Content-Type': 'text/xml'}
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── HTML Frontend ───────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SanMar Product Search — Warehouse Edition</title>
<style>
  :root {
    --primary: #1a3a5c;
    --primary-light: #2a5a8c;
    --accent: #e8930a;
    --accent-hover: #d17f00;
    --bg: #f5f7fa;
    --card-bg: #ffffff;
    --text: #333;
    --text-light: #666;
    --border: #dde1e7;
    --success: #22c55e;
    --warning: #f59e0b;
    --danger: #ef4444;
    --fav-bg: #fef3c7;
    --fav-border: #f59e0b;
    --shadow: 0 2px 8px rgba(0,0,0,0.08);
    --shadow-lg: 0 4px 20px rgba(0,0,0,0.12);
    --radius: 10px;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  .header {
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
    color: white;
    padding: 20px 0;
    box-shadow: var(--shadow-lg);
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .header-inner {
    max-width: 1400px;
    margin: 0 auto;
    padding: 0 24px;
    display: flex;
    align-items: center;
    gap: 24px;
    flex-wrap: wrap;
  }
  .logo {
    font-size: 22px;
    font-weight: 700;
    white-space: nowrap;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .logo-icon {
    width: 36px;
    height: 36px;
    background: var(--accent);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
  }
  .logo small {
    font-weight: 400;
    font-size: 12px;
    opacity: 0.8;
    display: block;
  }

  .search-box {
    flex: 1;
    min-width: 300px;
    display: flex;
    gap: 8px;
  }
  .search-box input {
    flex: 1;
    padding: 12px 18px;
    border: 2px solid rgba(255,255,255,0.3);
    border-radius: var(--radius);
    font-size: 15px;
    background: rgba(255,255,255,0.15);
    color: white;
    outline: none;
    transition: all 0.2s;
  }
  .search-box input::placeholder { color: rgba(255,255,255,0.6); }
  .search-box input:focus {
    background: rgba(255,255,255,0.25);
    border-color: var(--accent);
  }
  .search-box button {
    padding: 12px 28px;
    background: var(--accent);
    color: white;
    border: none;
    border-radius: var(--radius);
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
    white-space: nowrap;
  }
  .search-box button:hover { background: var(--accent-hover); transform: translateY(-1px); }
  .search-box button:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }

  .main {
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px;
    display: flex;
    gap: 24px;
    align-items: flex-start;
  }

  .sidebar {
    width: 280px;
    flex-shrink: 0;
    position: sticky;
    top: 100px;
  }
  .filter-card {
    background: var(--card-bg);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 20px;
    margin-bottom: 16px;
  }
  .filter-card h3 {
    font-size: 14px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-light);
    margin-bottom: 14px;
    padding-bottom: 10px;
    border-bottom: 2px solid var(--bg);
  }
  .warehouse-list { list-style: none; }
  .warehouse-list li {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.15s;
    font-size: 14px;
  }
  .warehouse-list li:hover { background: var(--bg); }
  .warehouse-list li.favorite {
    background: var(--fav-bg);
    border: 1px solid var(--fav-border);
    font-weight: 600;
  }
  .warehouse-list li.favorite .fav-star { display: inline; }
  .fav-star { display: none; color: var(--accent); font-size: 12px; }
  .warehouse-list input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--primary);
  }
  .wh-qty {
    margin-left: auto;
    font-size: 13px;
    font-weight: 600;
    color: var(--primary);
    background: var(--bg);
    padding: 2px 8px;
    border-radius: 12px;
  }

  .color-filters { display: flex; flex-wrap: wrap; gap: 6px; }
  .color-chip {
    padding: 5px 12px;
    border: 1px solid var(--border);
    border-radius: 20px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
    background: white;
  }
  .color-chip:hover, .color-chip.active {
    background: var(--primary);
    color: white;
    border-color: var(--primary);
  }

  .content { flex: 1; min-width: 0; }

  .status-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    padding: 12px 16px;
    background: var(--card-bg);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    font-size: 14px;
    color: var(--text-light);
  }

  .product-card {
    background: var(--card-bg);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    margin-bottom: 20px;
    overflow: hidden;
    transition: box-shadow 0.2s;
  }
  .product-card:hover { box-shadow: var(--shadow-lg); }

  .product-header {
    display: flex;
    gap: 24px;
    padding: 24px;
  }

  .product-image {
    width: 250px;
    height: 250px;
    flex-shrink: 0;
    background: #f8f9fa;
    border-radius: 8px;
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .product-image img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }
  .product-image .no-image {
    color: #ccc;
    font-size: 48px;
  }

  .product-info { flex: 1; min-width: 0; }
  .product-info h2 {
    font-size: 22px;
    color: var(--primary);
    margin-bottom: 4px;
  }
  .product-style {
    font-size: 14px;
    color: var(--accent);
    font-weight: 600;
    margin-bottom: 12px;
  }
  .product-desc {
    font-size: 14px;
    color: var(--text-light);
    line-height: 1.5;
    margin-bottom: 16px;
    max-height: 80px;
    overflow: hidden;
  }
  .product-desc.expanded { max-height: none; }

  .price-block {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    background: #f0fdf4;
    padding: 8px 16px;
    border-radius: 8px;
    margin-bottom: 16px;
  }
  .price-main {
    font-size: 28px;
    font-weight: 700;
    color: #16a34a;
  }
  .price-label {
    font-size: 12px;
    color: var(--text-light);
  }

  .price-tiers {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }
  .price-tier {
    padding: 4px 10px;
    background: var(--bg);
    border-radius: 6px;
    font-size: 12px;
    color: var(--text-light);
  }
  .price-tier strong { color: var(--text); }

  .add-quote-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-top: 12px;
    padding: 9px 18px;
    background: linear-gradient(135deg, #e8701a, #d0611a);
    color: #fff;
    border: none;
    border-radius: 9px;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    letter-spacing: .3px;
    transition: all .2s;
  }
  .add-quote-btn:hover { transform: translateY(-1px); box-shadow: 0 5px 16px rgba(232,112,26,.4); }
  .add-quote-btn-sm {
    display: inline-block;
    margin-top: 6px;
    padding: 4px 10px;
    background: #e8701a;
    color: #fff;
    border: none;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    cursor: pointer;
    transition: background .2s;
  }
  .add-quote-btn-sm:hover { background: #d0611a; }

  .color-swatches {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 8px;
  }
  .swatch {
    padding: 4px 10px;
    border: 1px solid var(--border);
    border-radius: 14px;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .swatch:hover, .swatch.active {
    border-color: var(--primary);
    background: var(--primary);
    color: white;
  }

  .inventory-section {
    border-top: 2px solid var(--bg);
    padding: 20px 24px;
  }
  .inventory-section h3 {
    font-size: 15px;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .inv-table-wrap {
    overflow-x: auto;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .inv-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .inv-table th {
    background: var(--primary);
    color: white;
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    white-space: nowrap;
    position: sticky;
    top: 0;
  }
  .inv-table th.fav-wh {
    background: var(--accent);
  }
  .inv-table td {
    padding: 8px 14px;
    border-bottom: 1px solid #f0f0f0;
    white-space: nowrap;
  }
  .inv-table tr:hover td { background: #eef2f7; }
  .inv-table tr:nth-child(even) td { background: #fafbfc; }

  .inv-table td.fav-wh {
    background: var(--fav-bg) !important;
    font-weight: 700;
  }
  .qty-val { font-variant-numeric: tabular-nums; }
  .qty-high { color: #16a34a; font-weight: 600; }
  .qty-med { color: var(--warning); font-weight: 600; }
  .qty-low { color: var(--danger); font-weight: 600; }
  .qty-zero { color: #ccc; }

  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 60px;
    color: var(--text-light);
    font-size: 16px;
  }
  .spinner {
    width: 28px;
    height: 28px;
    border: 3px solid var(--border);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .empty-state {
    text-align: center;
    padding: 80px 24px;
    color: var(--text-light);
  }
  .empty-state .icon { font-size: 64px; margin-bottom: 16px; opacity: 0.5; }
  .empty-state h3 { font-size: 20px; color: var(--text); margin-bottom: 8px; }
  .empty-state p { max-width: 400px; margin: 0 auto; line-height: 1.5; }

  .quick-search {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
  }
  .quick-chip {
    padding: 6px 14px;
    background: rgba(255,255,255,0.15);
    border: 1px solid rgba(255,255,255,0.3);
    border-radius: 20px;
    color: white;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .quick-chip:hover { background: var(--accent); border-color: var(--accent); }

  .keyword-results {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px;
  }
  .keyword-result-card {
    padding: 14px;
    background: var(--card-bg);
    border: 2px solid var(--border);
    border-radius: var(--radius);
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
    font-weight: 600;
    color: var(--primary);
    font-size: 15px;
  }
  .keyword-result-card:hover {
    border-color: var(--accent);
    box-shadow: var(--shadow);
    transform: translateY(-2px);
  }

  .size-filter-row {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 10px;
  }
  .size-chip {
    padding: 4px 10px;
    border: 1px solid var(--border);
    border-radius: 14px;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
    background: white;
  }
  .size-chip:hover, .size-chip.active {
    border-color: var(--primary);
    background: var(--primary);
    color: white;
  }

  .wh-select-all {
    font-size: 12px;
    color: var(--primary);
    cursor: pointer;
    text-decoration: underline;
    margin-bottom: 8px;
    display: inline-block;
  }

  .debug-toggle {
    font-size: 11px;
    color: rgba(255,255,255,0.5);
    cursor: pointer;
    text-decoration: underline;
    margin-left: 8px;
  }

  @media (max-width: 900px) {
    .main { flex-direction: column; }
    .sidebar { width: 100%; position: static; }
    .product-header { flex-direction: column; }
    .product-image { width: 100%; height: 200px; }
  }

  .toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--primary);
    color: white;
    padding: 12px 20px;
    border-radius: 8px;
    box-shadow: var(--shadow-lg);
    font-size: 14px;
    z-index: 1000;
    opacity: 0;
    transform: translateY(20px);
    transition: all 0.3s;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.error { background: var(--danger); }

  /* ── Quote Builder Modal ──────────────────────────────── */
  .qt-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.55); z-index: 900;
    align-items: center; justify-content: center;
  }
  .qt-overlay.open { display: flex; }

  .qt-drawer {
    position: relative;
    width: 80vw; max-width: 960px; height: 82vh;
    background: #eef0f4; z-index: 901;
    border-radius: 18px;
    box-shadow: 0 24px 64px rgba(0,0,0,.35);
    display: flex; flex-direction: column; overflow: hidden;
    opacity: 0; transform: scale(.96);
    transition: opacity .2s ease, transform .2s ease;
    pointer-events: none;
  }
  .qt-overlay.open .qt-drawer {
    opacity: 1; transform: scale(1); pointer-events: all;
  }

  .qt-drawer-header {
    background: #1a2744; color: #fff; padding: 0 20px;
    height: 60px; display: flex; align-items: center;
    justify-content: space-between; flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(0,0,0,.25);
  }
  .qt-drawer-title { font-size: 1rem; font-weight: 800; letter-spacing: -.3px; }
  .qt-drawer-title span { color: #e8701a; }
  .qt-drawer-actions { display: flex; gap: 8px; align-items: center; }
  .qt-hdr-btn {
    background: transparent; border: 1.5px solid rgba(255,255,255,.3);
    color: #fff; padding: 5px 12px; border-radius: 7px; cursor: pointer;
    font-size: .78rem; font-weight: 600; transition: all .2s;
  }
  .qt-hdr-btn:hover { background: rgba(255,255,255,.12); }
  .qt-close-btn {
    background: transparent; border: none; color: rgba(255,255,255,.6);
    font-size: 1.4rem; cursor: pointer; line-height: 1;
    padding: 4px 6px; border-radius: 6px; transition: all .2s;
  }
  .qt-close-btn:hover { color: #fff; background: rgba(255,255,255,.1); }

  .qt-body {
    flex: 1; overflow-y: auto; padding: 20px 20px 60px;
  }

  .qt-card {
    background: #fff; border-radius: 14px; padding: 22px;
    margin-bottom: 16px; box-shadow: 0 2px 10px rgba(0,0,0,.06);
  }
  .qt-card-title {
    font-size: .68rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1.2px; color: #94a3b8; margin-bottom: 14px;
  }
  .qt-form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
  .qt-form-group { display: flex; flex-direction: column; }
  .qt-form-group label { font-size: .78rem; font-weight: 700; color: #374151; margin-bottom: 5px; }
  .qt-hint { font-size: .72rem; color: #94a3b8; margin-top: 3px; }

  .qt-input {
    border: 1.5px solid #e2e8f0; border-radius: 9px; padding: 9px 12px;
    font-size: .92rem; color: #1a2744; outline: none; width: 100%;
    transition: border-color .2s, box-shadow .2s; background: #fff;
    font-family: inherit; -moz-appearance: textfield;
  }
  .qt-input:focus { border-color: #e8701a; box-shadow: 0 0 0 3px rgba(232,112,26,.12); }
  .qt-input::-webkit-inner-spin-button { opacity: .5; }

  .qt-loc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .hidden { display: none !important; }
  .qt-loc-panel {
    border: 2px solid #e2e8f0; border-radius: 10px; padding: 14px;
    transition: all .2s; background: #f8fafc;
  }
  .qt-loc-panel.active-panel { background: #fff; border-color: #1a2744; }
  .qt-loc-name {
    font-size: .72rem; font-weight: 800; text-transform: uppercase;
    letter-spacing: 1px; color: #94a3b8; margin-bottom: 10px; display: block;
  }
  .qt-loc-panel.active-panel .qt-loc-name { color: #1a2744; }
  .qt-loc-pills { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
  .qt-loc-pill {
    border: 1.5px solid #e2e8f0; background: #fff; border-radius: 20px;
    padding: 4px 10px; font-size: .72rem; font-weight: 700; color: #64748b;
    cursor: pointer; transition: all .2s; white-space: nowrap;
  }
  .qt-loc-pill:hover { border-color: #1a2744; color: #1a2744; }
  .qt-loc-pill.active-none { border-color: #94a3b8; background: #f1f5f9; color: #64748b; }
  .qt-loc-pill.active-type { border-color: #e8701a; background: #fff8f3; color: #e8701a; }
  .qt-loc-opts { margin-top: 4px; }
  .qt-loc-opts label { font-size: .72rem; }
  .qt-select {
    border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 7px 10px;
    font-size: .85rem; color: #1a2744; outline: none; width: 100%;
    transition: border-color .2s; background: #fff; font-family: inherit;
  }
  .qt-select:focus { border-color: #e8701a; }

  .qt-alert { border-radius: 8px; padding: 9px 14px; font-size: .8rem; margin-bottom: 12px; display: none; }
  .qt-alert.warn { background: #fef3c7; border: 1px solid #f59e0b; color: #92400e; }
  .qt-alert.err  { background: #fee2e2; border: 1px solid #ef4444; color: #991b1b; }
  .qt-alert.show { display: block; }

  .qt-calc-btn {
    background: linear-gradient(135deg, #e8701a, #d0611a); color: #fff;
    border: none; border-radius: 10px; padding: 13px; font-size: .95rem;
    font-weight: 800; cursor: pointer; width: 100%; letter-spacing: .4px;
    transition: all .2s;
  }
  .qt-calc-btn:hover { transform: translateY(-1px); box-shadow: 0 5px 16px rgba(232,112,26,.35); }

  .qt-results-card {
    background: linear-gradient(135deg, #1a2744, #0f1d3a); color: #fff;
    border-radius: 14px; padding: 22px; margin-bottom: 16px;
    box-shadow: 0 4px 20px rgba(26,39,68,.3);
  }
  .qt-results-card .qt-card-title { color: rgba(255,255,255,.45); }
  .qt-results-primary { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; margin-bottom: 14px; }
  .qt-res-big { background: rgba(255,255,255,.08); border-radius: 10px; padding: 14px 10px; text-align: center; }
  .qt-res-big .val { font-size: 1.5rem; font-weight: 800; color: #f5a623; line-height: 1; }
  .qt-res-big .lbl { font-size: .68rem; color: rgba(255,255,255,.55); margin-top: 5px; text-transform: uppercase; letter-spacing: .7px; }
  .qt-results-secondary { display: grid; grid-template-columns: repeat(2,1fr); gap: 8px; }
  .qt-res-row { display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,.06); border-radius: 8px; padding: 9px 12px; }
  .qt-res-row .rk { font-size: .76rem; color: rgba(255,255,255,.55); }
  .qt-res-row .rv { font-size: .84rem; font-weight: 700; }
  .qt-res-row.highlight .rv { color: #6ee7b7; }
  .qt-res-row.span2 { grid-column: 1 / -1; }
  .qt-loc-breakdown { display: flex; flex-direction: column; gap: 5px; margin-bottom: 12px; }
  .qt-loc-line { display: flex; justify-content: space-between; padding: 7px 12px; background: rgba(255,255,255,.04); border-radius: 7px; font-size: .76rem; }
  .qt-loc-line .lk { color: rgba(255,255,255,.5); }
  .qt-loc-line .lv { font-weight: 600; }

  .qt-quote-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .qt-quote-actions { display: flex; gap: 8px; margin-top: 12px; }
  .qt-btn-outline {
    flex: 1; background: #fff; border: 1.5px solid #e2e8f0; border-radius: 8px;
    padding: 9px; font-size: .82rem; font-weight: 700; cursor: pointer;
    color: #374151; transition: all .2s;
  }
  .qt-btn-outline:hover { border-color: #1a2744; color: #1a2744; }
  .qt-btn-solid {
    flex: 1; background: #1a2744; color: #fff; border: none;
    border-radius: 8px; padding: 9px; font-size: .82rem; font-weight: 700;
    cursor: pointer; transition: all .2s;
  }
  .qt-btn-solid:hover { background: #243564; }
  #qt-quote-preview {
    width: 100%; border: 1.5px solid #e2e8f0; border-radius: 10px;
    min-height: 300px; display: block; background: #fff;
  }
  .qt-copy-flash { font-size: .75rem; color: #059669; font-weight: 700; opacity: 0; transition: opacity .3s; }
  .qt-copy-flash.show { opacity: 1; }

  .qt-overlay-modal {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.55);
    z-index: 1000; align-items: center; justify-content: center; padding: 16px;
  }
  .qt-overlay-modal.open { display: flex; }
  .qt-modal {
    background: #fff; border-radius: 16px; padding: 24px; width: 100%;
    max-width: 480px; max-height: 88vh; overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0,0,0,.3);
  }
  .qt-modal-title { font-size: 1.1rem; font-weight: 800; margin-bottom: 5px; }
  .qt-modal-sub { font-size: .82rem; color: #64748b; margin-bottom: 18px; }
  .qt-modal-actions { display: flex; gap: 10px; margin-top: 18px; }
  #qt-edit-html-area {
    width: 100%; min-height: 260px; border: 1.5px solid #e2e8f0;
    border-radius: 9px; padding: 12px; font-size: .76rem;
    font-family: 'Courier New', monospace; outline: none; resize: vertical;
  }
  #qt-edit-html-area:focus { border-color: #e8701a; }
  .qt-quote-list { display: flex; flex-direction: column; gap: 8px; }
  .qt-quote-item {
    border: 1.5px solid #e2e8f0; border-radius: 9px; padding: 12px 14px;
    display: flex; justify-content: space-between; align-items: center;
    cursor: pointer; transition: all .2s;
  }
  .qt-quote-item:hover { border-color: #1a2744; background: #f8fafc; }
  .qt-qi-name { font-weight: 700; font-size: .88rem; }
  .qt-qi-meta { font-size: .74rem; color: #94a3b8; margin-top: 2px; }
  .qt-qi-del { background: none; border: none; color: #ef4444; cursor: pointer; font-size: 1rem; padding: 4px 8px; border-radius: 6px; }
  .qt-qi-del:hover { background: #fee2e2; }
  .qt-empty-state { text-align: center; color: #94a3b8; padding: 28px; font-size: .88rem; }

  #qt-results-section { display: none; }

  .catalog-reminder {
    display: none; position: fixed; bottom: 24px; left: 24px; z-index: 800;
    background: #fff; border: 2px solid #f59e0b; border-radius: 12px;
    padding: 14px 18px; max-width: 320px;
    box-shadow: 0 4px 20px rgba(0,0,0,.15);
    font-size: 13px; color: #1a2744; line-height: 1.5;
  }
  .catalog-reminder.show { display: block; }
  .catalog-reminder strong { color: #92400e; display: block; margin-bottom: 4px; font-size: 13px; }
  .catalog-reminder-actions { display: flex; gap: 8px; margin-top: 10px; }
  .cr-btn-rebuild {
    flex: 1; background: #1a2744; color: #fff; border: none;
    border-radius: 7px; padding: 7px 12px; font-size: 12px;
    font-weight: 700; cursor: pointer; transition: background .2s;
  }
  .cr-btn-rebuild:hover { background: #243564; }
  .cr-btn-dismiss {
    background: transparent; border: 1.5px solid #e2e8f0; border-radius: 7px;
    padding: 7px 12px; font-size: 12px; font-weight: 600;
    color: #64748b; cursor: pointer; transition: all .2s;
  }
  .cr-btn-dismiss:hover { border-color: #94a3b8; }

  .quote-tool-fab {
    position: fixed; bottom: 28px; right: 28px; z-index: 800;
    background: linear-gradient(135deg, #e8701a, #d0611a);
    color: #fff; border: none; border-radius: 50px;
    padding: 13px 22px; font-size: .9rem; font-weight: 800;
    cursor: pointer; box-shadow: 0 4px 18px rgba(232,112,26,.45);
    transition: all .2s; display: flex; align-items: center; gap: 8px;
  }
  .quote-tool-fab:hover { transform: translateY(-2px); box-shadow: 0 7px 24px rgba(232,112,26,.55); }
</style>
</head>
<body>

<header class="header">
  <div class="header-inner">
    <div class="logo">
      <a href="https://www.4zdesign.com" target="_blank" rel="noopener" title="4Z Design - Marketing &amp; Printing" style="display:flex;align-items:center;gap:12px;text-decoration:none;color:inherit;">
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 405 430" width="38" height="38" style="flex-shrink:0;">
          <path fill="#fe7a16" d="M221.8,387.7c21.6,0,42.2-4.4,60.9-12.3h52c-31.1,24.4-70.3,39-113,39-101.1,0-183-81.9-183-183s0-3,0-4.5h26.7c0,1.5,0,3,0,4.5,0,86.3,70,156.3,156.3,156.3h0ZM38.7,208.7h120.3v88.7l42-43.1v-45.6h79.9l-123.6,127.2,15.1,21.3h182.2c31.1-32.8,50.2-77.1,50.2-125.9,0-101.1-81.9-183-183-183s-1.8,0-2.7,0v26.7c.9,0,1.8,0,2.7,0,86.3,0,156.3,70,156.3,156.3s-9.7,61.9-26.3,86.7h-113.7l106.9-109.4v-39.2h-144.1V48.6h-42l-120.3,120.9v39.2h0ZM159,102.1v67.4h-65.9l65.9-67.4h0Z"/>
        </svg>
        <div>
          SanMar Search
          <small>by 4Z Design</small>
        </div>
      </a>
    </div>

    <div class="search-box">
      <input type="text" id="searchInput" placeholder="Style # (PC54), keyword (polo), or describe it (red polo, nike jacket...)" autocomplete="off" />
      <button onclick="doSearch()" id="searchBtn">Search</button>
    </div>

    <span class="debug-toggle" onclick="buildCatalog()" title="Build full product catalog for keyword search" id="catalogBtn">&#128230; build catalog</span>
    <span class="debug-toggle" onclick="runHealthCheck()" title="Test API connectivity">&#9889; health check</span>
    <span class="debug-toggle" onclick="toggleDebug()">debug</span>
    <span class="debug-toggle" onclick="qtOpen()" title="Open Quote Builder" style="color:#e8701a;font-weight:700;">&#128203; Quote Builder</span>
  </div>

  <div class="header-inner" style="padding-top: 8px;">
    <div class="quick-search" id="recentSearches" style="display:none;">
      <span style="font-size:12px;opacity:0.7;line-height:28px;">Recent:</span>
    </div>
  </div>
</header>

<div class="main">
  <aside class="sidebar" id="sidebar" style="display:none;">
    <div class="filter-card">
      <h3>&#128230; Warehouse Filter</h3>
      <span class="wh-select-all" onclick="toggleAllWarehouses()">Select All / None</span>
      <ul class="warehouse-list" id="warehouseList"></ul>
    </div>
    <div class="filter-card" id="brandFilterCard" style="display:none;">
      <h3>&#127991; Brand</h3>
      <div class="color-filters" id="brandFilters"></div>
    </div>
    <div class="filter-card" id="genderFilterCard" style="display:none;">
      <h3>&#9892; Gender</h3>
      <div class="color-filters" id="genderFilters"></div>
    </div>
    <div class="filter-card" id="categoryFilterCard" style="display:none;">
      <h3>&#128193; Category</h3>
      <div class="color-filters" id="categoryFilters"></div>
    </div>
    <div class="filter-card" id="colorFilterCard" style="display:none;">
      <h3>&#127912; Colors</h3>
      <div class="color-filters" id="colorFilters"></div>
    </div>
    <div class="filter-card" id="sizeFilterCard" style="display:none;">
      <h3>&#128207; Sizes</h3>
      <div class="size-filter-row" id="sizeFilters"></div>
    </div>
  </aside>

  <main class="content" id="content">
    <div class="empty-state" id="emptyState">
      <div class="icon">&#128269;</div>
      <h3>Search SanMar Products</h3>
      <p>Enter a style number like <strong>PC54</strong> for a direct lookup, or describe what you need like <strong>"red polo"</strong> or <strong>"nike jacket"</strong>.</p>
      <div id="catalogStatus" style="margin-top:20px;font-size:13px;"></div>
    </div>
  </main>
</div>

<div class="toast" id="toast"></div>

<script>
const DEFAULT_WAREHOUSES = {{ config.favorite_warehouses | tojson }};
const HIGHLIGHT_WAREHOUSES = {{ config.highlight_warehouses | tojson }};
let currentProducts = [];
let allWarehouses = [];
let selectedWarehouses = new Set();
let selectedColor = null;
let selectedSize = null;
let selectedBrand = null;
let selectedCategory = null;
let selectedGender = null;
let debugMode = false;
let warehouseDefaultSet = false;
let currentSort = 'default';
let preferredWarehouse = 'Seattle';  // User's warehouse preference from browse page

const searchInput = document.getElementById('searchInput');
searchInput.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

let recentSearches = [];

function quickSearch(term) {
  searchInput.value = term;
  doSearch();
}

function addRecentSearch(term) {
  if (!term) return;
  recentSearches = recentSearches.filter(s => s.toLowerCase() !== term.toLowerCase());
  recentSearches.unshift(term);
  if (recentSearches.length > 10) recentSearches.pop();
  renderRecentSearches();
}

function renderRecentSearches() {
  const container = document.getElementById('recentSearches');
  if (!container || recentSearches.length === 0) { if (container) container.style.display = 'none'; return; }
  container.style.display = 'flex';
  let html = '<span style="font-size:12px;opacity:0.7;line-height:28px;">Recent:</span>';
  for (const s of recentSearches) {
    html += `<span class="quick-chip" onclick="quickSearch('${s.replace(/'/g, "\\'")}')">${s}</span>`;
  }
  container.innerHTML = html;
}

async function doSearch() {
  const q = searchInput.value.trim();
  if (!q) return;
  addRecentSearch(q);

  const btn = document.getElementById('searchBtn');
  btn.disabled = true;
  btn.textContent = 'Searching...';
  showLoading();

  try {
    const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await resp.json();

    if (data.error) {
      showError(data.error);
      return;
    }

    if (data.searchType === 'style' && data.products && data.products.length === 1) {
      // Single exact style match — show full product detail
      currentProducts = data.products;
      selectedBrand = null; selectedCategory = null; selectedGender = null;
      selectedSize = null; selectedColor = null; currentSort = 'default';
      warehouseDefaultSet = false;
      renderProducts();
    } else if (data.products && data.products.length > 0) {
      // All other results — unified browse view
      showBrowseResults(data);
    } else {
      showEmpty('No products found for "' + q + '"');
    }
  } catch (err) {
    showError('Search failed: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Search';
  }
}

async function loadProduct(productId) {
  showLoading();
  try {
    const resp = await fetch(`/api/product/${encodeURIComponent(productId)}`);
    const data = await resp.json();
    if (data.error) {
      showError(data.error);
      return;
    }
    currentProducts = [data];
    renderProducts();
  } catch (err) {
    showError('Failed to load product: ' + err.message);
  }
}

function showLoading() {
  document.getElementById('content').innerHTML = `
    <div class="loading"><div class="spinner"></div>Fetching from SanMar...</div>
  `;
}

function showEmpty(msg) {
  document.getElementById('content').innerHTML = `
    <div class="empty-state">
      <div class="icon">&#128528;</div>
      <h3>${msg}</h3>
    </div>
  `;
}

function showError(msg) {
  document.getElementById('content').innerHTML = `
    <div class="empty-state">
      <div class="icon">&#9888;&#65039;</div>
      <h3>Something went wrong</h3>
      <p>${msg}</p>
      ${debugMode ? '<p style="margin-top:12px"><a href="#" onclick="showDebugInfo()">View debug info</a></p>' : ''}
    </div>
  `;
}

function showBrowseResults(data) {
  const sidebar = document.getElementById('sidebar');
  sidebar.style.display = 'none';

  // Auto-apply color filter from natural language search
  if (data.autoFilterColors && data.autoFilterColors.length > 0) {
    toast(`Filtering by color: ${data.autoFilterColors[0]}`);
  }

  // Build filter options from data
  const allBrands = [...new Set(data.products.map(p => p.productBrand).filter(Boolean))].sort();
  const allCategories = [...new Set(data.products.flatMap(p => p.categories || []).filter(Boolean))].sort();
  const allColors = [...new Set(data.products.flatMap(p => p.colorNames || []).filter(Boolean))].sort();

  const selStyle = 'padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:#fff;';

  let html = `<div class="status-bar">${data.message || 'Found ' + data.products.length + ' products'}</div>`;
  const warehouseOptions = ['Seattle', 'Reno', 'Dallas', 'Cincinnati', 'Orlando', 'Kansas City', 'Pittsburgh', 'Jacksonville', 'Minneapolis', 'Phoenix'];

  html += `<div style="display:flex;gap:6px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
    <select id="browseWarehouseFilter" onchange="setPreferredWarehouse(this.value)" style="${selStyle};border-color:#2563eb;font-weight:600;">
      ${warehouseOptions.map(w => '<option value="' + w + '"' + (w === preferredWarehouse ? ' selected' : '') + '>' + (w === preferredWarehouse ? '\u2713 ' : '') + w + '</option>').join('')}
    </select>
    <select id="browseGenderFilter" onchange="sortBrowseResults()" style="${selStyle}">
      <option value="all">All Genders</option>
      <option value="Men">Men</option>
      <option value="Women">Women</option>
      <option value="Youth">Youth</option>
      <option value="Unisex">Unisex Only</option>
    </select>
    <select id="browseColorFilter" onchange="sortBrowseResults()" style="${selStyle}">
      <option value="all">All Colors</option>
      ${allColors.map(c => '<option value="' + c + '"' + (data.autoFilterColors && data.autoFilterColors[0] === c ? ' selected' : '') + '>' + c + '</option>').join('')}
    </select>
    <select id="browseBrandFilter" onchange="sortBrowseResults()" style="${selStyle}">
      <option value="all">All Brands</option>
      ${allBrands.map(b => '<option value="' + b.replace(/"/g, '&quot;') + '">' + b + '</option>').join('')}
    </select>
    <select id="browseCategoryFilter" onchange="sortBrowseResults()" style="${selStyle}">
      <option value="all">All Categories</option>
      ${allCategories.map(c => '<option value="' + c.replace(/"/g, '&quot;') + '">' + c + '</option>').join('')}
    </select>
    <select id="browsePriceFilter" onchange="sortBrowseResults()" style="${selStyle}">
      <option value="all">All Prices</option>
      <option value="0-5">Under $5</option>
      <option value="5-10">$5 - $10</option>
      <option value="10-20">$10 - $20</option>
      <option value="20-50">$20 - $50</option>
      <option value="50-999">$50+</option>
    </select>
    <select id="browseSortSelect" onchange="sortBrowseResults()" style="${selStyle}">
      <option value="default">Sort: Default</option>
      <option value="price_asc">Sort: Price Low-High</option>
      <option value="price_desc">Sort: Price High-Low</option>
      <option value="name_asc">Sort: Name A-Z</option>
      <option value="brand_asc">Sort: Brand A-Z</option>
    </select>
  </div>`;
  html += '<div class="keyword-results" id="browseGrid"></div>';

  window._browseData = data.products;
  document.getElementById('content').innerHTML = html;
  sortBrowseResults();
}

function sortBrowseResults() {
  let items = [...(window._browseData || [])];
  const sortVal = document.getElementById('browseSortSelect')?.value || 'default';
  const priceVal = document.getElementById('browsePriceFilter')?.value || 'all';
  const brandVal = document.getElementById('browseBrandFilter')?.value || 'all';
  const genderVal = document.getElementById('browseGenderFilter')?.value || 'all';
  const colorVal = document.getElementById('browseColorFilter')?.value || 'all';
  const catVal = document.getElementById('browseCategoryFilter')?.value || 'all';

  // Filter by price range
  if (priceVal !== 'all') {
    const [minP, maxP] = priceVal.split('-').map(Number);
    items = items.filter(p => {
      const price = parseFloat(String(p.basePrice || '').replace(/[^0-9.]/g, ''));
      if (isNaN(price)) return false;
      return price >= minP && price <= maxP;
    });
  }

  // Filter by brand
  if (brandVal !== 'all') {
    items = items.filter(p => p.productBrand === brandVal);
  }

  // Filter by gender (Men/Women include Unisex products)
  if (genderVal !== 'all') {
    items = items.filter(p => {
      const g = p.gender || 'Unisex';
      if (genderVal === 'Men') return g === 'Men' || g === 'Unisex';
      if (genderVal === 'Women') return g === 'Women' || g === 'Unisex';
      return g === genderVal;
    });
  }

  // Filter by color
  if (colorVal !== 'all') {
    const cl = colorVal.toLowerCase();
    items = items.filter(p => (p.colorNames || []).some(c => c.toLowerCase() === cl));
  }

  // Filter by category
  if (catVal !== 'all') {
    items = items.filter(p => (p.categories || []).includes(catVal));
  }

  // Sort
  if (sortVal === 'price_asc') {
    items.sort((a, b) => (parseFloat(a.basePrice) || 999) - (parseFloat(b.basePrice) || 999));
  } else if (sortVal === 'price_desc') {
    items.sort((a, b) => (parseFloat(b.basePrice) || 0) - (parseFloat(a.basePrice) || 0));
  } else if (sortVal === 'name_asc') {
    items.sort((a, b) => (a.productName || '').localeCompare(b.productName || ''));
  } else if (sortVal === 'brand_asc') {
    items.sort((a, b) => (a.productBrand || '').localeCompare(b.productBrand || ''));
  }

  // Render — show count
  const grid = document.getElementById('browseGrid');
  if (!grid) return;
  const statusBar = document.querySelector('.status-bar');
  if (statusBar) statusBar.textContent = items.length + ' products' + (items.length !== (window._browseData || []).length ? ' (filtered)' : '');

  let html = '';
  for (const p of items) {
    const name = p.productName || p.productId;
    const brand = p.productBrand ? `<div style="font-size:11px;color:var(--text-light);margin-top:3px;">${p.productBrand}</div>` : '';
    const price = p.basePrice ? `<div style="font-size:14px;font-weight:700;color:#16a34a;margin-top:4px;">$${p.basePrice}</div>` : '';
    const safeName = (name||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    const addBtn = p.basePrice
      ? `<button class="add-quote-btn-sm" data-qname="${safeName}" data-qprice="${p.basePrice}" onclick="event.stopPropagation();addToQuote(this.dataset.qname,this.dataset.qprice)">+ Quote</button>`
      : '';
    html += `<div class="keyword-result-card" onclick="loadProduct('${p.productId}')" style="text-align:left;padding:12px;">
      <div style="display:flex;justify-content:space-between;align-items:start;">
        <div style="font-size:13px;font-weight:700;color:var(--primary);">${p.productId}</div>
        ${price}
      </div>
      <div style="font-size:12px;margin-top:2px;">${name}</div>
      ${brand}
      ${addBtn}
    </div>`;
  }
  if (items.length === 0) {
    html = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text-light);">No products match your filters.</div>';
  }
  grid.innerHTML = html;
}

function setPreferredWarehouse(wh) {
  preferredWarehouse = wh;
  // Reset warehouse default so next product detail uses new preference
  warehouseDefaultSet = false;
  toast('Warehouse preference: ' + wh);
}

function renderProducts() {
  const content = document.getElementById('content');
  const sidebar = document.getElementById('sidebar');

  if (!currentProducts.length) {
    showEmpty('No products to display');
    sidebar.style.display = 'none';
    return;
  }

  allWarehouses = [];
  const whSet = new Set();
  for (const prod of currentProducts) {
    const inv = prod.inventory;
    if (inv && inv.warehouses) {
      for (const wh of inv.warehouses) {
        if (!whSet.has(wh)) {
          whSet.add(wh);
          allWarehouses.push(wh);
        }
      }
    }
  }

  allWarehouses.sort((a, b) => {
    const aFav = HIGHLIGHT_WAREHOUSES.some(f => a.toLowerCase().includes(f.toLowerCase()));
    const bFav = HIGHLIGHT_WAREHOUSES.some(f => b.toLowerCase().includes(f.toLowerCase()));
    if (aFav && !bFav) return -1;
    if (!aFav && bFav) return 1;
    return a.localeCompare(b);
  });

  if (!warehouseDefaultSet) {
    // Use preferredWarehouse from browse page dropdown, fall back to DEFAULT_WAREHOUSES
    const prefList = preferredWarehouse ? [preferredWarehouse] : DEFAULT_WAREHOUSES;
    selectedWarehouses.clear();
    for (const wh of allWarehouses) {
      const isDef = prefList.some(f => wh.toLowerCase().includes(f.toLowerCase()));
      if (isDef) selectedWarehouses.add(wh);
    }
    // If no match found, select all as fallback
    if (selectedWarehouses.size === 0) {
      allWarehouses.forEach(w => selectedWarehouses.add(w));
    }
    warehouseDefaultSet = true;
  }

  renderWarehouseSidebar();
  renderAllFilters();
  sidebar.style.display = 'block';

  // Apply brand and category filters
  let visibleProducts = getVisibleProducts();

  // Apply sort
  if (currentSort === 'price_asc') {
    visibleProducts.sort((a, b) => (parseFloat(a.basePrice) || 999) - (parseFloat(b.basePrice) || 999));
  } else if (currentSort === 'price_desc') {
    visibleProducts.sort((a, b) => (parseFloat(b.basePrice) || 0) - (parseFloat(a.basePrice) || 0));
  } else if (currentSort === 'name_asc') {
    visibleProducts.sort((a, b) => (a.productName || '').localeCompare(b.productName || ''));
  }

  let html = '';
  html += `<div class="status-bar">
    <span>Showing ${visibleProducts.length} of ${currentProducts.length} product(s)</span>
    <div style="display:flex;gap:8px;align-items:center;">
      <label style="font-size:12px;color:var(--text-light);">Sort:</label>
      <select onchange="setSort(this.value)" style="padding:4px 8px;border:1px solid var(--border);border-radius:6px;font-size:12px;">
        <option value="default" ${currentSort==='default'?'selected':''}>Default</option>
        <option value="price_asc" ${currentSort==='price_asc'?'selected':''}>Price: Low to High</option>
        <option value="price_desc" ${currentSort==='price_desc'?'selected':''}>Price: High to Low</option>
        <option value="name_asc" ${currentSort==='name_asc'?'selected':''}>Name: A-Z</option>
      </select>
    </div>
  </div>`;

  for (const prod of visibleProducts) {
    html += renderProductCard(prod);
  }

  if (visibleProducts.length === 0 && currentProducts.length > 0) {
    html += `<div class="empty-state"><div class="icon">&#128683;</div><h3>No products match your filters</h3><p>Try changing your brand, category, or color filters.</p></div>`;
  }

  content.innerHTML = html;
}

function renderWarehouseSidebar() {
  const list = document.getElementById('warehouseList');
  let html = '';

  const whTotals = {};
  if (currentProducts.length > 0) {
    const inv = currentProducts[0].inventory;
    if (inv && inv.parts) {
      for (const part of inv.parts) {
        for (const loc of (part.locations || [])) {
          whTotals[loc.name] = (whTotals[loc.name] || 0) + parseInt(loc.qty || 0);
        }
      }
    }
  }

  for (const wh of allWarehouses) {
    const isFav = HIGHLIGHT_WAREHOUSES.some(f => wh.toLowerCase().includes(f.toLowerCase()));
    const checked = selectedWarehouses.has(wh) ? 'checked' : '';
    const total = whTotals[wh] || 0;
    html += `
      <li class="${isFav ? 'favorite' : ''}" onclick="toggleWarehouse('${wh}', this)">
        <input type="checkbox" ${checked} onclick="event.stopPropagation(); toggleWarehouse('${wh}')" />
        <span>${wh}</span>
        <span class="fav-star">&#11088;</span>
        <span class="wh-qty">${total.toLocaleString()}</span>
      </li>
    `;
  }
  list.innerHTML = html;
}

function renderAllFilters() {
  // ── Brands ──
  const brandSet = new Set();
  for (const prod of currentProducts) {
    const b = (prod.productBrand || '').trim();
    if (b) brandSet.add(b);
  }
  const brandCard = document.getElementById('brandFilterCard');
  const brandDiv = document.getElementById('brandFilters');
  if (brandSet.size > 1) {
    brandCard.style.display = 'block';
    let html = `<span class="color-chip ${!selectedBrand ? 'active' : ''}" onclick="filterBrand(null)">All</span>`;
    for (const b of [...brandSet].sort()) {
      html += `<span class="color-chip ${selectedBrand === b ? 'active' : ''}" onclick="filterBrand('${b.replace(/'/g, "\\'")}')">${b}</span>`;
    }
    brandDiv.innerHTML = html;
  } else {
    brandCard.style.display = 'none';
  }

  // ── Gender ──
  const genderOrder = ['Men', 'Women', 'Youth', 'Unisex'];
  const genderSet = new Set();
  for (const prod of currentProducts) {
    const g = (prod.gender || '').trim();
    if (g) genderSet.add(g);
  }
  const genderCard = document.getElementById('genderFilterCard');
  const genderDiv = document.getElementById('genderFilters');
  if (genderSet.size > 1) {
    genderCard.style.display = 'block';
    let html = `<span class="color-chip ${!selectedGender ? 'active' : ''}" onclick="filterGender(null)">All</span>`;
    for (const g of genderOrder.filter(g => genderSet.has(g))) {
      html += `<span class="color-chip ${selectedGender === g ? 'active' : ''}" onclick="filterGender('${g}')">${g}</span>`;
    }
    genderDiv.innerHTML = html;
  } else {
    genderCard.style.display = 'none';
  }

  // ── Categories ──
  const catSet = new Set();
  for (const prod of currentProducts) {
    for (const c of (prod.categories || [])) {
      const cat = c.trim();
      if (cat) catSet.add(cat);
    }
  }
  const catCard = document.getElementById('categoryFilterCard');
  const catDiv = document.getElementById('categoryFilters');
  if (catSet.size > 1) {
    catCard.style.display = 'block';
    let html = `<span class="color-chip ${!selectedCategory ? 'active' : ''}" onclick="filterCategory(null)">All</span>`;
    for (const c of [...catSet].sort()) {
      html += `<span class="color-chip ${selectedCategory === c ? 'active' : ''}" onclick="filterCategory('${c.replace(/'/g, "\\'")}')">${c}</span>`;
    }
    catDiv.innerHTML = html;
  } else {
    catCard.style.display = 'none';
  }

  // ── Colors ──
  const colorSet = new Set();
  for (const prod of getVisibleProducts()) {
    for (const c of (prod.allColors || [])) {
      colorSet.add(c.name);
    }
  }
  const colorCard = document.getElementById('colorFilterCard');
  const colorDiv = document.getElementById('colorFilters');
  if (colorSet.size > 0) {
    colorCard.style.display = 'block';
    let html = `<span class="color-chip ${!selectedColor ? 'active' : ''}" onclick="filterColor(null)">All</span>`;
    for (const c of [...colorSet].sort()) {
      html += `<span class="color-chip ${selectedColor === c ? 'active' : ''}" onclick="filterColor('${c}')">${c}</span>`;
    }
    colorDiv.innerHTML = html;
  } else {
    colorCard.style.display = 'none';
  }

  // ── Sizes ──
  const sizeSet = new Set();
  for (const prod of getVisibleProducts()) {
    for (const s of (prod.allSizes || [])) {
      sizeSet.add(s);
    }
  }
  const sizeCard = document.getElementById('sizeFilterCard');
  const sizeDiv = document.getElementById('sizeFilters');
  if (sizeSet.size > 0) {
    sizeCard.style.display = 'block';
    let html = `<span class="size-chip ${!selectedSize ? 'active' : ''}" onclick="filterSize(null)">All</span>`;
    for (const s of [...sizeSet]) {
      html += `<span class="size-chip ${selectedSize === s ? 'active' : ''}" onclick="filterSize('${s}')">${s}</span>`;
    }
    sizeDiv.innerHTML = html;
  } else {
    sizeCard.style.display = 'none';
  }
}

// Get products filtered by brand, category, and gender (used before rendering cards)
function getVisibleProducts() {
  let prods = currentProducts;
  if (selectedBrand) {
    prods = prods.filter(p => (p.productBrand || '') === selectedBrand);
  }
  if (selectedCategory) {
    prods = prods.filter(p => (p.categories || []).includes(selectedCategory));
  }
  if (selectedGender) {
    prods = prods.filter(p => {
      const g = p.gender || 'Unisex';
      if (selectedGender === 'Men') return g === 'Men' || g === 'Unisex';
      if (selectedGender === 'Women') return g === 'Women' || g === 'Unisex';
      return g === selectedGender;
    });
  }
  return prods;
}

// Build a map of color name -> image URL from the product's images + parts cross-reference
function buildColorImageMap(prod) {
  const map = {};
  if (!prod.images || prod.images.length === 0) return map;

  // Step 1: Build partId -> image URL map from media data
  const partIdToUrl = {};
  for (const img of prod.images) {
    const pid = (img.partId || '').toLowerCase().trim();
    if (pid && !partIdToUrl[pid]) {
      partIdToUrl[pid] = img.url;
    }
    // Also map by explicit color from media service
    const color = (img.color || '').toLowerCase().trim();
    if (color && !map[color]) {
      map[color] = img.url;
    }
  }

  // Step 2: Build color name -> partId mapping from product parts data
  if (prod.parts) {
    for (const part of prod.parts) {
      const pid = (part.partId || '').toLowerCase().trim();
      for (const c of (part.colors || [])) {
        const colorName = (c.name || '').toLowerCase().trim();
        if (colorName && pid) {
          // Try to find an image for this partId
          let url = partIdToUrl[pid];
          // Also try partial partId match (media partId might be shorter)
          if (!url) {
            for (const [mpid, murl] of Object.entries(partIdToUrl)) {
              if (pid.includes(mpid) || mpid.includes(pid)) {
                url = murl;
                break;
              }
            }
          }
          if (url && !map[colorName]) {
            map[colorName] = url;
          }
        }
      }
    }
  }

  // Step 3: Try matching by color code in partId (e.g., PC54-WHT -> "wht" matches color code)
  if (prod.allColors) {
    for (const c of prod.allColors) {
      const colorName = (c.name || '').toLowerCase().trim();
      const colorCode = (c.code || '').toLowerCase().trim();
      if (colorName && !map[colorName] && colorCode) {
        // Search partId-to-URL map for partIds containing this color code
        for (const [pid, url] of Object.entries(partIdToUrl)) {
          if (pid.includes(colorCode) || pid.includes(colorName.replace(/\\s+/g, ''))) {
            map[colorName] = url;
            break;
          }
        }
      }
    }
  }

  return map;
}

// Store color-image maps globally so swatch clicks can find them
const productImageMaps = {};
const productDefaultImages = {};

function switchImage(productId, colorName) {
  const imgEl = document.getElementById('prod-img-' + productId);
  if (!imgEl) return;

  const map = productImageMaps[productId] || {};
  const colorLower = colorName.toLowerCase();

  // Try exact match, then partial match
  let url = map[colorLower];
  if (!url) {
    for (const [key, val] of Object.entries(map)) {
      if (key.includes(colorLower) || colorLower.includes(key)) {
        url = val;
        break;
      }
    }
  }

  if (url) {
    imgEl.src = url;
  }

  // Highlight active swatch
  const container = imgEl.closest('.product-card');
  if (container) {
    container.querySelectorAll('.swatch').forEach(s => s.classList.remove('active'));
    container.querySelectorAll('.swatch').forEach(s => {
      if (s.textContent.toLowerCase() === colorLower) s.classList.add('active');
    });
  }
}

function renderProductCard(prod) {
  const pid = (prod.productId || 'unknown').replace(/[^a-zA-Z0-9]/g, '_');

  // Build color -> image map
  const colorMap = buildColorImageMap(prod);
  productImageMaps[pid] = colorMap;

  // Pick default image: prefer first image, or match selected color
  let defaultImageUrl = '';
  if (prod.images && prod.images.length > 0) {
    defaultImageUrl = prod.images[0].url;
    // If a color filter is active, try to find a matching image
    if (selectedColor) {
      const colorLower = selectedColor.toLowerCase();
      for (const [key, val] of Object.entries(colorMap)) {
        if (key.includes(colorLower) || colorLower.includes(key)) {
          defaultImageUrl = val;
          break;
        }
      }
    }
  }
  productDefaultImages[pid] = defaultImageUrl;

  const mainImage = defaultImageUrl
    ? `<img id="prod-img-${pid}" src="${defaultImageUrl}" alt="${prod.productName || ''}" loading="lazy" />`
    : `<div class="no-image">&#128247;</div>`;

  // Sanmar.com link
  const sanmarUrl = `https://www.sanmar.com/search?text=${encodeURIComponent(prod.productId || '')}`;

  let priceTiersHtml = '';
  if (prod.priceTiers && prod.priceTiers.length > 1) {
    priceTiersHtml = '<div class="price-tiers">';
    for (const tier of prod.priceTiers) {
      const minQ = tier.minQty || '1';
      priceTiersHtml += `<span class="price-tier"><strong>$${tier.price}</strong> @ ${minQ}+</span>`;
    }
    priceTiersHtml += '</div>';
  }

  let swatchesHtml = '';
  if (prod.allColors && prod.allColors.length > 0) {
    swatchesHtml = '<div class="color-swatches">';
    for (const c of prod.allColors) {
      const isActive = selectedColor && c.name.toLowerCase().includes(selectedColor.toLowerCase());
      swatchesHtml += `<span class="swatch ${isActive ? 'active' : ''}" title="${c.name}" onclick="switchImage('${pid}', '${c.name.replace(/'/g, "\\'")}')">${c.name}</span>`;
    }
    swatchesHtml += '</div>';
  }

  let invHtml = '';
  const inv = prod.inventory;
  if (inv && inv.parts && inv.parts.length > 0) {
    const visibleWH = allWarehouses.filter(w => selectedWarehouses.has(w));

    let filteredParts = inv.parts;
    if (selectedColor) {
      filteredParts = filteredParts.filter(p =>
        (p.partColor || p.partDescription || '').toLowerCase().includes(selectedColor.toLowerCase())
      );
    }
    if (selectedSize) {
      filteredParts = filteredParts.filter(p =>
        (p.labelSize || p.partDescription || '').toLowerCase().includes(selectedSize.toLowerCase()) ||
        (p.partId || '').toLowerCase().includes(selectedSize.toLowerCase())
      );
    }

    if (filteredParts.length > 0 && visibleWH.length > 0) {
      invHtml = `<div class="inventory-section">
        <h3>&#128230; Warehouse Inventory <span style="font-weight:400;font-size:13px;color:var(--text-light)">(${filteredParts.length} SKUs)</span></h3>
        <div class="inv-table-wrap"><table class="inv-table"><thead><tr>
          <th>Part ID</th><th>Color</th><th>Size</th><th>Total Qty</th>`;

      for (const wh of visibleWH) {
        const isFav = HIGHLIGHT_WAREHOUSES.some(f => wh.toLowerCase().includes(f.toLowerCase()));
        invHtml += `<th class="${isFav ? 'fav-wh' : ''}">${isFav ? '&#11088; ' : ''}${wh}</th>`;
      }
      invHtml += `</tr></thead><tbody>`;

      for (const part of filteredParts) {
        const locMap = {};
        for (const loc of (part.locations || [])) {
          locMap[loc.name] = parseInt(loc.qty || 0);
        }

        const totalQty = parseInt(part.totalQty || 0);
        invHtml += `<tr>
          <td><strong>${part.partId || ''}</strong></td>
          <td>${part.partColor || ''}</td>
          <td>${part.labelSize || ''}</td>
          <td class="qty-val ${qtyClass(totalQty)}">${totalQty.toLocaleString()}</td>`;

        for (const wh of visibleWH) {
          const q = locMap[wh] || 0;
          const isFav = HIGHLIGHT_WAREHOUSES.some(f => wh.toLowerCase().includes(f.toLowerCase()));
          invHtml += `<td class="qty-val ${qtyClass(q)} ${isFav ? 'fav-wh' : ''}">${q.toLocaleString()}</td>`;
        }
        invHtml += `</tr>`;
      }

      invHtml += `</tbody></table></div></div>`;
    }
  }

  return `<div class="product-card">
    <div class="product-header">
      <div class="product-image">${mainImage}</div>
      <div class="product-info">
        <h2><a href="${sanmarUrl}" target="_blank" style="color:var(--primary);text-decoration:none;" title="View on SanMar.com">${prod.productName || prod.productId || 'Unknown Product'}</a></h2>
        <div class="product-style">
          <a href="${sanmarUrl}" target="_blank" style="color:var(--accent);text-decoration:none;">Style #${prod.productId || ''} &#8599;</a>
          ${prod.productBrand ? '&mdash; ' + prod.productBrand : ''}
        </div>
        <div class="product-desc">${prod.description || 'No description available.'}</div>
        ${prod.basePrice ? `<div class="price-block"><span class="price-main">$${prod.basePrice}</span><span class="price-label">per unit</span></div>` : ''}
        ${priceTiersHtml}
        ${prod.basePrice ? `<button class="add-quote-btn" data-qname="${(prod.productName||prod.productId||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;')}" data-qprice="${prod.basePrice}" onclick="addToQuote(this.dataset.qname,this.dataset.qprice)">&#128203; Add to Quote</button>` : ''}
        ${swatchesHtml}
      </div>
    </div>
    ${invHtml}
  </div>`;
}

function qtyClass(qty) {
  if (qty === 0) return 'qty-zero';
  if (qty < 24) return 'qty-low';
  if (qty < 100) return 'qty-med';
  return 'qty-high';
}

function toggleWarehouse(wh) {
  if (selectedWarehouses.has(wh)) {
    selectedWarehouses.delete(wh);
  } else {
    selectedWarehouses.add(wh);
  }
  renderProducts();
}

function toggleAllWarehouses() {
  // If any are selected, deselect all. If none selected, select all.
  if (selectedWarehouses.size > 0) {
    selectedWarehouses.clear();
  } else {
    allWarehouses.forEach(w => selectedWarehouses.add(w));
  }
  warehouseDefaultSet = true;
  renderProducts();
}

function filterBrand(brand) {
  selectedBrand = brand;
  renderProducts();
}

function filterCategory(category) {
  selectedCategory = category;
  renderProducts();
}

function filterGender(gender) {
  selectedGender = gender;
  renderProducts();
}

function filterColor(color) {
  selectedColor = color;
  // Update all visible product images to show the selected color
  if (color) {
    for (const pid of Object.keys(productImageMaps)) {
      switchImage(pid, color);
    }
  } else {
    // Reset all images to defaults
    for (const [pid, defaultUrl] of Object.entries(productDefaultImages)) {
      const imgEl = document.getElementById('prod-img-' + pid);
      if (imgEl && defaultUrl) imgEl.src = defaultUrl;
    }
  }
  renderProducts();
}

function filterSize(size) {
  selectedSize = size;
  renderProducts();
}

function setSort(val) {
  currentSort = val;
  renderProducts();
}

function toggleDebug() {
  debugMode = !debugMode;
  toast(debugMode ? 'Debug mode ON' : 'Debug mode OFF');
}

async function showDebugInfo() {
  const q = searchInput.value.trim();
  if (!q) return;
  window.open(`/api/debug/raw/product/${encodeURIComponent(q)}`, '_blank');
}

// ─── Catalog Builder ─────────────────────────────────────────────────────
async function buildCatalog() {
  const btn = document.getElementById('catalogBtn');
  btn.textContent = '⏳ building...';
  try {
    await fetch('/api/catalog/build', {method: 'POST'});
    pollCatalogStatus();
  } catch(err) {
    toast('Failed to start catalog build: ' + err.message, true);
    btn.textContent = '📦 build catalog';
  }
}

let catalogPollTimer = null;
async function pollCatalogStatus() {
  try {
    const resp = await fetch('/api/catalog/status');
    const data = await resp.json();
    const btn = document.getElementById('catalogBtn');
    const statusDiv = document.getElementById('catalogStatus');

    if (data.running) {
      const pct = data.total > 0 ? Math.round((data.progress / data.total) * 100) : 0;
      btn.textContent = `⏳ ${pct}% (${data.indexed}/${data.total})`;
      if (statusDiv) statusDiv.innerHTML = `<div style="background:var(--border);border-radius:6px;overflow:hidden;height:8px;max-width:300px;margin:10px auto;">` +
        `<div style="background:var(--accent);height:100%;width:${pct}%;transition:width 0.3s;"></div></div>` +
        `<div>${data.message}</div>`;
      catalogPollTimer = setTimeout(pollCatalogStatus, 2000);
    } else {
      if (data.catalog_exists) {
        btn.textContent = `📦 ${data.catalog_size} products indexed`;
        if (statusDiv) statusDiv.innerHTML = `<span style="color:var(--success);">&#9989; Catalog ready: ${data.catalog_size} products (built ${data.last_built || 'recently'})</span>`;
      } else {
        btn.textContent = '📦 build catalog';
        if (statusDiv) statusDiv.innerHTML = `<span style="color:var(--text-light);">Click <strong>"build catalog"</strong> in the header to index all SanMar products for full keyword search.</span>`;
      }
    }
  } catch(err) {
    console.error('Catalog poll error:', err);
  }
}

// Check catalog status on page load
setTimeout(pollCatalogStatus, 500);

async function runHealthCheck() {
  showLoading();
  try {
    const resp = await fetch('/api/healthcheck');
    const data = await resp.json();
    let html = '<div class="product-card" style="padding:24px;">';
    html += '<h2 style="margin-bottom:16px;">&#9889; API Health Check</h2>';
    html += '<table class="inv-table" style="width:auto;"><thead><tr><th>Service</th><th>Status</th><th>Details</th></tr></thead><tbody>';
    for (const [name, info] of Object.entries(data)) {
      const icon = info.ok ? '&#9989;' : '&#10060;';
      html += `<tr><td><strong>${name}</strong></td><td>${icon}</td><td>${info.message || 'Unknown'}</td></tr>`;
    }
    html += '</tbody></table>';
    html += '<p style="margin-top:16px;color:var(--text-light);font-size:13px;">If auth fails, try editing CONFIG in sanmar_search.py — some SanMar accounts use customer ID (140741) as the username.</p>';
    html += '</div>';
    document.getElementById('content').innerHTML = html;
  } catch(err) {
    showError('Health check failed: ' + err.message);
  }
}

function toast(msg, isError) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isError ? ' error' : '');
  setTimeout(() => { el.className = 'toast'; }, 3000);
}

// ─── Catalog Age Reminder ─────────────────────────────────────────────────────
const REMINDER_DISMISS_KEY = '4zd_catalog_dismissed';
const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;

async function checkCatalogAge() {
  try {
    const resp = await fetch('/api/catalog/status');
    const data = await resp.json();
    if (!data.last_built) return; // catalog never built, build button visible already

    const builtAt   = new Date(data.last_built);
    const ageMs     = Date.now() - builtAt.getTime();
    if (ageMs < THIRTY_DAYS_MS) return; // fresh enough

    // Check if user dismissed recently
    const dismissed = localStorage.getItem(REMINDER_DISMISS_KEY);
    if (dismissed && (Date.now() - parseInt(dismissed)) < THIRTY_DAYS_MS) return;

    // Show the reminder
    document.getElementById('catalog-reminder').classList.add('show');
  } catch(e) { /* silently ignore if API unreachable */ }
}

function dismissCatalogReminder() {
  localStorage.setItem(REMINDER_DISMISS_KEY, Date.now().toString());
  document.getElementById('catalog-reminder').classList.remove('show');
}

window.addEventListener('load', checkCatalogAge);

// ─── Quote Builder — Modal open/close ────────────────────────────────────────
function qtOpen() {
  document.getElementById('qt-overlay').classList.add('open');
}
function qtClose() {
  document.getElementById('qt-overlay').classList.remove('open');
}

// ─── Quote Builder — addToQuote entry point ───────────────────────────────────
function addToQuote(name, price) {
  document.getElementById('qt-garment-desc').value  = name;
  document.getElementById('qt-apparel-cost').value  = parseFloat(price).toFixed(2);
  qtClearResults(); qtOnQtyChange();
  qtOpen();
  setTimeout(() => document.getElementById('qt-qty').focus(), 350);
}

// ─── Quote Builder — Pricing data ────────────────────────────────────────────
const QT_IPU_RATE = 0.10;
function qtGetMargin(qty) {
  if (qty < 36)   return 1.00;
  if (qty <= 60)  return 0.85;
  if (qty <= 144) return 0.65;
  if (qty <= 249) return 0.55;
  return 0.40;
}
function qtMarginLabel(qty) { return (qtGetMargin(qty)*100).toFixed(0)+'%'; }

const QT_HT_SIZES = [
  '1.5\u2033 \xd7 1.5\u2033','2.5\u2033 \xd7 2.5\u2033','4\u2033 \xd7 4\u2033',
  '5.8\u2033 \xd7 8.3\u2033','11.7\u2033 \xd7 4.25\u2033','16.5\u2033 \xd7 5.85\u2033',
  '8.3\u2033 \xd7 11.7\u2033','11.7\u2033 \xd7 11.7\u2033','11.7\u2033 \xd7 16.5\u2033'
];
const QT_HT_TIERS = [
  { from:10,  to:19,  prices:[2.62,2.95,3.23,4.28,4.28,5.12,5.12,6.43,7.74] },
  { from:20,  to:49,  prices:[2.18,2.45,2.69,3.57,3.57,4.27,4.27,5.37,6.46] },
  { from:50,  to:99,  prices:[1.30,1.62,1.96,2.56,2.56,3.30,3.30,4.24,5.20] },
  { from:100, to:199, prices:[0.87,1.11,1.45,1.77,1.77,2.27,2.27,2.95,3.63] },
  { from:200, to:299, prices:[0.70,0.93,1.26,1.49,1.49,1.86,1.86,2.43,3.02] },
  { from:300, to:499, prices:[0.56,0.78,1.10,1.33,1.33,1.71,1.71,2.24,2.78] },
];
const QT_EMB_TIERS = [
  { from:1,   to:5,   price:7.00 },
  { from:6,   to:23,  price:5.50 },
  { from:24,  to:35,  price:5.25 },
  { from:36,  to:71,  price:4.75 },
  { from:72,  to:143, price:4.25 },
  { from:144, to:500, price:3.25 },
];
const QT_SP_TIERS = [
  { from:12,  to:36,  factor:30, base:[2.85,2.95,3.10,3.30,3.50,3.65] },
  { from:37,  to:60,  factor:30, base:[2.35,2.45,2.60,2.70,2.80,2.90] },
  { from:61,  to:144, factor:30, base:[2.00,2.05,2.20,2.40,2.55,2.65] },
  { from:145, to:249, factor:30, base:[1.80,1.90,2.05,2.15,2.35,2.55] },
  { from:250, to:600, factor:34, base:[1.20,1.25,1.45,1.50,1.65,2.50] },
];
function qtHtPrice(qty, si)  { const t = QT_HT_TIERS.find(r => qty >= r.from && qty <= r.to); return t ? t.prices[si] : null; }
function qtEmbPrice(qty)     { const t = QT_EMB_TIERS.find(r => qty >= r.from && qty <= r.to); return t ? t.price : null; }
function qtSpPrice(qty, ci)  { const t = QT_SP_TIERS.find(r => qty >= r.from && qty <= r.to); if (!t) return null; return t.base[ci] + t.factor*(ci+1)/qty; }

// ─── Quote Builder — Location panels ─────────────────────────────────────────
const QT_LOC_KEYS   = ['front','back','rsleeve','lsleeve'];
const QT_LOC_LABELS = { front:'Side 1 — Front', back:'Side 2 — Back', rsleeve:'Right Sleeve', lsleeve:'Left Sleeve' };
const qtLocState    = { front:'none', back:'none', rsleeve:'none', lsleeve:'none' };

const QT_HT_OPTS = QT_HT_SIZES.map((s,i) => `<option value="${i}">${s}</option>`).join('');
const QT_SP_OPTS = [1,2,3,4,5,6].map((c,i) => `<option value="${i}">${c} Color${c>1?'s':''}</option>`).join('');

function qtBuildPanel(key) {
  return `
  <div class="qt-loc-panel" id="qt-panel-${key}">
    <span class="qt-loc-name">${QT_LOC_LABELS[key]}</span>
    <div class="qt-loc-pills">
      <button class="qt-loc-pill active-none" id="qt-pill-${key}-none"       onclick="qtSetLocType('${key}','none')">None</button>
      <button class="qt-loc-pill"             id="qt-pill-${key}-heat"       onclick="qtSetLocType('${key}','heat')">&#128293; Heat Transfer</button>
      <button class="qt-loc-pill"             id="qt-pill-${key}-embroidery" onclick="qtSetLocType('${key}','embroidery')">&#129525; Embroidery</button>
      <button class="qt-loc-pill"             id="qt-pill-${key}-screen"     onclick="qtSetLocType('${key}','screen')">&#128424; Screen Print</button>
    </div>
    <div class="qt-loc-opts hidden" id="qt-opts-${key}-heat">
      <div class="qt-form-group">
        <label>Transfer Size</label>
        <select class="qt-select" id="qt-${key}-ht-size" onchange="qtClearResults()">
          <option value="">— Select size —</option>${QT_HT_OPTS}
        </select>
      </div>
    </div>
    <div class="qt-loc-opts hidden" id="qt-opts-${key}-screen">
      <div class="qt-form-group">
        <label>Number of Colors</label>
        <select class="qt-select" id="qt-${key}-sp-colors" onchange="qtClearResults()">
          <option value="">— Select —</option>${QT_SP_OPTS}
        </select>
        <div class="qt-hint">Setup cost split across qty.</div>
      </div>
    </div>
  </div>`;
}

(function qtInit() {
  const grid  = document.getElementById('qt-loc-grid');
  const qname = document.getElementById('qt-quote-name');
  if (!grid || !qname) { setTimeout(qtInit, 30); return; }
  grid.innerHTML = QT_LOC_KEYS.map(qtBuildPanel).join('');
  qname.addEventListener('keydown', e => { if (e.key === 'Enter') qtSaveQuote(); });
})();

function qtSetLocType(key, type) {
  qtLocState[key] = type;
  ['none','heat','embroidery','screen'].forEach(t => {
    const pill = document.getElementById(`qt-pill-${key}-${t}`);
    pill.classList.remove('active-none','active-type');
    if (t === type) pill.classList.add(type === 'none' ? 'active-none' : 'active-type');
  });
  const panel = document.getElementById(`qt-panel-${key}`);
  panel.classList.toggle('active-panel', type !== 'none');
  ['heat','screen'].forEach(t => {
    const el = document.getElementById(`qt-opts-${key}-${t}`);
    if (el) el.classList.toggle('hidden', type !== t);
  });
  qtClearResults();
}

// ─── Quote Builder — Qty / margin ────────────────────────────────────────────
function qtOnQtyChange() {
  const qty  = parseInt(document.getElementById('qt-qty').value);
  const hint = document.getElementById('qt-margin-hint');
  hint.textContent = (!isNaN(qty) && qty > 0) ? 'Margin rate: ' + qtMarginLabel(qty) : '';
  qtClearResults();
}

// ─── Quote Builder — Calculate ────────────────────────────────────────────────
const qtFmt  = n => '$' + n.toFixed(2);
const qtFmtK = n => n >= 1000 ? '$' + (n/1000).toFixed(1) + 'K' : '$' + n.toFixed(2);

function qtShowAlert(type, msg) { const el = document.getElementById('qt-alert-'+type); el.textContent = msg; el.classList.add('show'); }
function qtClearAlerts() { ['warn','err'].forEach(t => document.getElementById('qt-alert-'+t).classList.remove('show')); }
function qtClearResults() { document.getElementById('qt-results-section').style.display = 'none'; }

function qtCalculate() {
  qtClearAlerts();
  const apparelCost = parseFloat(document.getElementById('qt-apparel-cost').value);
  const qty         = parseInt(document.getElementById('qt-qty').value);
  const clientName  = document.getElementById('qt-client-name').value.trim() || '[Client Name]';
  const garmentDesc = document.getElementById('qt-garment-desc').value.trim() || '[Garment]';

  if (isNaN(apparelCost) || apparelCost < 0) { qtShowAlert('err','Enter a valid apparel cost.'); return; }
  if (isNaN(qty) || qty < 1)                 { qtShowAlert('err','Enter a valid quantity.'); return; }

  const active = [];
  for (const key of QT_LOC_KEYS) {
    const type = qtLocState[key];
    if (type === 'none') continue;
    let price = null, label = '';
    if (type === 'heat') {
      const val = document.getElementById(`qt-${key}-ht-size`).value;
      if (val === '') { qtShowAlert('err', `Select a transfer size for ${QT_LOC_LABELS[key]}.`); return; }
      const si = parseInt(val);
      if (qty < 10 || qty > 499) { qtShowAlert('err', `Heat Transfer qty must be 10–499.`); return; }
      price = qtHtPrice(qty, si);
      if (price === null) { qtShowAlert('err', `No Heat Transfer pricing for qty ${qty}.`); return; }
      label = `Heat Transfer — ${QT_HT_SIZES[si]}`;
    } else if (type === 'embroidery') {
      if (qty > 500) { qtShowAlert('err', `Embroidery max is 500.`); return; }
      price = qtEmbPrice(qty);
      if (price === null) { qtShowAlert('err', `No embroidery pricing for qty ${qty}.`); return; }
      label = 'Embroidery (6,000 stitches)';
    } else if (type === 'screen') {
      const val = document.getElementById(`qt-${key}-sp-colors`).value;
      if (val === '') { qtShowAlert('err', `Select number of colors for ${QT_LOC_LABELS[key]}.`); return; }
      const ci = parseInt(val);
      if (qty < 12 || qty > 600) { qtShowAlert('err', `Screen Printing qty must be 12–600.`); return; }
      price = qtSpPrice(qty, ci);
      if (price === null) { qtShowAlert('err', `No Screen Printing pricing for qty ${qty}.`); return; }
      label = `Screen Printing — ${ci+1} color${ci>0?'s':''}`;
    }
    active.push({ key, locLabel: QT_LOC_LABELS[key], price, label });
  }

  if (active.length === 0) { qtShowAlert('err', 'Select at least one decoration location.'); return; }

  const totalPrintCost = active.reduce((s, a) => s + a.price, 0);
  const margin         = qtGetMargin(qty);
  const subTotal       = totalPrintCost + apparelCost;
  const totalUnit      = subTotal * (1 + margin);
  const marginDollars  = (totalUnit - subTotal) * qty;
  const ipuPerUnit     = totalUnit * QT_IPU_RATE;
  const ipuTotal       = ipuPerUnit * qty;
  const profit         = marginDollars - ipuTotal;
  const totalOrder     = totalUnit * qty;

  document.getElementById('qt-r-total-unit').textContent  = qtFmt(totalUnit);
  document.getElementById('qt-r-total-order').textContent = qtFmtK(totalOrder);
  document.getElementById('qt-r-profit').textContent      = qtFmtK(profit);
  document.getElementById('qt-r-apparel').textContent     = qtFmt(apparelCost);
  document.getElementById('qt-r-print-total').textContent = qtFmt(totalPrintCost);
  document.getElementById('qt-r-subtotal').textContent    = qtFmt(subTotal);
  document.getElementById('qt-r-margin-rate').textContent = qtMarginLabel(qty);
  document.getElementById('qt-r-margin').textContent      = qtFmtK(marginDollars);
  document.getElementById('qt-r-ipu-total').textContent   = qtFmtK(ipuTotal);
  document.getElementById('qt-r-profit2').textContent     = qtFmtK(profit);

  document.getElementById('qt-loc-breakdown').innerHTML = active.map(a =>
    `<div class="qt-loc-line"><span class="lk">${a.locLabel}</span><span class="lv">${a.label} — ${qtFmt(a.price)}/unit</span></div>`
  ).join('');

  const result = { totalUnit, totalOrder, subTotal, marginDollars, ipuTotal, profit, qty, apparelCost, clientName, garmentDesc, active };
  window._qtLastResult = result;
  window._qtQuoteHtml  = qtBuildQuoteHtml(result);
  qtRenderQuotePreview(window._qtQuoteHtml);
  document.getElementById('qt-results-section').style.display = 'block';
  setTimeout(() => document.getElementById('qt-results-section').scrollIntoView({ behavior:'smooth', block:'start' }), 50);
}

// ─── Quote Builder — HTML quote generation ────────────────────────────────────
function qtEscHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function qtBuildQuoteHtml(r) {
  const today = new Date().toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'});
  const decorRows = r.active.map(a =>
    `<tr><td style="padding:3px 4px;color:#666666;width:175px;vertical-align:top;">&#8203;</td><td style="padding:3px 4px;font-size:13px;color:#555;">${qtEscHtml(a.locLabel)}: ${qtEscHtml(a.label)}</td></tr>`
  ).join('');
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;color:#222;background:#fff;font-size:15px;line-height:1.6;">
<div style="max-width:560px;margin:0 auto;">
<p style="margin:0 0 16px">Hi ${qtEscHtml(r.clientName)},</p>
<p style="margin:0 0 16px">Thanks for reaching out—here's a first pass on your quote:</p>
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:2px solid #1a2744;border-bottom:2px solid #1a2744;margin:20px 0;">
  <tr><td colspan="2" style="padding:14px 4px 10px;font-weight:700;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#1a2744;">QUOTE SUMMARY</td></tr>
  <tr><td style="padding:5px 4px;color:#666;width:175px;">Garment:</td><td style="padding:5px 4px;font-weight:600;">${qtEscHtml(r.garmentDesc)}</td></tr>
  <tr><td style="padding:5px 4px;color:#666;vertical-align:top;">Decoration:</td><td style="padding:5px 4px;font-weight:600;">${r.active.length===1?qtEscHtml(r.active[0].label):''}</td></tr>
  ${r.active.length>1?decorRows:''}
  <tr><td style="padding:5px 4px;color:#666;">Quantity:</td><td style="padding:5px 4px;font-weight:600;">${r.qty} pieces</td></tr>
  <tr><td style="padding:5px 4px;color:#666;">Price per garment:</td><td style="padding:5px 4px;font-weight:600;">${qtFmt(r.totalUnit)}</td></tr>
  <tr><td style="padding:14px 4px 10px;color:#444;font-size:15px;">Total:</td><td style="padding:14px 4px 10px;font-weight:700;font-size:20px;color:#e8701a;">${qtFmt(r.totalOrder)}</td></tr>
</table>
<p style="margin:0 0 14px;color:#444;">This includes the decoration and is based on the quantity above.</p>
<p style="margin:0 0 14px;color:#444;">Shipping and sales tax are not included and will be added once we finalize details.</p>
<p style="margin:0 0 24px;color:#444;">If you want to tweak garment options, sizing, or quantities, I can adjust this quickly. Just let me know what direction you want to go.</p>
<p style="margin:0;color:#222;line-height:1.9;">— Marc<br><strong>4Z Design</strong><br>
<a href="mailto:marc@4zdesign.com" style="color:#e8701a;text-decoration:none;">marc@4zdesign.com</a></p>
</div></body></html>`;
}

function qtRenderQuotePreview(html) {
  const iframe = document.getElementById('qt-quote-preview');
  iframe.srcdoc = html;
  iframe.onload = () => { try { iframe.style.height = (iframe.contentDocument.body.scrollHeight + 32) + 'px'; } catch(e){} };
}

// ─── Quote Builder — Copy / Edit ─────────────────────────────────────────────
function qtFlash() { const f = document.getElementById('qt-copy-flash'); f.classList.add('show'); setTimeout(()=>f.classList.remove('show'),2000); }
function qtOpenEmail() {
  const r = window._qtLastResult; if (!r) return;
  const subject = encodeURIComponent('Quote — ' + r.garmentDesc + ' x' + r.qty);
  const body    = encodeURIComponent(
    'Hi ' + r.clientName + ',\n\n' +
    'Thanks for reaching out\u2014here\u2019s a first pass on your quote:\n\n' +
    '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n' +
    'QUOTE SUMMARY\n' +
    '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n' +
    'Garment:           ' + r.garmentDesc + '\n' +
    'Decoration:\n' + r.active.map(a => '  ' + a.locLabel + ': ' + a.label).join('\n') + '\n' +
    'Quantity:          ' + r.qty + ' pieces\n' +
    'Price per garment: ' + qtFmt(r.totalUnit) + '\n' +
    'Total:             ' + qtFmt(r.totalOrder) + '\n' +
    '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n' +
    'This includes the decoration and is based on the quantity above.\n\n' +
    'Shipping and sales tax are not included and will be added once we finalize details.\n\n' +
    'If you want to tweak garment options, sizing, or quantities, I can adjust this quickly. Just let me know what direction you want to go.\n\n' +
    '\u2014 Marc\n4Z Design\nmarc@4zdesign.com'
  );
  window.location.href = 'mailto:?subject=' + subject + '&body=' + body;
}
function qtCopyPlainText() {
  const r = window._qtLastResult; if (!r) return;
  const text = `Hi ${r.clientName},\n\nThanks for reaching out—here's a first pass on your quote:\n\n──────────────────────────────\nQUOTE SUMMARY\n──────────────────────────────\nGarment:           ${r.garmentDesc}\nDecoration:\n${r.active.map(a=>'  '+a.locLabel+': '+a.label).join('\n')}\nQuantity:          ${r.qty} pieces\nPrice per garment: ${qtFmt(r.totalUnit)}\nTotal:             ${qtFmt(r.totalOrder)}\n──────────────────────────────\n\nThis includes the decoration and is based on the quantity above.\n\nShipping and sales tax are not included and will be added once we finalize details.\n\nIf you want to tweak garment options, sizing, or quantities, I can adjust this quickly. Just let me know what direction you want to go.\n\n— Marc\n4Z Design\nmarc@4zdesign.com`;
  navigator.clipboard.writeText(text).then(qtFlash);
}
function qtOpenEditModal()  { document.getElementById('qt-edit-html-area').value = window._qtQuoteHtml||''; document.getElementById('qt-edit-modal').classList.add('open'); }
function qtApplyEditedHtml(){ window._qtQuoteHtml = document.getElementById('qt-edit-html-area').value; qtRenderQuotePreview(window._qtQuoteHtml); qtCloseModal('qt-edit-modal'); }

// ─── Quote Builder — Reset ────────────────────────────────────────────────────
function qtResetAll() {
  QT_LOC_KEYS.forEach(k => qtSetLocType(k,'none'));
  ['qt-apparel-cost','qt-qty','qt-client-name','qt-garment-desc'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('qt-margin-hint').textContent = '';
  qtClearResults(); qtClearAlerts();
  window._qtLastResult = null; window._qtQuoteHtml = '';
}

// ─── Quote Builder — Save / Load ─────────────────────────────────────────────
const QT_STORAGE_KEY = '4zd_quotes_v3';
const qtLoadAll = () => { try { return JSON.parse(localStorage.getItem(QT_STORAGE_KEY))||[]; } catch { return []; } };
const qtSaveAll = q => localStorage.setItem(QT_STORAGE_KEY, JSON.stringify(q));

function qtOpenSaveModal() {
  if (!window._qtLastResult) { alert('Run a calculation first.'); return; }
  document.getElementById('qt-quote-name').value = window._qtLastResult.clientName !== '[Client Name]' ? window._qtLastResult.clientName : '';
  document.getElementById('qt-save-modal').classList.add('open');
  setTimeout(() => document.getElementById('qt-quote-name').focus(), 100);
}
function qtSaveQuote() {
  const name = document.getElementById('qt-quote-name').value.trim();
  if (!name) { document.getElementById('qt-quote-name').style.borderColor='#ef4444'; return; }
  const r = window._qtLastResult;
  const entry = {
    id: Date.now(), name,
    date: new Date().toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}),
    quoteHtml: window._qtQuoteHtml, result: r,
    formState: {
      clientName: r.clientName, garmentDesc: r.garmentDesc,
      apparelCost: r.apparelCost, qty: r.qty,
      locState: {...qtLocState},
      locSelects: Object.fromEntries(QT_LOC_KEYS.flatMap(k => [
        [`qt-${k}-ht-size`,   document.getElementById(`qt-${k}-ht-size`).value],
        [`qt-${k}-sp-colors`, document.getElementById(`qt-${k}-sp-colors`).value],
      ]))
    }
  };
  const all = qtLoadAll(); all.unshift(entry); qtSaveAll(all);
  qtCloseModal('qt-save-modal');
  toast('Quote saved!');
}
function qtOpenLoadModal()  { qtRenderQuoteList(); document.getElementById('qt-load-modal').classList.add('open'); }
function qtRenderQuoteList() {
  const all = qtLoadAll();
  const c   = document.getElementById('qt-quote-list');
  if (!all.length) { c.innerHTML='<div class="qt-empty-state">No saved quotes yet.</div>'; return; }
  c.innerHTML = all.map(q => `
    <div class="qt-quote-item" onclick="qtLoadQuote(${q.id})">
      <div>
        <div class="qt-qi-name">${qtEscHtml(q.name)}</div>
        <div class="qt-qi-meta">${q.date} &middot; ${q.formState.qty} pcs &middot; ${qtFmt(q.result.totalUnit)}/ea</div>
      </div>
      <button class="qt-qi-del" onclick="qtDeleteQuote(event,${q.id})">&#128465;</button>
    </div>`).join('');
}
function qtLoadQuote(id) {
  const q = qtLoadAll().find(x => x.id === id); if (!q) return;
  qtResetAll();
  const fs = q.formState;
  document.getElementById('qt-client-name').value  = fs.clientName||'';
  document.getElementById('qt-garment-desc').value  = fs.garmentDesc||'';
  document.getElementById('qt-apparel-cost').value  = fs.apparelCost;
  document.getElementById('qt-qty').value           = fs.qty;
  qtOnQtyChange();
  QT_LOC_KEYS.forEach(k => {
    if (fs.locState && fs.locState[k]) qtSetLocType(k, fs.locState[k]);
    if (fs.locSelects) {
      const htEl = document.getElementById(`qt-${k}-ht-size`);
      const spEl = document.getElementById(`qt-${k}-sp-colors`);
      if (htEl && fs.locSelects[`qt-${k}-ht-size`])   htEl.value = fs.locSelects[`qt-${k}-ht-size`];
      if (spEl && fs.locSelects[`qt-${k}-sp-colors`])  spEl.value = fs.locSelects[`qt-${k}-sp-colors`];
    }
  });
  qtCloseModal('qt-load-modal');
  setTimeout(() => {
    qtCalculate();
    if (q.quoteHtml) { window._qtQuoteHtml = q.quoteHtml; qtRenderQuotePreview(q.quoteHtml); }
  }, 80);
}
function qtDeleteQuote(e, id) {
  e.stopPropagation();
  if (!confirm('Delete this quote?')) return;
  qtSaveAll(qtLoadAll().filter(x => x.id !== id)); qtRenderQuoteList();
}
function qtCloseModal(id) { document.getElementById(id).classList.remove('open'); }
</script>

<!-- ── Catalog Reminder Banner ────────────────────────────── -->
<div class="catalog-reminder" id="catalog-reminder">
  <strong>&#9888;&#65039; Catalog may be outdated</strong>
  Your product catalog hasn't been rebuilt in over 30 days. New SanMar products may be missing.
  <div class="catalog-reminder-actions">
    <button class="cr-btn-rebuild" onclick="buildCatalog();dismissCatalogReminder()">Rebuild Now</button>
    <button class="cr-btn-dismiss" onclick="dismissCatalogReminder()">Remind Later</button>
  </div>
</div>

<!-- ── Quote Builder FAB ─────────────────────────────────── -->
<button class="quote-tool-fab" onclick="qtOpen()">&#128203; Quote Builder</button>

<!-- ── Quote Builder Modal ───────────────────────────────── -->
<div class="qt-overlay" id="qt-overlay" onclick="if(event.target===this)qtClose()">
<div class="qt-drawer" id="qt-drawer">
  <div class="qt-drawer-header">
    <div class="qt-drawer-title">4Z<span>Design</span> &nbsp;Quote Builder</div>
    <div class="qt-drawer-actions">
      <button class="qt-hdr-btn" onclick="qtOpenLoadModal()">&#128194; Saved</button>
      <button class="qt-hdr-btn" onclick="qtResetAll()">+ New</button>
      <button class="qt-close-btn" onclick="qtClose()" title="Close">&times;</button>
    </div>
  </div>

  <div class="qt-body">

    <!-- Step 1 -->
    <div class="qt-card">
      <div class="qt-card-title">Step 1 — Order Details</div>
      <div class="qt-form-row">
        <div class="qt-form-group">
          <label for="qt-client-name">Client Name</label>
          <input class="qt-input" type="text" id="qt-client-name" placeholder="e.g. Acme Corp" oninput="qtClearResults()">
        </div>
        <div class="qt-form-group">
          <label for="qt-garment-desc">Garment</label>
          <input class="qt-input" type="text" id="qt-garment-desc" placeholder="e.g. Cotton T-Shirt" oninput="qtClearResults()">
        </div>
      </div>
      <div class="qt-form-row">
        <div class="qt-form-group">
          <label for="qt-apparel-cost">Apparel Cost / Piece ($)</label>
          <input class="qt-input" type="number" id="qt-apparel-cost" min="0" step="0.01" placeholder="e.g. 9.00" oninput="qtClearResults()">
        </div>
        <div class="qt-form-group">
          <label for="qt-qty">Quantity</label>
          <input class="qt-input" type="number" id="qt-qty" min="1" step="1" placeholder="e.g. 50" oninput="qtOnQtyChange()">
          <div class="qt-hint" id="qt-margin-hint"></div>
        </div>
      </div>
    </div>

    <!-- Step 2 -->
    <div class="qt-card">
      <div class="qt-card-title">Step 2 — Decorations <span style="font-weight:400;text-transform:none;letter-spacing:0;color:#b0bec5;font-size:.75rem;">Select a type for each location</span></div>
      <div class="qt-loc-grid" id="qt-loc-grid"></div>
    </div>

    <div class="qt-alert warn" id="qt-alert-warn"></div>
    <div class="qt-alert err"  id="qt-alert-err"></div>

    <button class="qt-calc-btn" onclick="qtCalculate()">Calculate Quote &#8594;</button>

    <!-- Results -->
    <div id="qt-results-section" style="margin-top:16px;">
      <div class="qt-results-card">
        <div class="qt-card-title">Your Numbers</div>
        <div class="qt-results-primary">
          <div class="qt-res-big"><div class="val" id="qt-r-total-unit">—</div><div class="lbl">Per Unit</div></div>
          <div class="qt-res-big"><div class="val" id="qt-r-total-order">—</div><div class="lbl">Total Order</div></div>
          <div class="qt-res-big"><div class="val" id="qt-r-profit">—</div><div class="lbl">Net Profit</div></div>
        </div>
        <div class="qt-loc-breakdown" id="qt-loc-breakdown"></div>
        <div class="qt-results-secondary">
          <div class="qt-res-row"><span class="rk">Apparel Cost</span><span class="rv" id="qt-r-apparel">—</span></div>
          <div class="qt-res-row"><span class="rk">Total Decoration</span><span class="rv" id="qt-r-print-total">—</span></div>
          <div class="qt-res-row"><span class="rk">Sub Total / unit</span><span class="rv" id="qt-r-subtotal">—</span></div>
          <div class="qt-res-row"><span class="rk">Margin Rate</span><span class="rv" id="qt-r-margin-rate">—</span></div>
          <div class="qt-res-row"><span class="rk">Gross Margin $</span><span class="rv" id="qt-r-margin">—</span></div>
          <div class="qt-res-row"><span class="rk">IPU Cost (10%)</span><span class="rv" id="qt-r-ipu-total">—</span></div>
          <div class="qt-res-row highlight span2"><span class="rk">Profit After IPU</span><span class="rv" id="qt-r-profit2">—</span></div>
        </div>
      </div>

      <div class="qt-card">
        <div class="qt-quote-header">
          <div class="qt-card-title" style="margin:0;">Client Quote</div>
          <div style="display:flex;align-items:center;gap:8px;">
            <span class="qt-copy-flash" id="qt-copy-flash">Copied!</span>
            <button class="qt-btn-outline" style="flex:none;padding:6px 12px;font-size:.75rem;" onclick="qtOpenEditModal()">&#9999;&#65039; Edit HTML</button>
          </div>
        </div>
        <iframe id="qt-quote-preview" title="Quote Preview" sandbox="allow-same-origin"></iframe>
        <div class="qt-quote-actions">
          <button class="qt-btn-outline" onclick="qtOpenEmail()">&#128231; Open in Email</button>
          <button class="qt-btn-outline" onclick="qtCopyPlainText()">&#128196; Copy Text</button>
          <button class="qt-btn-solid"   onclick="qtOpenSaveModal()">&#128190; Save Quote</button>
        </div>
      </div>
    </div>

  </div><!-- /qt-body -->
</div><!-- /qt-drawer -->
</div><!-- /qt-overlay -->

<!-- Quote Builder Modals (z-index 1000, above modal) -->
<div class="qt-overlay-modal" id="qt-edit-modal">
  <div class="qt-modal" style="max-width:640px;">
    <div class="qt-modal-title">Edit Quote HTML</div>
    <div class="qt-modal-sub">Modify then click Apply.</div>
    <textarea id="qt-edit-html-area"></textarea>
    <div class="qt-modal-actions">
      <button class="qt-btn-outline" onclick="qtCloseModal('qt-edit-modal')">Cancel</button>
      <button class="qt-btn-solid"   onclick="qtApplyEditedHtml()">Apply</button>
    </div>
  </div>
</div>

<div class="qt-overlay-modal" id="qt-save-modal">
  <div class="qt-modal">
    <div class="qt-modal-title">Save This Quote</div>
    <div class="qt-modal-sub">Name it so you can pull it back up later.</div>
    <div class="qt-form-group">
      <label for="qt-quote-name">Quote Name</label>
      <input class="qt-input" type="text" id="qt-quote-name" placeholder="e.g. Acme Corp — 50 Polos">
    </div>
    <div class="qt-modal-actions">
      <button class="qt-btn-outline" onclick="qtCloseModal('qt-save-modal')">Cancel</button>
      <button class="qt-btn-solid"   onclick="qtSaveQuote()">Save</button>
    </div>
  </div>
</div>

<div class="qt-overlay-modal" id="qt-load-modal">
  <div class="qt-modal">
    <div class="qt-modal-title">Saved Quotes</div>
    <div class="qt-modal-sub">Click a quote to restore it.</div>
    <div class="qt-quote-list" id="qt-quote-list"></div>
    <div class="qt-modal-actions">
      <button class="qt-btn-outline" onclick="qtCloseModal('qt-load-modal')" style="flex:none;padding:9px 22px;">Close</button>
    </div>
  </div>
</div>

</body>
</html>"""


# ─── Main ────────────────────────────────────────────────────────────────────
# ─── Scheduled Catalog Rebuild ───────────────────────────────────────────────
def scheduled_catalog_rebuild():
    """Run at 1am every Monday. Skip if a build is already in progress."""
    if _index_status.get('running'):
        print("[SCHEDULER] Catalog build already running — skipping scheduled rebuild.")
        return
    print("[SCHEDULER] Starting scheduled weekly catalog rebuild...")
    thread = threading.Thread(target=build_catalog_background, daemon=True)
    thread.start()

# Only start the scheduler in the main process (not in gunicorn worker reloads)
if not os.environ.get('WERKZEUG_RUN_MAIN'):
    _scheduler = BackgroundScheduler(timezone='America/Los_Angeles')
    _scheduler.add_job(
        scheduled_catalog_rebuild,
        CronTrigger(day_of_week='mon', hour=1, minute=0),
        id='weekly_catalog_rebuild',
        replace_existing=True,
    )
    _scheduler.start()
    print("[SCHEDULER] Weekly catalog rebuild scheduled — Mondays at 1:00 AM PT.")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print("=" * 60)
    print("  SanMar Product Search — Warehouse Edition")
    print(f"  Open http://localhost:{port} in your browser")
    print("=" * 60)
    print(f"  User: {CONFIG['username']}")
    print(f"  Customer ID: {CONFIG['customer_id']}")
    print(f"  Favorite Warehouses: {', '.join(CONFIG['favorite_warehouses'])}")
    print("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=False)
