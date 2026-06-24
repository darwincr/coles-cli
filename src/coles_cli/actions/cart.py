from __future__ import annotations

import re
import time

from playwright.sync_api import Error as PlaywrightError

from coles_cli.actions.auth import ensure_logged_in
from coles_cli.browser import clean_url, dismiss_cookie_banner, first_visible, goto_domcontentloaded, normalize_space
from coles_cli.conf import COLES_HOME_URL, DEFAULT_CHECKOUT_TIMEOUT_S
from coles_cli.exceptions import CartError, CheckoutError

CART_BUTTON_LOCATORS = [
    lambda p: p.locator('header button[aria-label*="trolley" i], header button[aria-label*="cart" i]'),
    lambda p: p.locator('header [role="button"][aria-label*="trolley" i], header [role="button"][aria-label*="cart" i]'),
    lambda p: p.locator('header [data-testid*="trolley" i] button, header [data-testid*="cart" i] button'),
    lambda p: p.locator("header button").filter(has_text=re.compile(r"\$\s*\d", re.I)),
    lambda p: p.get_by_role("button", name=re.compile(r"view trolley|open trolley|trolley total|cart total", re.I)),
]

_CART_HELPERS_JS = r"""
const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
const cleanKey = (text) => clean(text).toLowerCase();
const visible = (node) => {
  const rect = node && node.getBoundingClientRect();
  if (!rect || rect.width <= 0 || rect.height <= 0) return false;
  const style = window.getComputedStyle(node);
  return style.visibility !== 'hidden' && style.display !== 'none';
};
const absolute = (href) => {
  try { return new URL(href, location.origin).href; } catch { return href || ''; }
};
const productId = (href) => {
  const match = String(href || '').match(/-(\d+)(?:[/?#]|$)/);
  return match ? match[1] : null;
};

function cartRoot() {
  const selectors = [
    '[data-testid="trolley-drawer"]',
    '[data-testid="drawer-body-trolley-drawer"]',
    '[data-testid="trolley-drawer-content"]',
    '[data-testid*="trolley-drawer" i]',
    '[role="dialog"]',
    '[aria-modal="true"]',
    'aside',
    '[class*="drawer" i]',
  ].join(', ');
  const roots = Array.from(document.querySelectorAll(selectors)).filter(visible);
  let best = null;
  let bestScore = 0;
  for (const node of roots) {
    const text = clean(node.innerText || node.textContent || '');
    const testid = node.getAttribute('data-testid') || '';
    const cls = String(node.className || '');
    const marker = `${testid} ${cls}`;
    if (/add-to-cart|ProductTileAddToCart|product-tiles-btn/i.test(marker)) continue;
    if (/^Product is (not )?in your trolley Add$/i.test(text)) continue;
    if (text.length < 10 && !node.querySelector('button, a, [role="button"]')) continue;
    let score = 0;
    if (/^trolley-drawer$|drawer-body-trolley-drawer|trolley-drawer-content/i.test(testid)) score += 200;
    if (/\bMuiDrawer-paper\b|DrawerDrawerContent|DrawerDrawerBody/i.test(cls)) score += 120;
    if (node.getAttribute('role') === 'dialog' || node.getAttribute('aria-modal') === 'true') score += 80;
    if (/Trolley\s*-\s*\d+\s+items?/i.test(text)) score += 80;
    if (/Your items|Trolley total|Estimated total|Checkout/i.test(text)) score += 60;
    if (node.querySelector('[data-testid^="trolley-productItem-"]')) score += 80;
    score += Math.min(node.querySelectorAll('a[href*="/product/"]').length, 6) * 10;
    if (/empty|no items|nothing in your trolley|your trolley is empty/i.test(text)) score += 40;
    if (!/trolley|checkout|estimated total|your items|empty|no items|nothing in your trolley/i.test(text)) score -= 80;
    if (score > bestScore) {
      best = node;
      bestScore = score;
    }
  }
  return best;
}

function cartSnapshot() {
  const root = cartRoot();
  if (!root) {
    return {open: false, empty: true, total: '', item_count: 0, items: [], checkout_available: false, messages: []};
  }
  const text = clean(root.innerText || root.textContent || '');
  const lines = (root.innerText || '').split(/\n+/).map(clean).filter(Boolean);
  const cards = cartItemCards(root);
  const items = cards.map((card, index) => itemFromCard(card, index + 1));
  const total = cartTotal(lines, text);
  const buttons = Array.from(root.querySelectorAll('button, a, [role="button"]')).filter(visible);
  const checkoutAvailable = buttons.some((button) => /checkout/i.test(clean(`${button.getAttribute('aria-label') || ''} ${button.innerText || ''}`)) && !button.disabled);
  const messages = Array.from(root.querySelectorAll('[role="alert"], .error, .warning')).map((node) => clean(node.innerText || node.textContent || '')).filter(Boolean);
  return {
    open: true,
    empty: items.length === 0 && /empty|no items|nothing in your trolley|don.t have any|your trolley is|is empty|not in your trolley/i.test(text),
    total,
    item_count: items.length,
    items,
    checkout_available: checkoutAvailable,
    messages,
  };
}

function cartItemCards(root) {
  const cards = [];
  const candidates = [
    ...Array.from(root.querySelectorAll('a[href*="/product/"]')),
    ...Array.from(root.querySelectorAll('[data-testid*="item" i], [data-testid*="product" i], [class*="item" i], article, li')),
    ...Array.from(root.querySelectorAll('button, [role="button"], input')).filter(itemControlSignal),
  ];
  for (const node of candidates) {
    const card = usefulCard(node, root);
    if (!card || card === root || !visible(card)) continue;
    const cardText = clean(card.innerText || card.textContent || '');
    if (!itemSignal(card, cardText)) continue;
    if (isSummaryOnly(cardText) && !itemTitle(card)) continue;
    cards.push(card);
  }
  return dedupeCards(cards).map((card, index) => ({card, index})).sort((a, b) => {
    if (a.card === b.card) return 0;
    const pos = a.card.compareDocumentPosition(b.card);
    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
    if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
    return a.index - b.index;
  }).map((entry) => entry.card);
}

function usefulCard(node, root) {
  let current = node;
  let best = null;
  for (let i = 0; i < 8 && current && current !== root; i += 1) {
    const text = clean(current.innerText || current.textContent || '');
    if (text.length > 8 && text.length < 1800 && itemSignal(current, text)) {
      best = current;
      if ((/\$/.test(text) || /qty|quantity|remove|delete/i.test(text)) && itemTitle(current)) return current;
    }
    current = current.parentElement;
  }
  return best;
}

function itemSignal(node, text) {
  if (!text && !node.querySelector('a[href*="/product/"], img[alt]')) return false;
  if (node.querySelector('a[href*="/product/"]')) return true;
  if (node.querySelector('[data-testid*="product" i], [data-testid*="item" i]') && /\$|qty|quantity|remove|delete|each|ea/i.test(text)) return true;
  if (/\$\s*\d|qty|quantity|remove|delete|substitution|each|ea/i.test(text) && (itemTitle(node) || node.querySelector('img[alt]'))) return true;
  return itemControlSignal(node);
}

function itemControlSignal(node) {
  const label = controlLabel(node);
  const marker = clean(`${node.getAttribute?.('data-testid') || ''} ${node.getAttribute?.('class') || ''}`);
  return /remove|delete|trash|qty|quantity|increase|decrease|increment|decrement/i.test(`${label} ${marker}`);
}

function dedupeCards(cards) {
  const seen = new Set();
  const result = [];
  for (const card of cards) {
    if (result.some((existing) => existing === card)) continue;
    const item = itemFromCard(card, result.length + 1);
    if (!item.id && !item.title) continue;
    if (!item.id && !item.price && !item.quantity && (genericControlTitle(item.title) || /\b(remove|delete) items?\b/i.test(item.text))) continue;
    const titleKey = cleanKey(item.title);
    const keys = [];
    if (item.id) keys.push(`id:${item.id}`);
    if (titleKey) keys.push(`title:${titleKey}`);
    if (!keys.length) keys.push(`text:${cleanKey(item.text).slice(0, 120)}`);
    if (keys.some((key) => seen.has(key))) continue;
    for (const key of keys) seen.add(key);
    result.push(card);
  }
  return result;
}

function itemFromCard(card, index) {
  const href = card.querySelector('a[href*="/product/"]')?.getAttribute('href') || '';
  const id = productId(href) || href;
  const cardText = clean(card.innerText || card.textContent || '');
  const prices = Array.from(cardText.matchAll(/\$\s*\d+(?:\.\d{2})?/g)).map((m) => m[0].replace(/\s+/g, ''));
  const quantity = itemQuantity(card, cardText);
  return {
    index,
    id: String(id || ''),
    title: itemTitle(card),
    url: href ? absolute(href) : '',
    quantity,
    price: prices[0] || '',
    line_total: prices.length > 1 ? prices[prices.length - 1] : (prices[0] || ''),
    text: cardText.slice(0, 500),
  };
}

function itemTitle(card) {
  const link = card.querySelector('a[href*="/product/"]');
  let title = clean(link?.innerText || link?.textContent || link?.getAttribute('aria-label') || '');
  if (!title) {
    const titleNode = card.querySelector('[data-testid*="title" i], [class*="title" i], h2, h3');
    title = clean(titleNode?.innerText || titleNode?.textContent || titleNode?.getAttribute?.('aria-label') || '');
  }
  if (!title) title = clean(card.querySelector('img[alt]')?.getAttribute('alt') || '');
  if (!title) title = titleFromControls(card);
  if (!title) title = titleFromLines(card);
  const normalized = dedupeRepeatedTitle(normalizeTitle(title));
  return genericControlTitle(normalized) ? '' : normalized;
}

function titleFromControls(card) {
  const controls = Array.from(card.querySelectorAll('button, [role="button"], input')).map(controlLabel).filter(Boolean);
  for (const label of controls) {
    const normalized = normalizeTitle(label);
    if (normalized && !genericControlTitle(normalized) && !/^(add|remove|delete|increase|decrease|quantity|qty|plus|minus)$/i.test(normalized)) return normalized;
  }
  return '';
}

function titleFromLines(card) {
  const lines = (card.innerText || '').split(/\n+/).map(clean).filter(Boolean);
  for (const line of lines) {
    if (line.length < 3) continue;
    if (/^\$|\$\s*\d|qty|quantity|remove|delete|increase|decrease|subtotal|estimated|checkout|special|save|was|now|each|ea|per\s+\d|\/\s*\d|^\d+(?:\.\d+)?$|^[-+]$|^add$/i.test(line)) continue;
    return line;
  }
  return '';
}

function normalizeTitle(value) {
  let title = clean(value);
  if (/^(remove|delete)(\s+items?)?$/i.test(title)) return '';
  title = title.replace(/^(remove|delete|increase quantity|decrease quantity|increase|decrease|add|subtract|minus|plus)(\s+(quantity|items?))?\s*(for|of|from|to)?\s*/i, '');
  title = title.replace(/\s+(from|to|in)\s+(your\s+)?(trolley|cart).*$/i, '');
  title = title.replace(/\s+quantity\s+is\s+\d+(?:\.\d+)?.*$/i, '');
  title = title.replace(/[.\s]+(remove|delete)(\s+items?)?$/i, '');
  return clean(title);
}

function genericControlTitle(title) {
  return /^(s|items?|remove|delete)(\s+items?)?$/i.test(clean(title));
}

function itemQuantity(card, text) {
  const input = card.querySelector('input[aria-label*="quantity" i], input[name*="quantity" i], input[type="number"]');
  const inputValue = clean(input?.value || input?.getAttribute?.('aria-valuenow') || input?.getAttribute?.('value') || '');
  if (/^\d+(?:\.\d+)?$/.test(inputValue)) return inputValue;
  const combined = clean(`${text || ''} ${Array.from(card.querySelectorAll('button, [role="button"], input')).map(controlLabel).join(' ')}`);
  const match = combined.match(/quantity\s+is\s+(\d+(?:\.\d+)?)/i) || combined.match(/(?:qty|quantity)\s*:?\s*(\d+(?:\.\d+)?)/i) || combined.match(/(\d+(?:\.\d+)?)\s*x\s*\$/i);
  return match ? match[1] : '';
}

function removeCartItem(targetIndex) {
  const root = cartRoot();
  if (!root) return {clicked: false, reason: 'cart is not open'};
  const cards = cartItemCards(root);
  const card = cards[targetIndex - 1];
  if (!card) return {clicked: false, reason: `cart item index ${targetIndex} was not found`, item_count: cards.length};
  const item = itemFromCard(card, targetIndex);
  const control = removeControl(card);
  if (!control) return {clicked: false, reason: `remove control was not found for cart item ${targetIndex}`, item};
  const label = controlLabel(control) || clean(control.innerText || control.textContent || control.getAttribute('data-testid') || 'remove');
  control.scrollIntoView({block: 'center', inline: 'center'});
  control.click();
  return {clicked: true, label, item};
}

function clickCartQuantity(targetIndex, action) {
  const root = cartRoot();
  if (!root) return {clicked: false, reason: 'cart is not open'};
  const cards = cartItemCards(root);
  const card = cards[targetIndex - 1];
  if (!card) return {clicked: false, reason: `cart item index ${targetIndex} was not found`, item_count: cards.length};
  const item = itemFromCard(card, targetIndex);
  const control = quantityControl(card, action);
  if (!control) return {clicked: false, reason: `${action} quantity control was not found for cart item ${targetIndex}`, item};
  const label = controlLabel(control) || clean(control.innerText || control.textContent || control.getAttribute('data-testid') || action);
  control.scrollIntoView({block: 'center', inline: 'center'});
  control.click();
  return {clicked: true, label, item};
}

function removeControl(card) {
  const controls = Array.from(card.querySelectorAll('button, [role="button"], a, input[type="button"], input[type="submit"]')).filter(visible);
  const removable = controls.find((node) => {
    if (node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    return /\b(remove|delete)\b|trash/i.test(`${controlLabel(node)} ${node.getAttribute('data-testid') || ''} ${node.getAttribute('class') || ''}`);
  });
  if (removable) return removable;
  const quantity = Number.parseFloat(itemQuantity(card, clean(card.innerText || card.textContent || '')) || '1');
  if (quantity <= 1) {
    return controls.find((node) => {
      if (node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
      return /decrease|decrement|minus|subtract/i.test(`${controlLabel(node)} ${node.getAttribute('data-testid') || ''} ${node.getAttribute('class') || ''}`);
    }) || null;
  }
  return null;
}

function quantityControl(card, action) {
  const pattern = action === 'increase' ? /increase|increment|plus|add|plus-btn/i : /decrease|decrement|minus|subtract|minus-btn/i;
  const controls = Array.from(card.querySelectorAll('button, [role="button"], a, input[type="button"], input[type="submit"]')).filter(visible);
  return controls.find((node) => {
    if (node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
    return pattern.test(`${controlLabel(node)} ${node.getAttribute('data-testid') || ''} ${node.getAttribute('class') || ''}`);
  }) || null;
}

function confirmRemoveDialog() {
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal" i]')).filter(visible);
  for (const dialog of dialogs) {
    const text = clean(dialog.innerText || dialog.textContent || '');
    if (!/remove (this )?item|delete (this )?item|are you sure|confirm removal|remove .{1,120} from/i.test(text)) continue;
    if (/checkout|estimated total|trolley total/i.test(text)) continue;
    const button = Array.from(dialog.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]')).filter(visible).find((node) => {
      if (node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
      return /^(remove|delete|yes|confirm)$|remove item|delete item/i.test(controlLabel(node));
    });
    if (!button) continue;
    const label = controlLabel(button);
    button.click();
    return label || 'confirm';
  }
  return '';
}

function controlLabel(node) {
  return clean(`${node.getAttribute?.('aria-label') || ''} ${node.getAttribute?.('title') || ''} ${node.value || ''} ${node.innerText || node.textContent || ''}`);
}

function isSummaryOnly(text) {
  return /estimated total|trolley total|subtotal|minimum order|checkout|delivery|pickup/i.test(text) && !/remove|delete|qty|quantity|product/i.test(text);
}

function cartTotal(lines, fallbackText) {
  const labels = [/^Estimated total/i, /^Trolley total$/i, /^Trolley total\s*\(/i, /^Subtotal/i];
  for (let i = 0; i < lines.length; i += 1) {
    if (/Minimum order/i.test(lines[i])) continue;
    if (!labels.some((regex) => regex.test(lines[i]))) continue;
    const sameLine = lines[i].match(/\$\s*\d+(?:\.\d{2})?/);
    if (sameLine) return sameLine[0].replace(/\s+/g, '');
    for (const next of lines.slice(i + 1, i + 4)) {
      if (/Minimum order/i.test(next)) continue;
      const match = next.match(/\$\s*\d+(?:\.\d{2})?/);
      if (match) return match[0].replace(/\s+/g, '');
    }
  }
  const all = fallbackText.match(/\$\s*\d+(?:\.\d{2})?/g) || [];
  return all.length ? all[all.length - 1].replace(/\s+/g, '') : '';
}

function dedupeRepeatedTitle(value) {
  const words = clean(value).split(' ').filter(Boolean);
  if (words.length > 0 && words.length % 2 === 0) {
    const half = words.length / 2;
    const first = words.slice(0, half).join(' ');
    const second = words.slice(half).join(' ');
    if (first === second) return first;
  }
  return clean(value);
}
"""


