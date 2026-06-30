from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Error as PlaywrightError

from coles_cli.browser import dismiss_cookie_banner, goto_domcontentloaded, next_data, normalize_space
from coles_cli.conf import COLES_BASE_URL
from coles_cli.exceptions import CartError, InteractiveAuthenticationRequired, ProductNotFoundError


def search_url(query: str, *, page: int = 1) -> str:
    params = {"q": query}
    if page > 1:
        params["page"] = page
    return f"{COLES_BASE_URL}/search/products?{urlencode(params)}"


def search_products(session, query: str, *, result_size: int = 48) -> dict:
    query = normalize_space(query)
    if not query:
        raise ValueError("Search query cannot be empty")
    if result_size < 1:
        raise ValueError("--result-size must be 1 or greater")
    page = session.page
    products = []
    current_page = 1
    while len(products) < result_size:
        goto_domcontentloaded(page, search_url(query, page=current_page))
        dismiss_cookie_banner(page)
        _wait_for_search_results(page)
        page_products = _extract_products(page)
        if not page_products:
            break
        for product in page_products:
            if len(products) >= result_size:
                break
            item = dict(product)
            item["index"] = len(products) + 1
            products.append(item)
        if len(page_products) < 48:
            break
        current_page += 1
    return {"ok": True, "query": query, "result_count": len(products), "products": public_products(products)}


def add_product_to_cart(session, query: str, *, index: int, quantity: int | None = None) -> dict:
    query = normalize_space(query)
    if not query:
        raise ValueError("Search query cannot be empty")
    if index < 1:
        raise ValueError("--index must be 1 or greater")
    if quantity is not None and quantity < 1:
        raise ValueError("--set-quantity must be 1 or greater")

    page = session.page
    reused = _current_search_matches(page.url, query)
    if not reused:
        goto_domcontentloaded(page, search_url(query))
        dismiss_cookie_banner(page)
    _wait_for_search_results(page)

    products = _extract_products(page)
    if not products:
        raise ProductNotFoundError(f"No product results found for {query!r}")
    if index > len(products):
        raise ProductNotFoundError(f"Product index {index} is out of range; only found {len(products)} result(s)")

    selected = products[index - 1]
    quantity_state = _set_product_quantity(page, index=index, quantity=quantity)
    target_quantity = _quantity_int(quantity_state.get("quantity")) or quantity or 1
    page.wait_for_timeout(1200)
    updated_products = _extract_products(page)
    updated = updated_products[index - 1] if len(updated_products) >= index else selected
    cart = _header_trolley_summary(page)
    state = _post_add_state(page)
    if state.get("login_required"):
        raise InteractiveAuthenticationRequired(state.get("message") or "Coles requires login before adding this item to the trolley")
    updated_quantity = _quantity_int((updated or {}).get("quantity")) or _quantity_int(quantity_state.get("quantity")) or 0
    if state.get("shopping_method_required") and updated_quantity >= target_quantity and not state.get("message"):
        state = state | {"shopping_method_required": False}
    if state.get("shopping_method_required") and updated_quantity < target_quantity:
        raise CartError(state.get("message") or "Coles is asking for a shopping method before the item can be added")
    added = bool(updated_quantity >= target_quantity or updated.get("in_trolley") or (cart.get("total") and cart.get("total") != "$0.00"))
    return {
        "ok": True,
        "added": added,
        "query": query,
        "reused_current_search": reused,
        "index": index,
        "target_quantity": target_quantity,
        "cart_quantity": updated_quantity or quantity_state.get("quantity"),
        "quantity_actions": quantity_state.get("actions") or [],
        "product": public_product(updated or selected),
        "cart": cart,
        "state": state,
    }


def public_products(products: list[dict]) -> list[dict]:
    return [public_product(product) for product in products]


def public_product(product: dict) -> dict:
    public = {key: value for key, value in (product or {}).items() if key in {"index", "title", "price", "unit_price", "available", "in_trolley"}}
    public["cart_quantity"] = (product or {}).get("quantity", "")
    return public


