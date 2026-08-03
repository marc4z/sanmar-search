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
from flask import Flask, request, jsonify, render_template_string, Response
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import datetime

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


@app.route('/api/image_proxy')
def api_image_proxy():
    """Fetch a SanMar product image server-side so the browser can embed it in the
    quote PDF without hitting a CORS wall (used as the default garment photo)."""
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'Missing url parameter'}), 400
    try:
        parsed = urlparse(url)
    except Exception:
        return jsonify({'error': 'Invalid url'}), 400
    host = (parsed.hostname or '').lower()
    # SanMar serves product media from a few different subdomains/CDNs (not all under
    # sanmar.com), so we allow anything with "sanmar" in the host rather than an exact
    # domain match — still far narrower than an open proxy.
    if parsed.scheme not in ('http', 'https') or 'sanmar' not in host:
        return jsonify({'error': 'URL host not allowed'}), 400
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        return jsonify({'error': f'Failed to fetch image: {e}'}), 502
    content_type = resp.headers.get('Content-Type', 'image/jpeg')
    return Response(resp.content, mimetype=content_type)


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
  .qt-quote-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
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


  /* ── Settings Panel ───────────────────────────────────────── */
  .qt-settings-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.55); z-index: 1000;
    align-items: center; justify-content: center;
  }
  .qt-settings-overlay.open { display: flex; }
  .qt-settings-box {
    background: #fff; border-radius: 18px; width: 90vw; max-width: 860px;
    max-height: 88vh; display: flex; flex-direction: column;
    box-shadow: 0 24px 64px rgba(0,0,0,.35);
    animation: qtFadeIn .2s ease;
  }
  .qt-settings-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 18px 24px 14px; border-bottom: 1.5px solid #e2e8f0;
    flex-shrink: 0;
  }
  .qt-settings-title { font-size: 1.1rem; font-weight: 800; color: #1a2744; }
  .qt-settings-body { flex: 1; overflow-y: auto; padding: 0; }
  .qt-stab-bar {
    display: flex; gap: 0; border-bottom: 2px solid #e2e8f0;
    padding: 0 24px; background: #f8fafc; flex-shrink: 0;
  }
  .qt-stab {
    padding: 12px 18px; font-size: .82rem; font-weight: 700; cursor: pointer;
    border: none; background: none; color: #64748b;
    border-bottom: 3px solid transparent; margin-bottom: -2px;
    transition: color .15s, border-color .15s;
  }
  .qt-stab.active { color: #e8701a; border-bottom-color: #e8701a; }
  .qt-stab:hover:not(.active) { color: #1a2744; }
  .qt-spanel { display: none; padding: 22px 24px; }
  .qt-spanel.active { display: block; }
  .qt-sfield-label {
    font-size: .75rem; font-weight: 700; color: #64748b; text-transform: uppercase;
    letter-spacing: .04em; margin-bottom: 6px; margin-top: 16px;
  }
  .qt-sfield-label:first-child { margin-top: 0; }
  .qt-sinput {
    border: 1.5px solid #e2e8f0; border-radius: 8px; padding: 8px 11px;
    font-size: .88rem; outline: none; width: 110px;
    transition: border-color .15s;
  }
  .qt-sinput:focus { border-color: #e8701a; }
  .qt-stier-table { width: 100%; border-collapse: collapse; font-size: .78rem; }
  .qt-stier-table th {
    background: #f1f5f9; padding: 7px 10px; text-align: center;
    font-weight: 700; color: #475569; border: 1px solid #e2e8f0; white-space: nowrap;
  }
  .qt-stier-table td { padding: 5px 6px; border: 1px solid #e2e8f0; text-align: center; }
  .qt-stier-table .qt-stier-label {
    font-weight: 700; color: #1a2744; text-align: left; background: #f8fafc;
    white-space: nowrap; padding: 5px 10px;
  }
  .qt-stier-table input {
    width: 68px; padding: 5px 6px; border: 1.5px solid #e2e8f0;
    border-radius: 6px; font-size: .78rem; text-align: center; outline: none;
  }
  .qt-stier-table input:focus { border-color: #e8701a; }
  .qt-margin-row { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; }
  .qt-margin-row label { font-size: .8rem; color: #475569; min-width: 80px; }
  .qt-settings-footer {
    padding: 14px 24px; border-top: 1.5px solid #e2e8f0;
    display: flex; justify-content: space-between; align-items: center;
    flex-shrink: 0; background: #f8fafc; border-radius: 0 0 18px 18px;
  }

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
<script src="https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js"></script>
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
    const safeDesc = (p.description||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
    const addBtn = p.basePrice
      ? `<button class="add-quote-btn-sm" data-qname="${safeName}" data-qprice="${p.basePrice}" data-qdesc="${safeDesc}" onclick="event.stopPropagation();addToQuote(this.dataset.qname,this.dataset.qprice,this.dataset.qdesc)">+ Quote</button>`
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
        ${prod.basePrice ? `<button class="add-quote-btn" data-qname="${(prod.productName||prod.productId||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;')}" data-qprice="${prod.basePrice}" data-qdesc="${(prod.description||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;')}" data-qimg="${(defaultImageUrl||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;')}" onclick="addToQuote(this.dataset.qname,this.dataset.qprice,this.dataset.qdesc,this.dataset.qimg)">&#128203; Add to Quote</button>` : ''}
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
function addToQuote(name, price, description, imageUrl) {
  document.getElementById('qt-garment-desc').value      = name;
  document.getElementById('qt-apparel-cost').value      = parseFloat(price).toFixed(2);
  document.getElementById('qt-garment-full-desc').value = description || '';
  window._qtDefaultImageUrl = imageUrl || '';
  qtClearImageUpload();                    // drop any leftover manual upload from a prior item
  if (imageUrl) qtShowImagePreview(imageUrl, false);
  qtClearResults(); qtOnQtyChange();
  qtOpen();
  setTimeout(() => document.getElementById('qt-qty').focus(), 350);
}

// ─── Quote Builder — Garment Image (upload or SanMar default) ────────────────
window._qtUploadedImageB64 = null;
window._qtDefaultImageUrl  = '';

function qtShowImagePreview(dataUrlOrSrc, isCustom) {
  const wrap = document.getElementById('qt-garment-image-preview');
  const img  = document.getElementById('qt-garment-image-preview-img');
  const hint = document.getElementById('qt-garment-image-hint');
  img.src = dataUrlOrSrc;
  wrap.style.display = 'flex';
  hint.textContent = isCustom
    ? 'Custom image will be used in the PDF instead of the SanMar photo.'
    : 'Using the SanMar product photo for the PDF.';
}

function qtHandleImageUpload(e) {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      // Downscale large uploads so the PDF stays small and localStorage doesn't bloat.
      const maxDim = 900;
      let { width, height } = img;
      if (width > maxDim || height > maxDim) {
        const scale = maxDim / Math.max(width, height);
        width = Math.round(width * scale);
        height = Math.round(height * scale);
      }
      const canvas = document.createElement('canvas');
      canvas.width = width; canvas.height = height;
      canvas.getContext('2d').drawImage(img, 0, 0, width, height);
      window._qtUploadedImageB64 = canvas.toDataURL('image/jpeg', 0.88);
      qtShowImagePreview(window._qtUploadedImageB64, true);
      qtClearResults();
    };
    img.src = reader.result;
  };
  reader.readAsDataURL(file);
}

function qtClearImageUpload() {
  window._qtUploadedImageB64 = null;
  const fileInput = document.getElementById('qt-garment-image');
  if (fileInput) fileInput.value = '';
  const wrap = document.getElementById('qt-garment-image-preview');
  const hint = document.getElementById('qt-garment-image-hint');
  if (wrap) wrap.style.display = 'none';
  if (hint) hint.textContent = window._qtDefaultImageUrl
    ? 'No custom image uploaded — the SanMar product photo will be used in the PDF.'
    : 'No image uploaded — the SanMar product photo will be used in the PDF if one is available.';
  qtClearResults();
}

// ─── Quote Builder — Pricing (dynamic, editable via Settings) ────────────────
const QT_PRICING_KEY = '4zd_pricing_v1';
const QT_HT_SIZES = [
  '1.5" x 1.5"','2.5" x 2.5"','4" x 4"',
  '5.8" x 8.3"','11.7" x 4.25"','16.5" x 5.85"',
  '8.3" x 11.7"','11.7" x 11.7"','11.7" x 16.5"'
];
const QT_DEFAULT_PRICING = {
  ipuRate: 0.10,
  margins: [
    {from:1,   to:35,  rate:1.00},
    {from:36,  to:60,  rate:0.85},
    {from:61,  to:144, rate:0.65},
    {from:145, to:249, rate:0.55},
    {from:250, to:9999,rate:0.40},
  ],
  htTiers: [
    {from:10,  to:19,  prices:[2.62,2.95,3.23,4.28,4.28,5.12,5.12,6.43,7.74]},
    {from:20,  to:49,  prices:[2.18,2.45,2.69,3.57,3.57,4.27,4.27,5.37,6.46]},
    {from:50,  to:99,  prices:[1.30,1.62,1.96,2.56,2.56,3.30,3.30,4.24,5.20]},
    {from:100, to:199, prices:[0.87,1.11,1.45,1.77,1.77,2.27,2.27,2.95,3.63]},
    {from:200, to:299, prices:[0.70,0.93,1.26,1.49,1.49,1.86,1.86,2.43,3.02]},
    {from:300, to:499, prices:[0.56,0.78,1.10,1.33,1.33,1.71,1.71,2.24,2.78]},
  ],
  embTiers: [
    {from:1,   to:5,   price:7.00},
    {from:6,   to:23,  price:5.50},
    {from:24,  to:35,  price:5.25},
    {from:36,  to:71,  price:4.75},
    {from:72,  to:143, price:4.25},
    {from:144, to:500, price:3.25},
  ],
  spTiers: [
    {from:12,  to:36,  factor:30, base:[2.85,2.95,3.10,3.30,3.50,3.65]},
    {from:37,  to:60,  factor:30, base:[2.35,2.45,2.60,2.70,2.80,2.90]},
    {from:61,  to:144, factor:30, base:[2.00,2.05,2.20,2.40,2.55,2.65]},
    {from:145, to:249, factor:30, base:[1.80,1.90,2.05,2.15,2.35,2.55]},
    {from:250, to:600, factor:34, base:[1.20,1.25,1.45,1.50,1.65,2.50]},
  ],
};
function qtGetActivePricing() {
  try {
    const s = localStorage.getItem(QT_PRICING_KEY);
    return s ? JSON.parse(s) : JSON.parse(JSON.stringify(QT_DEFAULT_PRICING));
  } catch(e) { return JSON.parse(JSON.stringify(QT_DEFAULT_PRICING)); }
}
function qtGetMargin(qty) {
  const t = qtGetActivePricing().margins.find(r => qty >= r.from && qty <= r.to);
  return t ? t.rate : 0.40;
}
function qtMarginLabel(qty) { return (qtGetMargin(qty)*100).toFixed(0)+'%'; }
function qtHtPrice(qty, si)  { const t = qtGetActivePricing().htTiers.find(r => qty >= r.from && qty <= r.to); return t ? t.prices[si] : null; }
function qtEmbPrice(qty)     { const t = qtGetActivePricing().embTiers.find(r => qty >= r.from && qty <= r.to); return t ? t.price : null; }
function qtSpPrice(qty, ci)  { const t = qtGetActivePricing().spTiers.find(r => qty >= r.from && qty <= r.to); if (!t) return null; return t.base[ci] + t.factor*(ci+1)/qty; }

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
    <div class="qt-loc-opts hidden" id="qt-opts-${key}-desc">
      <div class="qt-form-group">
        <label>Description <span style="font-weight:400;color:#94a3b8;">(optional)</span></label>
        <input class="qt-input" type="text" id="qt-${key}-desc-override" placeholder="e.g. Left Chest Logo" oninput="qtClearResults()">
        <div class="qt-hint">Leave blank to use the price-based description (e.g. "Heat Transfer — 4&quot; x 4&quot;") on the quote.</div>
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
  const descWrap = document.getElementById(`qt-opts-${key}-desc`);
  if (descWrap) descWrap.classList.toggle('hidden', type === 'none');
  qtClearResults();
}

// ─── Quote Builder — Qty / margin ────────────────────────────────────────────
function qtGetMarginOverrideRaw() {
  const el = document.getElementById('qt-margin-override');
  return el ? el.value.trim() : '';
}
function qtMarginOverrideActive() {
  const raw = qtGetMarginOverrideRaw();
  return raw !== '' && !isNaN(parseFloat(raw)) && parseFloat(raw) >= 0;
}
function qtOnQtyChange() {
  const qty  = parseInt(document.getElementById('qt-qty').value);
  const hint = document.getElementById('qt-margin-hint');
  const overrideRaw = qtGetMarginOverrideRaw();
  if (overrideRaw !== '') {
    hint.textContent = qtMarginOverrideActive()
      ? 'Margin override active: ' + parseFloat(overrideRaw).toFixed(1) + '%' + ((!isNaN(qty) && qty > 0) ? ' (tiered rate would be ' + qtMarginLabel(qty) + ')' : '')
      : 'Enter a valid margin override (0 or higher), or clear the field to use the tiered rate.';
  } else {
    hint.textContent = (!isNaN(qty) && qty > 0) ? 'Margin rate: ' + qtMarginLabel(qty) : '';
  }
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
  const garmentFullDesc = document.getElementById('qt-garment-full-desc').value.trim();
  const decorationDesc = document.getElementById('qt-decoration-desc').value.trim();

  if (isNaN(apparelCost) || apparelCost < 0) { qtShowAlert('err','Enter a valid apparel cost.'); return; }
  if (isNaN(qty) || qty < 1)                 { qtShowAlert('err','Enter a valid quantity.'); return; }

  const marginOverrideRaw = qtGetMarginOverrideRaw();
  if (marginOverrideRaw !== '' && !qtMarginOverrideActive()) { qtShowAlert('err','Enter a valid margin override percentage (0 or higher), or clear the field.'); return; }
  const marginOverridden = qtMarginOverrideActive();

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
    const descOverrideEl = document.getElementById(`qt-${key}-desc-override`);
    const descOverride   = descOverrideEl ? descOverrideEl.value.trim() : '';
    active.push({ key, locLabel: QT_LOC_LABELS[key], price, label: descOverride || label, autoLabel: label, descOverridden: !!descOverride });
  }

  if (active.length === 0) { qtShowAlert('err', 'Select at least one decoration location.'); return; }

  const totalPrintCost = active.reduce((s, a) => s + a.price, 0);
  const margin         = marginOverridden ? parseFloat(marginOverrideRaw) / 100 : qtGetMargin(qty);
  const subTotal       = totalPrintCost + apparelCost;
  const totalUnit      = subTotal * (1 + margin);
  const marginDollars  = (totalUnit - subTotal) * qty;
  const ipuPerUnit     = totalUnit * qtGetActivePricing().ipuRate;
  const ipuTotal       = ipuPerUnit * qty;
  const profit         = marginDollars - ipuTotal;
  const totalOrder     = totalUnit * qty;

  document.getElementById('qt-r-total-unit').textContent  = qtFmt(totalUnit);
  document.getElementById('qt-r-total-order').textContent = qtFmtK(totalOrder);
  document.getElementById('qt-r-profit').textContent      = qtFmtK(profit);
  document.getElementById('qt-r-apparel').textContent     = qtFmt(apparelCost);
  document.getElementById('qt-r-print-total').textContent = qtFmt(totalPrintCost);
  document.getElementById('qt-r-subtotal').textContent    = qtFmt(subTotal);
  document.getElementById('qt-r-margin-rate').textContent = (margin*100).toFixed(1) + '%' + (marginOverridden ? ' (override)' : '');
  document.getElementById('qt-r-margin').textContent      = qtFmtK(marginDollars);
  document.getElementById('qt-r-ipu-total').textContent   = qtFmtK(ipuTotal);
  document.getElementById('qt-r-profit2').textContent     = qtFmtK(profit);

  document.getElementById('qt-loc-breakdown').innerHTML = active.map(a =>
    `<div class="qt-loc-line"><span class="lk">${a.locLabel}</span><span class="lv">${a.label} — ${qtFmt(a.price)}/unit</span></div>`
  ).join('');

  const result = { totalUnit, totalOrder, subTotal, marginDollars, ipuTotal, profit, qty, apparelCost, clientName, garmentDesc, garmentFullDesc, active, margin, marginOverridden, decorationDesc, garmentImageUrl: window._qtDefaultImageUrl || '', garmentImageUploaded: !!window._qtUploadedImageB64 };
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
  const decorDescBlock = r.decorationDesc
    ? `<p style="margin:0 0 14px;color:#444;"><strong>What's included:</strong> ${qtEscHtml(r.decorationDesc)}</p>`
    : '';
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;color:#222;background:#fff;font-size:15px;line-height:1.6;">
<div style="max-width:560px;margin:0 auto;">
<p style="margin:0 0 16px">Hi ${qtEscHtml(r.clientName)},</p>
<p style="margin:0 0 16px">Thanks for reaching out—here's a first pass on your quote:</p>
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:2px solid #1a2744;border-bottom:2px solid #1a2744;margin:20px 0;">
  <tr><td colspan="2" style="padding:14px 4px 10px;font-weight:700;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#1a2744;">QUOTE SUMMARY</td></tr>
  <tr><td style="padding:5px 4px;color:#666;width:175px;">Garment:</td><td style="padding:5px 4px;font-weight:600;">${qtEscHtml(r.garmentDesc)}</td></tr>
  ${r.garmentFullDesc ? `<tr><td></td><td style="padding:0 4px 5px;font-size:12.5px;color:#777;">${qtEscHtml(r.garmentFullDesc)}</td></tr>` : ''}
  <tr><td style="padding:5px 4px;color:#666;vertical-align:top;">Decoration:</td><td style="padding:5px 4px;font-weight:600;">${r.active.length===1?qtEscHtml(r.active[0].label):''}</td></tr>
  ${r.active.length>1?decorRows:''}
  <tr><td style="padding:5px 4px;color:#666;">Quantity:</td><td style="padding:5px 4px;font-weight:600;">${r.qty} pieces</td></tr>
  <tr><td style="padding:5px 4px;color:#666;">Price per garment:</td><td style="padding:5px 4px;font-weight:600;">${qtFmt(r.totalUnit)}</td></tr>
  <tr><td style="padding:14px 4px 10px;color:#444;font-size:15px;">Total:</td><td style="padding:14px 4px 10px;font-weight:700;font-size:20px;color:#e8701a;">${qtFmt(r.totalOrder)}</td></tr>
</table>
${decorDescBlock}
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

// ─── Quote Builder — Logo (base64, embedded for PDF generation) ─────────────
const QT_LOGO_B64 = "data:image/png;base64,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";
const QT_LOGO_ASPECT = 242/900; // height/width of the source asset

// ─── Quote Builder — PDF Generation ──────────────────────────────────────────
// Resolve the image to embed in the PDF: a manually uploaded image wins; otherwise fall
// back to the SanMar product photo (fetched through our own server to dodge CORS).
async function qtGetGarmentImageDataUrl(r) {
  if (window._qtUploadedImageB64) return window._qtUploadedImageB64;
  if (r.garmentImageUrl) {
    try {
      const resp = await fetch('/api/image_proxy?url=' + encodeURIComponent(r.garmentImageUrl));
      if (!resp.ok) throw new Error('Image proxy returned ' + resp.status);
      const blob = await resp.blob();
      return await new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload  = () => resolve(fr.result);
        fr.onerror = () => reject(new Error('Could not read image data'));
        fr.readAsDataURL(blob);
      });
    } catch (e) {
      console.error('Could not load the SanMar product image for the PDF', e);
      return null;
    }
  }
  return null;
}

async function qtGeneratePdf(r) {
  if (!window.jspdf || !window.jspdf.jsPDF) { alert('PDF library did not load — check your internet connection and try again.'); return; }
  const { jsPDF } = window.jspdf;
  const doc = new jsPDF({ unit: 'pt', format: 'letter' });
  const pageW = doc.internal.pageSize.getWidth();
  const margin = 54;
  let y = 56;

  const logoW = 130, logoH = logoW * QT_LOGO_ASPECT;
  try { doc.addImage(QT_LOGO_B64, 'PNG', margin, y, logoW, logoH); } catch (e) { console.error('Logo embed failed', e); }

  const today = new Date().toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
  doc.setFont('helvetica', 'normal'); doc.setFontSize(9); doc.setTextColor(120);
  doc.text('Quote Date: ' + today, pageW - margin, y + 14, { align: 'right' });
  doc.text('Prepared by: Marc — 4Z Design', pageW - margin, y + 27, { align: 'right' });
  doc.text('marc@4zdesign.com', pageW - margin, y + 40, { align: 'right' });

  y += Math.max(logoH, 44) + 22;
  doc.setDrawColor(26, 39, 68); doc.setLineWidth(1.4);
  doc.line(margin, y, pageW - margin, y);
  y += 28;

  doc.setFont('helvetica', 'bold'); doc.setFontSize(17); doc.setTextColor(26, 39, 68);
  doc.text('Quote for ' + r.clientName, margin, y);
  y += 10;
  doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5); doc.setTextColor(140);
  doc.text('QUOTE SUMMARY', margin, y + 14);
  y += 32;

  doc.setFontSize(11);
  const infoRows = [
    ['Garment', r.garmentDesc],
    ['Quantity', r.qty + ' pieces'],
    ['Price per Garment', qtFmt(r.totalUnit)],
  ];
  infoRows.forEach(([label, val]) => {
    doc.setFont('helvetica', 'bold'); doc.setTextColor(90);
    doc.text(label + ':', margin, y);
    doc.setFont('helvetica', 'normal'); doc.setTextColor(30);
    doc.text(String(val), margin + 150, y);
    y += 18;
  });

  if (r.garmentFullDesc) {
    doc.setFont('helvetica', 'italic'); doc.setFontSize(9.5); doc.setTextColor(110);
    const gLines = doc.splitTextToSize(r.garmentFullDesc, pageW - margin * 2 - 8);
    doc.text(gLines, margin, y);
    y += gLines.length * 12 + 4;
  }

  const garmentImgDataUrl = await qtGetGarmentImageDataUrl(r);
  if (garmentImgDataUrl) {
    try {
      const props = doc.getImageProperties(garmentImgDataUrl);
      const maxW = 170, maxH = 170;
      let iw = maxW, ih = maxW * (props.height / props.width);
      if (ih > maxH) { ih = maxH; iw = maxH * (props.width / props.height); }
      const imgX = margin + ((pageW - margin * 2) - iw) / 2;
      y += 10;
      doc.setDrawColor(225); doc.setLineWidth(0.75);
      doc.rect(imgX - 2, y - 2, iw + 4, ih + 4);
      doc.addImage(garmentImgDataUrl, props.fileType, imgX, y, iw, ih);
      y += ih + 20;
    } catch (e) { console.error('Could not embed the garment image in the PDF', e); }
  }

  y += 8;
  doc.setFont('helvetica', 'bold'); doc.setFontSize(11); doc.setTextColor(26, 39, 68);
  doc.text('Decoration', margin, y);
  y += 17;
  doc.setFont('helvetica', 'normal'); doc.setFontSize(10.5); doc.setTextColor(70);
  r.active.forEach(a => {
    doc.text('• ' + a.locLabel + ': ' + a.label, margin + 8, y);
    y += 15;
  });

  if (r.decorationDesc) {
    y += 10;
    doc.setFont('helvetica', 'bold'); doc.setFontSize(10); doc.setTextColor(26, 39, 68);
    doc.text('What’s Included', margin, y);
    y += 14;
    doc.setFont('helvetica', 'normal'); doc.setFontSize(10); doc.setTextColor(70);
    const lines = doc.splitTextToSize(r.decorationDesc, pageW - margin * 2 - 8);
    doc.text(lines, margin + 8, y);
    y += lines.length * 13 + 6;
  }

  y += 14;
  doc.setDrawColor(225); doc.setLineWidth(1);
  doc.line(margin, y, pageW - margin, y);
  y += 30;

  doc.setFont('helvetica', 'bold'); doc.setFontSize(13); doc.setTextColor(26, 39, 68);
  doc.text('Total Order:', margin, y);
  doc.setFontSize(19); doc.setTextColor(232, 112, 26);
  doc.text(qtFmt(r.totalOrder), margin + 135, y);
  y += 34;

  doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5); doc.setTextColor(100);
  const notes = [
    'This price includes the decoration above and is based on the quantity shown.',
    'Shipping and sales tax are not included and will be added once details are finalized.',
    'Questions or want changes? Just reply to this quote — happy to adjust.',
  ];
  notes.forEach(n => {
    const lines = doc.splitTextToSize(n, pageW - margin * 2);
    doc.text(lines, margin, y);
    y += lines.length * 13 + 5;
  });

  y += 12;
  doc.setDrawColor(225); doc.line(margin, y, pageW - margin, y);
  y += 22;
  doc.setFont('helvetica', 'bold'); doc.setFontSize(10.5); doc.setTextColor(26, 39, 68);
  doc.text('Marc — 4Z Design', margin, y);
  y += 14;
  doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5); doc.setTextColor(232, 112, 26);
  doc.text('marc@4zdesign.com', margin, y);
  y += 13;
  doc.setTextColor(100);
  doc.text('www.4zdesign.com', margin, y);

  const safeClient = (r.clientName || 'Client').replace(/[\\/:*?"<>|]/g, '').trim() || 'Client';
  doc.save('Quote - ' + safeClient + ' - ' + r.qty + 'pc.pdf');
}

// ─── Quote Builder — Copy / Edit ─────────────────────────────────────────────
function qtFlash() { const f = document.getElementById('qt-copy-flash'); f.classList.add('show'); setTimeout(()=>f.classList.remove('show'),2000); }
async function qtOpenEmail() {
  const r = window._qtLastResult; if (!r) return;
  const attachPdf = document.getElementById('qt-attach-pdf').checked;
  if (attachPdf) {
    try { await qtGeneratePdf(r); } catch (e) { console.error(e); alert('Could not generate the PDF: ' + e.message); }
  }
  const subject = encodeURIComponent('Quote — ' + r.garmentDesc + ' x' + r.qty);
  const body    = encodeURIComponent(
    'Hi ' + r.clientName + ',\n\n' +
    'Thanks for reaching out\u2014here\u2019s a first pass on your quote:\n\n' +
    '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n' +
    'QUOTE SUMMARY\n' +
    '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n' +
    'Garment:           ' + r.garmentDesc + '\n' +
    (r.garmentFullDesc ? '                   ' + r.garmentFullDesc + '\n' : '') +
    'Decoration:\n' + r.active.map(a => '  ' + a.locLabel + ': ' + a.label).join('\n') + '\n' +
    'Quantity:          ' + r.qty + ' pieces\n' +
    'Price per garment: ' + qtFmt(r.totalUnit) + '\n' +
    'Total:             ' + qtFmt(r.totalOrder) + '\n' +
    '\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n' +
    (r.decorationDesc ? 'What\u2019s included: ' + r.decorationDesc + '\n\n' : '') +
    'This includes the decoration and is based on the quantity above.\n\n' +
    'Shipping and sales tax are not included and will be added once we finalize details.\n\n' +
    (attachPdf ? 'A PDF copy of this quote has just been downloaded to your computer\u2014please attach it to this email before sending.\n\n' : '') +
    'If you want to tweak garment options, sizing, or quantities, I can adjust this quickly. Just let me know what direction you want to go.\n\n' +
    '\u2014 Marc\n4Z Design\nmarc@4zdesign.com'
  );
  window.location.href = 'mailto:?subject=' + subject + '&body=' + body;
}
function qtCopyPlainText() {
  const r = window._qtLastResult; if (!r) return;
  const text = `Hi ${r.clientName},\n\nThanks for reaching out—here's a first pass on your quote:\n\n──────────────────────────────\nQUOTE SUMMARY\n──────────────────────────────\nGarment:           ${r.garmentDesc}\n${r.garmentFullDesc ? '                   ' + r.garmentFullDesc + '\n' : ''}Decoration:\n${r.active.map(a=>'  '+a.locLabel+': '+a.label).join('\n')}\nQuantity:          ${r.qty} pieces\nPrice per garment: ${qtFmt(r.totalUnit)}\nTotal:             ${qtFmt(r.totalOrder)}\n──────────────────────────────\n\n${r.decorationDesc ? 'What’s included: ' + r.decorationDesc + '\n\n' : ''}This includes the decoration and is based on the quantity above.\n\nShipping and sales tax are not included and will be added once we finalize details.\n\nIf you want to tweak garment options, sizing, or quantities, I can adjust this quickly. Just let me know what direction you want to go.\n\n— Marc\n4Z Design\nmarc@4zdesign.com`;
  navigator.clipboard.writeText(text).then(qtFlash);
}
function qtOpenEditModal()  { document.getElementById('qt-edit-html-area').value = window._qtQuoteHtml||''; document.getElementById('qt-edit-modal').classList.add('open'); }
function qtApplyEditedHtml(){ window._qtQuoteHtml = document.getElementById('qt-edit-html-area').value; qtRenderQuotePreview(window._qtQuoteHtml); qtCloseModal('qt-edit-modal'); }

// ─── Quote Builder — Reset ────────────────────────────────────────────────────
function qtResetAll() {
  QT_LOC_KEYS.forEach(k => {
    qtSetLocType(k,'none');
    const el = document.getElementById(`qt-${k}-desc-override`);
    if (el) el.value = '';
  });
  ['qt-apparel-cost','qt-qty','qt-client-name','qt-garment-desc','qt-garment-full-desc','qt-margin-override','qt-decoration-desc'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('qt-attach-pdf').checked = false;
  document.getElementById('qt-margin-hint').textContent = '';
  window._qtDefaultImageUrl = '';
  qtClearImageUpload();
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
      garmentFullDesc: r.garmentFullDesc || '',
      apparelCost: r.apparelCost, qty: r.qty,
      marginOverride: document.getElementById('qt-margin-override').value,
      decorationDesc: r.decorationDesc || '',
      garmentImageUrl: r.garmentImageUrl || '',
      uploadedImageB64: window._qtUploadedImageB64 || '',
      locState: {...qtLocState},
      locSelects: Object.fromEntries(QT_LOC_KEYS.flatMap(k => [
        [`qt-${k}-ht-size`,   document.getElementById(`qt-${k}-ht-size`).value],
        [`qt-${k}-sp-colors`, document.getElementById(`qt-${k}-sp-colors`).value],
        [`qt-${k}-desc-override`, document.getElementById(`qt-${k}-desc-override`)?.value || ''],
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
  document.getElementById('qt-garment-full-desc').value = fs.garmentFullDesc || '';
  document.getElementById('qt-apparel-cost').value  = fs.apparelCost;
  document.getElementById('qt-qty').value           = fs.qty;
  document.getElementById('qt-margin-override').value = fs.marginOverride || '';
  document.getElementById('qt-decoration-desc').value = fs.decorationDesc || '';
  window._qtDefaultImageUrl = fs.garmentImageUrl || '';
  if (fs.uploadedImageB64) {
    window._qtUploadedImageB64 = fs.uploadedImageB64;
    qtShowImagePreview(fs.uploadedImageB64, true);
  } else if (fs.garmentImageUrl) {
    qtShowImagePreview(fs.garmentImageUrl, false);
  }
  qtOnQtyChange();
  QT_LOC_KEYS.forEach(k => {
    if (fs.locState && fs.locState[k]) qtSetLocType(k, fs.locState[k]);
    if (fs.locSelects) {
      const htEl   = document.getElementById(`qt-${k}-ht-size`);
      const spEl   = document.getElementById(`qt-${k}-sp-colors`);
      const descEl = document.getElementById(`qt-${k}-desc-override`);
      if (htEl && fs.locSelects[`qt-${k}-ht-size`])   htEl.value = fs.locSelects[`qt-${k}-ht-size`];
      if (spEl && fs.locSelects[`qt-${k}-sp-colors`])  spEl.value = fs.locSelects[`qt-${k}-sp-colors`];
      if (descEl && fs.locSelects[`qt-${k}-desc-override`]) descEl.value = fs.locSelects[`qt-${k}-desc-override`];
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

// ── Pricing Settings ──────────────────────────────────────────
let _qtActiveSettingsTab = 'general';
function qtOpenSettings() {
  qtRenderSettingsTabs();
  document.getElementById('qt-settings-overlay').classList.add('open');
}
function qtCloseSettings() {
  document.getElementById('qt-settings-overlay').classList.remove('open');
}
function qtSettingsTab(name) {
  _qtActiveSettingsTab = name;
  document.querySelectorAll('.qt-stab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.qt-spanel').forEach(p => p.classList.toggle('active', p.id === 'qt-sp-' + name));
}
function qtRenderSettingsTabs() {
  const p = qtGetActivePricing();
  // General
  document.getElementById('qt-s-ipu-rate').value = (p.ipuRate * 100).toFixed(2);
  const mc = document.getElementById('qt-s-margins');
  mc.innerHTML = p.margins.map((m, i) => `
    <div class="qt-margin-row">
      <label>Qty ${m.from}–${m.to}</label>
      <input class="qt-sinput" type="number" step="0.01" min="0" max="5" id="qt-s-mg-${i}" value="${(m.rate*100).toFixed(1)}"> %
    </div>`).join('');
  // Heat Transfer
  const htSizes = ['1.5" sq','2.5" sq','4" sq','6" sq','8" sq','10" sq','12" sq','14" sq','4"×16"'];
  const htHead = '<tr><th>Qty Range</th>' + htSizes.map(s => `<th>${s}</th>`).join('') + '</tr>';
  const htRows = p.htTiers.map((t, ti) =>
    `<tr><td class="qt-stier-label">${t.from}–${t.to}</td>` +
    t.prices.map((v, si) => `<td><input type="number" step="0.01" min="0" id="qt-s-ht-${ti}-${si}" value="${v.toFixed(2)}"></td>`).join('') +
    '</tr>'
  ).join('');
  document.getElementById('qt-s-ht-table').innerHTML = htHead + htRows;
  // Embroidery
  const embRows = p.embTiers.map((t, ti) =>
    `<tr><td class="qt-stier-label">${t.from}–${t.to < 9999 ? t.to : '+'}</td>` +
    `<td><input type="number" step="0.01" min="0" id="qt-s-emb-${ti}" value="${t.price.toFixed(2)}"></td></tr>`
  ).join('');
  document.getElementById('qt-s-emb-table').innerHTML =
    '<tr><th>Qty Range</th><th>Price / Location</th></tr>' + embRows;
  // Screen Print
  const spCols = ['1 Color','2 Colors','3 Colors','4 Colors','5 Colors','6 Colors'];
  const spHead = '<tr><th>Qty Range</th><th>Setup Factor</th>' + spCols.map(c => `<th>${c} Base</th>`).join('') + '</tr>';
  const spRows = p.spTiers.map((t, ti) =>
    `<tr><td class="qt-stier-label">${t.from}–${t.to}</td>` +
    `<td><input type="number" step="1" min="0" id="qt-s-sp-${ti}-factor" value="${t.factor}"></td>` +
    t.base.map((v, ci) => `<td><input type="number" step="0.01" min="0" id="qt-s-sp-${ti}-${ci}" value="${v.toFixed(2)}"></td>`).join('') +
    '</tr>'
  ).join('');
  document.getElementById('qt-s-sp-table').innerHTML = spHead + spRows;
  // set active tab
  qtSettingsTab(_qtActiveSettingsTab);
}
function qtSaveSettings() {
  const p = qtGetActivePricing();
  // General
  const ipuPct = parseFloat(document.getElementById('qt-s-ipu-rate').value);
  if (isNaN(ipuPct)) { alert('Invalid IPU rate'); return; }
  p.ipuRate = ipuPct / 100;
  p.margins.forEach((m, i) => {
    const v = parseFloat(document.getElementById(`qt-s-mg-${i}`)?.value);
    if (!isNaN(v)) m.rate = v / 100;
  });
  // Heat Transfer
  p.htTiers.forEach((t, ti) => {
    t.prices.forEach((_, si) => {
      const v = parseFloat(document.getElementById(`qt-s-ht-${ti}-${si}`)?.value);
      if (!isNaN(v)) t.prices[si] = v;
    });
  });
  // Embroidery
  p.embTiers.forEach((t, ti) => {
    const v = parseFloat(document.getElementById(`qt-s-emb-${ti}`)?.value);
    if (!isNaN(v)) t.price = v;
  });
  // Screen Print
  p.spTiers.forEach((t, ti) => {
    const fv = parseFloat(document.getElementById(`qt-s-sp-${ti}-factor`)?.value);
    if (!isNaN(fv)) t.factor = fv;
    t.base.forEach((_, ci) => {
      const v = parseFloat(document.getElementById(`qt-s-sp-${ti}-${ci}`)?.value);
      if (!isNaN(v)) t.base[ci] = v;
    });
  });
  localStorage.setItem(QT_PRICING_KEY, JSON.stringify(p));
  qtCloseSettings();
  toast('Pricing saved!');
}
function qtResetPricing() {
  if (!confirm('Reset all pricing to factory defaults?')) return;
  localStorage.removeItem(QT_PRICING_KEY);
  qtRenderSettingsTabs();
  toast('Pricing reset to defaults.');
}

</script>

<!-- ── Pricing Settings Modal ─────────────────────────────── -->
<div class="qt-settings-overlay" id="qt-settings-overlay" onclick="if(event.target===this)qtCloseSettings()">
  <div class="qt-settings-box">
    <div class="qt-settings-header">
      <div class="qt-settings-title">&#9881;&#65039; Pricing Settings</div>
      <button class="qt-close-btn" onclick="qtCloseSettings()" title="Close">&times;</button>
    </div>
    <div class="qt-stab-bar">
      <button class="qt-stab active" data-tab="general" onclick="qtSettingsTab('general')">General</button>
      <button class="qt-stab" data-tab="ht" onclick="qtSettingsTab('ht')">Heat Transfer</button>
      <button class="qt-stab" data-tab="emb" onclick="qtSettingsTab('emb')">Embroidery</button>
      <button class="qt-stab" data-tab="sp" onclick="qtSettingsTab('sp')">Screen Print</button>
    </div>
    <div class="qt-settings-body">

      <!-- General -->
      <div class="qt-spanel active" id="qt-sp-general">
        <div class="qt-sfield-label">IPU Rate (% of unit price)</div>
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:20px;">
          <input class="qt-sinput" type="number" step="0.01" min="0" max="100" id="qt-s-ipu-rate"> %
        </div>
        <div class="qt-sfield-label">Margin Tiers (% markup on cost)</div>
        <div id="qt-s-margins"></div>
      </div>

      <!-- Heat Transfer -->
      <div class="qt-spanel" id="qt-sp-ht">
        <div class="qt-sfield-label">Price per location by qty &amp; transfer size</div>
        <div style="overflow-x:auto;">
          <table class="qt-stier-table" id="qt-s-ht-table"></table>
        </div>
      </div>

      <!-- Embroidery -->
      <div class="qt-spanel" id="qt-sp-emb">
        <div class="qt-sfield-label">Price per location by qty</div>
        <table class="qt-stier-table" id="qt-s-emb-table"></table>
      </div>

      <!-- Screen Print -->
      <div class="qt-spanel" id="qt-sp-sp">
        <div class="qt-sfield-label">Base price per color + per-color setup factor</div>
        <div style="overflow-x:auto;">
          <table class="qt-stier-table" id="qt-s-sp-table"></table>
        </div>
      </div>

    </div>
    <div class="qt-settings-footer">
      <button class="qt-btn-outline" onclick="qtResetPricing()" style="color:#ef4444;border-color:#ef4444;">&#9851; Reset to Defaults</button>
      <div style="display:flex;gap:10px;">
        <button class="qt-btn-outline" onclick="qtCloseSettings()">Cancel</button>
        <button class="qt-btn" onclick="qtSaveSettings()">Save Pricing</button>
      </div>
    </div>
  </div>
</div>

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
      <button class="qt-hdr-btn" onclick="qtOpenSettings()">&#9881;&#65039; Settings</button>
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
      <div class="qt-form-row" style="grid-template-columns:1fr;">
        <div class="qt-form-group">
          <label for="qt-garment-full-desc">Garment Description <span style="font-weight:400;color:#94a3b8;">(auto-filled from SanMar — edit if needed)</span></label>
          <textarea class="qt-input" id="qt-garment-full-desc" rows="2" placeholder="Pulled automatically when you add a product from search — type your own if this quote wasn't started from a product page." oninput="qtClearResults()" style="resize:vertical;min-height:44px;font-family:inherit;"></textarea>
        </div>
      </div>
      <div class="qt-form-row" style="grid-template-columns:1fr;">
        <div class="qt-form-group">
          <label for="qt-garment-image">Garment Image <span style="font-weight:400;color:#94a3b8;">(optional — used in the PDF)</span></label>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <input type="file" id="qt-garment-image" accept="image/png,image/jpeg,image/webp" onchange="qtHandleImageUpload(event)" style="font-size:.8rem;">
            <div id="qt-garment-image-preview" style="display:none;align-items:center;gap:8px;">
              <img id="qt-garment-image-preview-img" style="width:48px;height:48px;object-fit:cover;border-radius:6px;border:1.5px solid #e2e8f0;">
              <button type="button" class="qt-btn-outline" style="padding:4px 10px;font-size:.72rem;flex:none;" onclick="qtClearImageUpload()">Remove</button>
            </div>
          </div>
          <div class="qt-hint" id="qt-garment-image-hint">No image uploaded — the SanMar product photo will be used in the PDF if one is available.</div>
        </div>
      </div>
      <div class="qt-form-row" style="grid-template-columns:1fr;">
        <div class="qt-form-group">
          <label for="qt-margin-override">Margin Override % <span style="font-weight:400;color:#94a3b8;">(optional)</span></label>
          <input class="qt-input" type="number" id="qt-margin-override" min="0" step="0.1" placeholder="Auto — uses tiered margin from Settings" oninput="qtOnQtyChange()">
          <div class="qt-hint">Leave blank to use the tiered margin. If set, it replaces the tiered rate everywhere below — per-unit price, total, and margin/profit figures.</div>
        </div>
      </div>
    </div>

    <!-- Step 2 -->
    <div class="qt-card">
      <div class="qt-card-title">Step 2 — Decorations <span style="font-weight:400;text-transform:none;letter-spacing:0;color:#b0bec5;font-size:.75rem;">Select a type for each location</span></div>
      <div class="qt-loc-grid" id="qt-loc-grid"></div>
      <div class="qt-form-group" style="margin-top:12px;">
        <label for="qt-decoration-desc">Decoration Description <span style="font-weight:400;color:#94a3b8;">(optional — shown on the quote &amp; PDF)</span></label>
        <textarea class="qt-input" id="qt-decoration-desc" rows="2" placeholder="e.g. Full-color heat transfer on front, single-color embroidery on left sleeve" oninput="qtClearResults()" style="resize:vertical;min-height:44px;font-family:inherit;"></textarea>
      </div>
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
        <label style="display:flex;align-items:center;gap:7px;font-size:.78rem;color:#475569;font-weight:600;margin-top:12px;cursor:pointer;">
          <input type="checkbox" id="qt-attach-pdf" style="width:15px;height:15px;cursor:pointer;">
          Attach quote as PDF (downloads a PDF and adds a note to the email — mail apps won't let us attach it automatically)
        </label>
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
# ─── Scheduled Catalog Rebuild (no external dependencies) ────────────────────
def _catalog_scheduler_loop():
    """Background thread — wakes every minute, builds catalog on Monday at 1am PT."""
    print("[SCHEDULER] Weekly catalog rebuild scheduled — Mondays at 1:00 AM PT.")
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-8)))
            if now.weekday() == 0 and now.hour == 1 and now.minute == 0:
                if not _index_status.get('running'):
                    print("[SCHEDULER] Starting weekly catalog rebuild...")
                    t = threading.Thread(target=build_catalog_background, daemon=True)
                    t.start()
                time.sleep(61)   # skip the rest of this minute
            else:
                time.sleep(60)
        except Exception as e:
            print(f"[SCHEDULER] Error: {e}")
            time.sleep(60)

threading.Thread(target=_catalog_scheduler_loop, daemon=True, name='catalog-scheduler').start()

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