def list_cart(session) -> dict:
    page = session.page
    if page.url == "about:blank":
        goto_domcontentloaded(page, COLES_HOME_URL)
    dismiss_cookie_banner(page)
    open_cart(page)
    cart = extract_cart(page)
    return {"ok": True, "cart": public_cart(cart)}


def remove_cart_item(session, *, index: int) -> dict:
    if index < 1:
        raise ValueError("--index must be 1 or greater")

    page = session.page
    if page.url == "about:blank":
        goto_domcontentloaded(page, COLES_HOME_URL)
    dismiss_cookie_banner(page)
    open_cart(page)
    before = extract_cart(page)
    items = before.get("items") or []
    if before.get("empty") or not items:
        raise CartError("The Coles trolley is empty; there is nothing to remove")
    if index > len(items):
        raise CartError(f"Cart item index {index} is out of range; only found {len(items)} item(s)")

    clicked = _click_remove_item(page, index=index)
    page.wait_for_timeout(700)
    confirm_label = _confirm_remove_if_needed(page)
    if confirm_label:
        page.wait_for_timeout(700)

    target = clicked.get("item") or items[index - 1]
    deadline = time.monotonic() + 12
    after = extract_cart(page)
    while time.monotonic() < deadline:
        if _cart_item_removed(before, after, target):
            return {
                "ok": True,
                "removed": True,
                "removed_item": public_cart_item(target),
                "action": clicked.get("label"),
                "confirmation": confirm_label,
                "cart": public_cart(after),
            }
        page.wait_for_timeout(500)
        after = extract_cart(page)

    raise CartError(f"Clicked remove for cart item {index}, but the trolley did not update")