def _wait_for_search_results(page) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if page.locator('[data-testid="product-tile"], [data-testid="search-results"], #coles-targeting-search-content-container').count() > 0:
                return
        except PlaywrightError:
            pass
        page.wait_for_timeout(250)


def _current_search_matches(url: str, query: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != "www.coles.com.au":
        return False
    if parsed.path.rstrip("/") != "/search/products":
        return False
    current = parse_qs(parsed.query).get("q", [""])[0]
    return _query_key(current) == _query_key(query)


def _query_key(value: str) -> str:
    return normalize_space(value).casefold()


def _extract_products(page) -> list[dict]:
    products = _extract_products_from_dom(page)
    if products:
        return products
    return _extract_products_from_next_data(page)


def _extract_products_from_dom(page) -> list[dict]:
    try:
        return page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const absolute = (href) => {
                try { return new URL(href, location.origin).href; } catch { return href || ''; }
              };
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const productId = (href) => {
                const match = String(href || '').match(/-(\d+)(?:[/?#]|$)/);
                return match ? match[1] : null;
              };
              const tiles = Array.from(document.querySelectorAll('[data-testid="product-tile"], section.list-item, article')).filter((tile) => {
                return tile.querySelector('a[href*="/product/"]');
              });
              const seen = new Set();
              return tiles.map((tile, index) => {
                const titleEl = tile.querySelector('.product__title, h2, h3, a[href*="/product/"][aria-label]');
                const linkEl = titleEl?.closest?.('a[href*="/product/"]') || tile.querySelector('a[href*="/product/"]');
                const href = linkEl?.getAttribute('href') || '';
                const id = productId(href);
                const key = id || href || String(index);
                if (seen.has(key)) return null;
                seen.add(key);
                const buttons = Array.from(tile.querySelectorAll('button'));
                const addButton = buttons.find((node) => {
                  const label = clean(`${node.getAttribute('aria-label') || ''} ${node.innerText || ''}`);
                  return node.matches('[data-testid="add-to-cart-button"]') || /add to (trolley|cart)/i.test(label) || (/^add$/i.test(label) && !/list|save/i.test(label));
                });
                const quantity = productQuantity(tile);
                const image = tile.querySelector('img');
                const status = clean(tile.querySelector('[role="status"]')?.innerText || '');
                const text = clean(tile.innerText || tile.textContent || '');
                const title = clean(titleEl?.innerText || titleEl?.textContent || linkEl?.getAttribute('aria-label') || '');
                const price = clean(tile.querySelector('[data-testid="product-pricing"], .price__value')?.innerText || '');
                const unitPrice = clean(tile.querySelector('.price__calculation_method')?.innerText || '');
                const addLabel = clean(addButton?.getAttribute('aria-label') || addButton?.innerText || '');
                return {
                  index: seen.size,
                  id,
                  title,
                  url: absolute(href),
                  price,
                  unit_price: unitPrice,
                  image: image?.currentSrc || image?.src || '',
                  available: !/unavailable|out of stock|not available/i.test(text) && !(addButton && addButton.disabled),
                  in_trolley: !!quantity || (/in your trolley/i.test(status) && !/not in your trolley/i.test(status)) || /\badded\b/i.test(status),
                  quantity: quantity ? String(quantity) : '',
                  status,
                  add_label: addLabel,
                };
              }).filter(Boolean).map((product, index) => ({...product, index: index + 1}));

              function productQuantity(tile) {
                const input = tile.querySelector('[data-testid="quantity-input"], input[type="number"][aria-label*="Quantity" i], input[name*="quantity" i]');
                const raw = clean(input?.value || input?.getAttribute?.('value') || input?.getAttribute?.('aria-valuenow') || '');
                if (/^\d+$/.test(raw)) return Number.parseInt(raw, 10);
                const status = clean(tile.querySelector('[role="status"]')?.innerText || '');
                const text = clean(`${status} ${tile.innerText || tile.textContent || ''}`);
                const match = text.match(/(?:quantity\s+is\s+|qty\s*:?\s*)(\d+)/i) || text.match(/(^|\b)(\d+)\b[^.]{0,80}\badded\b/i);
                if (!match) return 0;
                return Number.parseInt(match[2] || match[1], 10) || 0;
              }
            }
            """
        )
    except PlaywrightError:
        return []


def _extract_products_from_next_data(page) -> list[dict]:
    data = next_data(page)
    results = (((data.get("props") or {}).get("pageProps") or {}).get("searchResults") or {}).get("results") or []
    products = []
    for idx, item in enumerate(results, start=1):
        if item.get("_type") and item.get("_type") != "PRODUCT":
            continue
        brand = normalize_space(item.get("brand"))
        name = normalize_space(item.get("name"))
        size = normalize_space(item.get("size"))
        title = normalize_space(" ".join(part for part in (brand, name, size) if part))
        pricing = item.get("pricing") or {}
        now = pricing.get("now")
        products.append(
            {
                "index": len(products) + 1,
                "id": str(item.get("id") or ""),
                "title": title or normalize_space(item.get("description")),
                "url": None,
                "price": f"${now:.2f}" if isinstance(now, int | float) else "",
                "unit_price": normalize_space(pricing.get("comparable")),
                "image": _next_image_url(item),
                "available": bool(item.get("availability")),
                "in_trolley": False,
                "quantity": "",
                "status": "",
                "add_label": "",
            }
        )
    return products


def _next_image_url(item: dict) -> str:
    images = item.get("imageUris") or []
    if not images:
        return ""
    uri = images[0].get("uri") or ""
    if not uri:
        return ""
    if uri.startswith("http"):
        return uri
    return f"https://cdn.productimages.coles.com.au/productimages{uri}"


def _click_add_button(page, *, index: int) -> None:
    try:
        tiles = page.locator('[data-testid="product-tile"], section.list-item')
        if tiles.count() >= index:
            tile = tiles.nth(index - 1)
            button = tile.locator('[data-testid="add-to-cart-button"], button[aria-label*="Add to trolley" i], button[aria-label*="Add to cart" i]').first
            button.scroll_into_view_if_needed(timeout=5000)
            button.click(timeout=10000)
            return
    except PlaywrightError:
        pass

    try:
        clicked = page.evaluate(
            r"""
            (targetIndex) => {
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const tiles = Array.from(document.querySelectorAll('[data-testid="product-tile"], section.list-item, article')).filter((tile) => tile.querySelector('a[href*="/product/"]'));
              const tile = tiles[targetIndex];
              if (!tile) return {clicked: false, reason: 'product tile not found'};
              const buttons = Array.from(tile.querySelectorAll('button'));
              const button = buttons.find((node) => {
                if (!visible(node) || node.disabled) return false;
                const label = `${node.getAttribute('aria-label') || ''} ${node.innerText || ''}`.replace(/\s+/g, ' ').trim();
                return node.matches('[data-testid="add-to-cart-button"]') || /add to (trolley|cart)/i.test(label) || (/^add$/i.test(label) && !/list|save/i.test(label));
              });
              if (!button) return {clicked: false, reason: 'add button not found'};
              button.scrollIntoView({block: 'center', inline: 'center'});
              button.click();
              return {clicked: true};
            }
            """,
            index - 1,
        )
    except PlaywrightError as exc:
        raise CartError(f"Could not click add-to-trolley for product index {index}: {exc}") from exc
    if not clicked or not clicked.get("clicked"):
        raise CartError(f"Could not click add-to-trolley for product index {index}: {(clicked or {}).get('reason') or 'unknown reason'}")


def _set_product_quantity(page, *, index: int, quantity: int | None) -> dict:
    actions = []
    state = _product_tile_state(page, index=index)
    current = _quantity_int(state.get("quantity")) or 0
    if quantity is None and current > 0:
        target = current + 1
        _click_quantity_button(page, index=index, action="increase")
        actions.append("increase")
        state = _wait_for_product_quantity(page, index=index, target=target)
        current = _quantity_int(state.get("quantity")) or current
        if current != target:
            raise CartError(f"Could not increment product index {index} quantity to {target}; current quantity is {current or 'unknown'}")
        return {"quantity": current, "actions": actions}

    if current == 0:
        _click_add_button(page, index=index)
        actions.append("add")
        state = _wait_for_product_quantity(page, index=index, minimum=1)
        current = _quantity_int(state.get("quantity")) or 0

    if quantity is None:
        return {"quantity": current, "actions": actions}

    deadline = time.monotonic() + max(10, quantity * 4)
    while current != quantity and time.monotonic() < deadline:
        action = "increase" if current < quantity else "decrease"
        _click_quantity_button(page, index=index, action=action)
        actions.append(action)
        state = _wait_for_product_quantity(page, index=index, target=current + (1 if action == "increase" else -1))
        current = _quantity_int(state.get("quantity")) or current

    if current != quantity:
        raise CartError(f"Could not set product index {index} quantity to {quantity}; current quantity is {current or 'unknown'}")
    return {"quantity": current, "actions": actions}


def _wait_for_product_quantity(page, *, index: int, target: int | None = None, minimum: int | None = None) -> dict:
    deadline = time.monotonic() + 8
    state = _product_tile_state(page, index=index)
    while time.monotonic() < deadline:
        current = _quantity_int(state.get("quantity")) or 0
        if target is not None and current == target:
            return state
        if minimum is not None and current >= minimum:
            return state
        page.wait_for_timeout(300)
        state = _product_tile_state(page, index=index)
    return state


def _product_tile_state(page, *, index: int) -> dict:
    try:
        return page.evaluate(
            r"""
            (targetIndex) => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                const style = window.getComputedStyle(node);
                return style.visibility !== 'hidden' && style.display !== 'none';
              };
              const tiles = Array.from(document.querySelectorAll('[data-testid="product-tile"], section.list-item, article')).filter((tile) => tile.querySelector('a[href*="/product/"]'));
              const tile = tiles[targetIndex - 1];
              if (!tile) return {found: false, quantity: 0, status: '', item_count: tiles.length};
              const status = clean(tile.querySelector('[role="status"]')?.innerText || '');
              const quantity = productQuantity(tile, status);
              const controls = Array.from(tile.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).filter(visible);
              const controlState = (regex) => {
                const node = controls.find((control) => regex.test(controlLabel(control)) || regex.test(clean(`${control.getAttribute('data-testid') || ''} ${control.getAttribute('class') || ''}`)));
                return {exists: !!node, disabled: !!node && (node.disabled || node.getAttribute('aria-disabled') === 'true')};
              };
              return {
                found: true,
                quantity,
                status,
                in_trolley: quantity > 0 || (/in your trolley/i.test(status) && !/not in your trolley/i.test(status)) || /\badded\b/i.test(status),
                add: controlState(/add to (trolley|cart)|^add$|add-to-cart-button/i),
                increase: controlState(/increase|increment|plus|plus-btn/i),
                decrease: controlState(/decrease|decrement|minus|minus-btn/i),
              };

              function productQuantity(tile, status) {
                const input = tile.querySelector('[data-testid="quantity-input"], input[type="number"][aria-label*="Quantity" i], input[name*="quantity" i]');
                const raw = clean(input?.value || input?.getAttribute?.('value') || input?.getAttribute?.('aria-valuenow') || '');
                if (/^\d+$/.test(raw)) return Number.parseInt(raw, 10);
                const text = clean(`${status || ''} ${tile.innerText || tile.textContent || ''}`);
                const match = text.match(/(?:quantity\s+is\s+|qty\s*:?\s*)(\d+)/i) || text.match(/(^|\b)(\d+)\b[^.]{0,80}\badded\b/i);
                if (!match) return 0;
                return Number.parseInt(match[2] || match[1], 10) || 0;
              }

              function controlLabel(node) {
                return clean(`${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''} ${node.value || ''} ${node.innerText || node.textContent || ''}`);
              }
            }
            """,
            index,
        )
    except PlaywrightError:
        return {"found": False, "quantity": 0, "status": ""}


def _click_quantity_button(page, *, index: int, action: str) -> None:
    try:
        clicked = page.evaluate(
            r"""
            ({targetIndex, action}) => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                const style = window.getComputedStyle(node);
                return style.visibility !== 'hidden' && style.display !== 'none';
              };
              const tiles = Array.from(document.querySelectorAll('[data-testid="product-tile"], section.list-item, article')).filter((tile) => tile.querySelector('a[href*="/product/"]'));
              const tile = tiles[targetIndex - 1];
              if (!tile) return {clicked: false, reason: 'product tile not found'};
              const pattern = action === 'increase' ? /increase|increment|plus|plus-btn/i : /decrease|decrement|minus|minus-btn/i;
              const buttons = Array.from(tile.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).filter(visible);
              const button = buttons.find((node) => {
                if (node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
                return pattern.test(controlLabel(node)) || pattern.test(clean(`${node.getAttribute('data-testid') || ''} ${node.getAttribute('class') || ''}`));
              });
              if (!button) return {clicked: false, reason: `${action} button not found`};
              button.scrollIntoView({block: 'center', inline: 'center'});
              button.click();
              return {clicked: true, label: controlLabel(button)};

              function controlLabel(node) {
                return clean(`${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''} ${node.value || ''} ${node.innerText || node.textContent || ''}`);
              }
            }
            """,
            {"targetIndex": index, "action": action},
        )
    except PlaywrightError as exc:
        raise CartError(f"Could not click {action} quantity for product index {index}: {exc}") from exc
    if not clicked or not clicked.get("clicked"):
        raise CartError(f"Could not click {action} quantity for product index {index}: {(clicked or {}).get('reason') or 'unknown reason'}")


def _quantity_int(value) -> int | None:
    if value is None:
        return None
    text = normalize_space(value)
    return int(text) if text.isdigit() else None


def _post_add_state(page) -> dict:
    try:
        state = page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], aside, [data-testid*="drawer" i]')).filter(visible);
              const dialogText = clean(dialogs.map((node) => node.innerText || node.textContent || '').find(Boolean) || '');
              const bodyText = clean(document.body.innerText || '');
              const text = dialogText || bodyText;
              return {
                shopping_method_required: /set shopping method|choose.*(delivery|pickup)|select.*(delivery|pickup)|postcode|suburb/i.test(dialogText) || (/choose.*(delivery|pickup)|select.*(delivery|pickup)/i.test(bodyText) && /before|required/i.test(bodyText)),
                login_required: /log in|login|sign in|sign up/i.test(text) && /start shopping|continue|checkout|account|before/i.test(text),
                message: dialogText.slice(0, 500),
              };
            }
            """
        )
    except PlaywrightError:
        return {"shopping_method_required": False, "login_required": False, "message": ""}
    return state or {"shopping_method_required": False, "login_required": False, "message": ""}


def _header_trolley_summary(page) -> dict:
    try:
        return page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible);
              const node = buttons.find((button) => /trolley|cart|\$\d/i.test(`${button.getAttribute('aria-label') || ''} ${button.innerText || ''}`));
              const text = clean(node?.innerText || node?.getAttribute('aria-label') || '');
              const total = (text.match(/\$\s*\d+(?:\.\d{2})?/) || [''])[0].replace(/\s+/g, '');
              return {text, total};
            }
            """
        )
    except PlaywrightError:
        return {"text": "", "total": ""}