def set_cart_item_quantity(session, *, index: int, quantity: int) -> dict:
    if index < 1:
        raise ValueError("--index must be 1 or greater")
    if quantity < 1:
        raise ValueError("--quantity must be 1 or greater")

    page = session.page
    if page.url == "about:blank":
        goto_domcontentloaded(page, COLES_HOME_URL)
    dismiss_cookie_banner(page)
    open_cart(page)
    cart = extract_cart(page)
    items = cart.get("items") or []
    if cart.get("empty") or not items:
        raise CartError("The Coles trolley is empty; there is nothing to update")
    if index > len(items):
        raise CartError(f"Cart item index {index} is out of range; only found {len(items)} item(s)")

    actions = []
    deadline = time.monotonic() + max(12, quantity * 4)
    current = _quantity_int(items[index - 1].get("quantity")) or 0
    target_item = items[index - 1]
    while current != quantity and time.monotonic() < deadline:
        action = "increase" if current < quantity else "decrease"
        clicked = _click_cart_quantity(page, index=index, action=action)
        actions.append(clicked.get("label") or action)
        page.wait_for_timeout(700)
        cart = extract_cart(page)
        items = cart.get("items") or []
        if len(items) < index:
            break
        target_item = items[index - 1]
        current = _quantity_int(target_item.get("quantity")) or current

    if current != quantity:
        raise CartError(f"Could not set cart item {index} quantity to {quantity}; current quantity is {current or 'unknown'}")

    return {
        "ok": True,
        "updated": True,
        "index": index,
        "quantity": current,
        "item": public_cart_item(target_item),
        "actions": actions,
        "cart": public_cart(cart),
    }


def checkout(session, *, timeout: int = DEFAULT_CHECKOUT_TIMEOUT_S) -> dict:
    account = ensure_logged_in(session)
    page = session.page
    open_cart(page)
    initial_cart = extract_cart(page)
    if initial_cart.get("empty"):
        raise CheckoutError("The Coles trolley is empty; add products before checkout")

    clicked = _click_action(page, [r"^checkout$", r"go to checkout", r"proceed to checkout"])
    if not clicked:
        raise CheckoutError("Could not find a visible Coles checkout button in the trolley")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    deadline = time.monotonic() + timeout
    steps = [{"action": clicked, "url": clean_url(page.url)}]
    while time.monotonic() < deadline:
        state = _checkout_state(page)
        if state.get("placed"):
            return {
                "ok": True,
                "placed": True,
                "account": account,
                "initial_cart": public_cart(initial_cart),
                "checkout_state": state,
                "steps": steps,
                "next_command": "coles orders list --status current",
                "message": "Order placed. Retrieve it with `coles orders list --status current`.",
                "url": clean_url(page.url),
            }

        blocker = _checkout_blocker(page)
        if blocker:
            raise CheckoutError(blocker)

        final = _click_action(page, [r"^place order$", r"place your order", r"^submit order$", r"^confirm order$", r"finalise order"])
        if final:
            steps.append({"action": final, "url": clean_url(page.url), "final": True})
            page.wait_for_timeout(2500)
            continue

        progressed = _click_action(
            page,
            [
                r"^continue$",
                r"continue to",
                r"next",
                r"save and continue",
                r"review order",
                r"continue checkout",
                r"continue to payment",
                r"confirm details",
            ],
        )
        if progressed:
            steps.append({"action": progressed, "url": clean_url(page.url)})
            page.wait_for_timeout(1500)
            continue

        page.wait_for_timeout(1000)

    state = _checkout_state(page)
    raise CheckoutError(f"Checkout did not complete within {timeout} seconds; current state: {state.get('summary') or page.url}")


def open_cart(page) -> None:
    dismiss_cookie_banner(page)
    button = first_visible(page, CART_BUTTON_LOCATORS, timeout_ms=1200)
    if button is None:
        goto_domcontentloaded(page, COLES_HOME_URL)
        dismiss_cookie_banner(page)
        button = first_visible(page, CART_BUTTON_LOCATORS, timeout_ms=3000)
    if button is None:
        raise CartError("Could not find the Coles trolley/cart button")
    try:
        button.click(timeout=5000)
    except PlaywrightError as exc:
        raise CartError(f"Could not open the Coles trolley/cart: {exc}") from exc
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _cart_is_open(page):
            return
        page.wait_for_timeout(250)


def extract_cart(page) -> dict:
    try:
        return page.evaluate(f"() => {{ {_CART_HELPERS_JS} return cartSnapshot(); }}")
    except PlaywrightError as exc:
        raise CartError(f"Could not read Coles trolley/cart: {exc}") from exc


def public_cart(cart: dict) -> dict:
    public = dict(cart or {})
    public["items"] = [public_cart_item(item) for item in public.get("items") or []]
    return public


def public_cart_item(item: dict) -> dict:
    return {key: value for key, value in (item or {}).items() if key not in {"id", "url", "text"}}


def _click_remove_item(page, *, index: int) -> dict:
    try:
        clicked = page.evaluate(f"(targetIndex) => {{ {_CART_HELPERS_JS} return removeCartItem(targetIndex); }}", index)
    except PlaywrightError as exc:
        raise CartError(f"Could not click remove for cart item {index}: {exc}") from exc
    if not clicked or not clicked.get("clicked"):
        raise CartError(f"Could not click remove for cart item {index}: {(clicked or {}).get('reason') or 'unknown reason'}")
    return clicked


def _click_cart_quantity(page, *, index: int, action: str) -> dict:
    try:
        clicked = page.evaluate(f"(targetIndex) => {{ {_CART_HELPERS_JS} return clickCartQuantity(targetIndex, {action!r}); }}", index)
    except PlaywrightError as exc:
        raise CartError(f"Could not click {action} quantity for cart item {index}: {exc}") from exc
    if not clicked or not clicked.get("clicked"):
        raise CartError(f"Could not click {action} quantity for cart item {index}: {(clicked or {}).get('reason') or 'unknown reason'}")
    return clicked


def _confirm_remove_if_needed(page) -> str:
    try:
        return page.evaluate(f"() => {{ {_CART_HELPERS_JS} return confirmRemoveDialog(); }}") or ""
    except PlaywrightError:
        return ""


def _cart_item_removed(before: dict, after: dict, target: dict) -> bool:
    before_items = before.get("items") or []
    after_items = after.get("items") or []
    if after.get("empty") or len(after_items) < len(before_items):
        return True
    target_id = target.get("id")
    if target_id and not any(item.get("id") == target_id for item in after_items):
        return True
    target_title = normalize_space(target.get("title")).casefold()
    if target_title and not any(normalize_space(item.get("title")).casefold() == target_title for item in after_items):
        return True
    return False


def _quantity_int(value) -> int | None:
    text = normalize_space(value)
    return int(text) if text.isdigit() else None


def _cart_is_open(page) -> bool:
    try:
        return bool(page.evaluate(f"() => {{ {_CART_HELPERS_JS} return !!cartRoot(); }}"))
    except PlaywrightError:
        return False


def _click_action(page, patterns: list[str]) -> str | None:
    try:
        return page.evaluate(
            r"""
            (patterns) => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const regexes = patterns.map((pattern) => new RegExp(pattern, 'i'));
              const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]')).filter(visible);
              for (const node of nodes) {
                if (node.disabled || node.getAttribute('aria-disabled') === 'true') continue;
                const label = clean(`${node.getAttribute('aria-label') || ''} ${node.value || ''} ${node.innerText || node.textContent || ''}`);
                if (!label || !regexes.some((regex) => regex.test(label))) continue;
                node.scrollIntoView({block: 'center', inline: 'center'});
                node.click();
                return label;
              }
              return null;
            }
            """,
            patterns,
        )
    except PlaywrightError:
        return None


def _checkout_state(page) -> dict:
    try:
        return page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const text = clean(document.body.innerText || '');
              const orderMatch = text.match(/(?:order(?: number| no\.?| #)?|confirmation(?: number)?)[^0-9]{0,30}(\d{6,})/i);
              const placed = /order confirmed|order has been placed|thanks[^.]{0,80}order|thank you[^.]{0,80}order|we.ve received your order/i.test(text);
              return {
                placed,
                order_id: orderMatch ? orderMatch[1] : null,
                summary: text.slice(0, 1000),
              };
            }
            """
        ) | {"url": clean_url(page.url)}
    except PlaywrightError:
        return {"placed": False, "order_id": None, "summary": "", "url": clean_url(page.url)}


def _checkout_blocker(page) -> str | None:
    try:
        blocker = page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const candidates = Array.from(document.querySelectorAll('[role="alert"], .error, .warning, [data-testid*="error" i], [data-testid*="warning" i]')).filter(visible);
              for (const node of candidates) {
                const text = clean(node.innerText || node.textContent || '');
                if (text && /payment|card|address|delivery|pickup|unavailable|out of stock|error|cannot|unable|failed|required|login|sign in|3d secure|verification/i.test(text)) return text;
              }
              const body = clean(document.body.innerText || '');
              const match = body.match(/((?:payment|card|address|delivery|pickup|unavailable|out of stock|required|3d secure|verification)[^.]{0,220})/i);
              return match ? match[1] : '';
            }
            """
        )
    except PlaywrightError:
        return None
    return normalize_space(blocker) or None
