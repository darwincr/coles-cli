from __future__ import annotations

import re
import time

from playwright.sync_api import Error as PlaywrightError

from coles_cli.actions.auth import ensure_logged_in
from coles_cli.browser import clean_url, dismiss_cookie_banner, goto_domcontentloaded, normalize_space
from coles_cli.conf import COLES_BASE_URL, COLES_ORDERS_ACTIVE_URL, COLES_ORDERS_PAST_URL
from coles_cli.exceptions import OrderNotFoundError


def list_orders(session, *, status: str = "current") -> dict:
    status = _status_key(status)
    ensure_logged_in(session)
    page = session.page
    url = COLES_ORDERS_ACTIVE_URL if status == "current" else COLES_ORDERS_PAST_URL
    goto_domcontentloaded(page, url)
    dismiss_cookie_banner(page)
    _wait_for_orders_page(page, status=status)
    orders = _extract_orders(page)
    return {
        "ok": True,
        "count": len(orders),
        "orders": orders,
        "empty": _orders_empty(page) and not orders,
    }


def order_items(session, order_id: str, *, from_status: str = "past") -> dict:
    order_id = str(order_id).strip()
    if not re.fullmatch(r"\d+", order_id):
        raise ValueError("order_id must be numeric")
    from_status = _status_key(from_status)
    page = session.page
    raw_status = "active" if from_status == "current" else "past"
    target = f"{COLES_BASE_URL}/account/orders/{order_id}?fromstatus={raw_status}"
    ensure_logged_in(session, url=target)
    if clean_url(page.url) != target:
        goto_domcontentloaded(page, target)
    dismiss_cookie_banner(page)
    _wait_for_order_detail(page, order_id)
    detail = _collect_order_detail(page, order_id)
    if not detail.get("found"):
        raise OrderNotFoundError(f"Could not open Coles order {order_id}; current URL: {page.url}")
    return {"ok": True, "order": public_order_detail(detail), "items": public_order_items(detail.get("items") or [])}


def public_order_detail(detail: dict) -> dict:
    public = {key: value for key, value in detail.items() if key != "url"}
    public["items"] = public_order_items(detail.get("items") or [])
    return public


def public_order_items(items: list[dict]) -> list[dict]:
    return [{key: value for key, value in item.items() if key not in {"id", "url", "text"}} for item in items]


def _status_key(status: str) -> str:
    value = normalize_space(status).casefold()
    if value in {"active", "current", "open"}:
        return "current"
    if value in {"past", "previous", "history"}:
        return "past"
    raise ValueError("status must be current or past")


def _wait_for_orders_page(page, *, status: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            text = page.locator("body").inner_text(timeout=1000)
            if re.search(r"Order\s*#\s*\d{6,}|no .*orders?|you don.t have .*orders", text, re.I):
                return
            if status == "current" and re.search(r"no current orders|active orders", text, re.I):
                return
        except PlaywrightError:
            pass
        page.wait_for_timeout(300)


def _wait_for_order_detail(page, order_id: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            text = page.locator("body").inner_text(timeout=1000)
            if order_id in text or re.search(r"items?|order details?|order summary", text, re.I):
                return
        except PlaywrightError:
            pass
        page.wait_for_timeout(300)


def _orders_empty(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=1000)
    except PlaywrightError:
        return False
    return bool(re.search(r"no .*orders|you don.t have .*orders|no current orders|no past orders", text, re.I))


def _extract_orders(page) -> list[dict]:
    try:
        return page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const absolute = (href) => {
                try { return new URL(href, location.origin).href; } catch { return href || ''; }
              };
              const cardFor = (link, id) => {
                let current = link;
                for (let i = 0; i < 8 && current; i += 1) {
                  const text = clean(current.innerText || current.textContent || '');
                  if (text.includes(id) && text.length > 20) return current;
                  if (/order|total|delivery|pickup|status/i.test(text) && text.length > 30) return current;
                  current = current.parentElement;
                }
                return link;
              };
              const links = Array.from(document.querySelectorAll('a[href*="/account/orders/"]')).filter(visible);
              const seen = new Set();
              const orders = [];
              for (const link of links) {
                const href = link.getAttribute('href') || '';
                const match = href.match(/\/account\/orders\/(\d+)/);
                if (!match || seen.has(match[1])) continue;
                const id = match[1];
                seen.add(id);
                const card = cardFor(link, id);
                const text = clean(card.innerText || card.textContent || link.innerText || '');
                const totals = Array.from(text.matchAll(/\$\s*\d+(?:\.\d{2})?/g)).map((m) => m[0].replace(/\s+/g, ''));
                const dateMatch = text.match(/(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}/i) || text.match(/\d{1,2}\/\d{1,2}\/\d{2,4}/);
                const statusMatch = text.match(/\b(Submitted|Placed|Processing|Active|Preparing|Ready|Delivered|Collected|Cancelled|Completed|Past|Refunded)\b/i);
                orders.push({
                  index: orders.length + 1,
                  id,
                  url: absolute(href),
                  status: statusMatch ? statusMatch[1] : '',
                  date: dateMatch ? dateMatch[0] : '',
                  total: totals.length ? totals[totals.length - 1] : '',
                  summary: text.slice(0, 800),
                });
              }
              if (orders.length) return orders;

              const rawBodyText = document.body.innerText || '';
              const lines = rawBodyText.split(/\n+/).map(clean).filter(Boolean);
              for (let i = 0; i < lines.length; i += 1) {
                const idMatch = lines[i].match(/Order\s*#\s*(\d{6,})/i);
                if (!idMatch || seen.has(idMatch[1])) continue;
                const id = idMatch[1];
                seen.add(id);
                const context = lines.slice(Math.max(0, i - 4), Math.min(lines.length, i + 4));
                const total = (context.find((line) => /^\$\s*\d+(?:\.\d{2})?$/.test(line)) || '').replace(/\s+/g, '');
                const date = context.find((line) => /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}(?:st|nd|rd|th)?\s+\w+/i.test(line)) || '';
                const statusLine = context.find((line) => /^Order\s+(?:Completed|Cancelled|Delivered|Placed|Processing|Active|Preparing|Ready|Collected|Refunded)/i.test(line)) || '';
                const method = context.find((line) => /Delivery|Pick\s*up|Pickup/i.test(line)) || '';
                orders.push({
                  index: orders.length + 1,
                  id,
                  url: absolute(`/account/orders/${id}?fromstatus=past`),
                  status: statusLine.replace(/^Order\s+/i, ''),
                  date,
                  total,
                  method,
                  summary: context.join(' ').slice(0, 800),
                });
              }
              if (orders.length) return orders;

              const bodyText = clean(rawBodyText);
              const blocks = bodyText.split(/(?=Order\s+(?:delivered|placed|processing|active|preparing|ready|collected|cancelled|completed|refunded))/i);
              for (const block of blocks) {
                const idMatch = block.match(/Order number\s*(\d{6,})/i) || block.match(/Order\s*#?\s*(\d{6,})/i);
                if (!idMatch || seen.has(idMatch[1])) continue;
                const id = idMatch[1];
                seen.add(id);
                const statusMatch = block.match(/Order\s+(delivered|placed|processing|active|preparing|ready|collected|cancelled|completed|refunded)/i) || block.match(/\b(Submitted|Placed|Processing|Active|Preparing|Ready|Delivered|Collected|Cancelled|Completed|Past|Refunded)\b/i);
                const totals = Array.from(block.matchAll(/\$\s*\d+(?:\.\d{2})?/g)).map((m) => m[0].replace(/\s+/g, ''));
                const dateMatch = block.match(/(?:Placed|When)?\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}/i) || block.match(/\d{1,2}\/\d{1,2}\/\d{2,4}/);
                orders.push({
                  index: orders.length + 1,
                  id,
                  url: absolute(`/account/orders/${id}?fromstatus=past`),
                  status: statusMatch ? statusMatch[1] : '',
                  date: dateMatch ? clean(dateMatch[0].replace(/^Placed\s*/i, '')) : '',
                  total: totals.length ? totals[totals.length - 1] : '',
                  summary: clean(block).slice(0, 800),
                });
              }
              return orders;
            }
            """
        )
    except PlaywrightError:
        return []


def _collect_order_detail(page, order_id: str) -> dict:
    _expand_order_items(page)
    detail = _extract_order_detail(page, order_id)
    merged = dict(detail)
    items_by_key: dict[str, dict] = {}
    for item in detail.get("items") or []:
        items_by_key[_item_key(item)] = item

    unchanged = 0
    for _ in range(36):
        declared_count = merged.get("declared_item_count")
        if declared_count and len(items_by_key) >= declared_count:
            break
        expanded = _expand_order_items(page)
        if expanded:
            current = _extract_order_detail(page, order_id)
            for item in current.get("items") or []:
                items_by_key.setdefault(_item_key(item), item)
            for key in ("found", "id", "status", "date", "total", "summary", "declared_item_count"):
                if current.get(key):
                    merged[key] = current[key]
            unchanged = 0
            continue
        try:
            state = page.evaluate(
                r"""
                () => {
                  const before = window.scrollY;
                  const step = Math.max(500, Math.floor(window.innerHeight * 0.8));
                  window.scrollBy(0, step);
                  return {before, after: window.scrollY, bottom: window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4};
                }
                """
            )
        except PlaywrightError:
            break
        page.wait_for_timeout(450)
        current = _extract_order_detail(page, order_id)
        before_count = len(items_by_key)
        for item in current.get("items") or []:
            items_by_key.setdefault(_item_key(item), item)
        if len(items_by_key) == before_count:
            unchanged += 1
        else:
            unchanged = 0
        for key in ("found", "id", "status", "date", "total", "summary", "declared_item_count"):
            if current.get(key):
                merged[key] = current[key]
        declared_count = merged.get("declared_item_count")
        if declared_count and len(items_by_key) >= declared_count:
            break
        if state.get("bottom") and unchanged >= 2:
            break

    items = list(items_by_key.values())
    for index, item in enumerate(items, start=1):
        item["index"] = index
    merged["items"] = items
    merged["visible_item_count"] = len(items)
    merged["item_count"] = len(items)
    return merged


def _expand_order_items(page) -> bool:
    try:
        clicked = page.evaluate(
            r"""
            () => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible);
              const node = nodes.find((candidate) => /\bview\s+all\s+items\b/i.test(clean(`${candidate.getAttribute('aria-label') || ''} ${candidate.innerText || candidate.textContent || ''}`)));
              if (!node) return false;
              node.scrollIntoView({block: 'center', inline: 'center'});
              node.click();
              return true;
            }
            """
        )
    except PlaywrightError:
        clicked = False
    if clicked:
        page.wait_for_timeout(1800)
    return bool(clicked)


def _item_key(item: dict) -> str:
    return str(item.get("id") or item.get("url") or item.get("text") or item.get("index"))


def _extract_order_detail(page, order_id: str) -> dict:
    try:
        return page.evaluate(
            r"""
            (orderId) => {
              const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
              const visible = (node) => {
                const rect = node && node.getBoundingClientRect();
                return !!rect && rect.width > 0 && rect.height > 0;
              };
              const absolute = (href) => {
                try { return new URL(href, location.origin).href; } catch { return href || ''; }
              };
              const productId = (href) => {
                const match = String(href || '').match(/-(\d+)(?:[/?#]|$)/);
                return match ? match[1] : null;
              };
              const bodyText = clean(document.body.innerText || '');
              const links = Array.from(document.querySelectorAll('a[href*="/product/"]')).filter(visible);
              const seen = new Set();
              const items = [];
              const declaredMatch = bodyText.match(/Items in your order\s*\((\d+)\)/i);
              const usefulCard = (node) => {
                let current = node;
                for (let i = 0; i < 8 && current; i += 1) {
                  const text = clean(current.innerText || current.textContent || '');
                  if (text.length > 20 && /\$|qty|quantity|substitution|each|ea|kg|pack/i.test(text)) return current;
                  current = current.parentElement;
                }
                return node;
              };
              for (const link of links) {
                const href = link.getAttribute('href') || '';
                const id = productId(href) || href;
                if (seen.has(id)) continue;
                seen.add(id);
                const card = usefulCard(link);
                const text = clean(card.innerText || card.textContent || '');
                let title = clean(link.innerText || link.textContent || link.getAttribute('aria-label') || '');
                if (!title) title = clean(text.split(/Quantity\s*:/i)[0] || '');
                const prices = Array.from(text.matchAll(/\$\s*\d+(?:\.\d{2})?/g)).map((m) => m[0].replace(/\s+/g, ''));
                const qtyMatch = text.match(/(?:qty|quantity)\s*:?\s*(\d+(?:\.\d+)?)/i) || text.match(/(\d+(?:\.\d+)?)\s*x\s*\$/i);
                items.push({
                  index: items.length + 1,
                  id: String(id),
                  title,
                  url: absolute(href),
                  quantity: qtyMatch ? qtyMatch[1] : '',
                  price: prices[0] || '',
                  line_total: prices.length > 1 ? prices[prices.length - 1] : (prices[0] || ''),
                  text: text.slice(0, 500),
                });
              }
              const totals = Array.from(bodyText.matchAll(/\$\s*\d+(?:\.\d{2})?/g)).map((m) => m[0].replace(/\s+/g, ''));
              const dateMatch = bodyText.match(/(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)?\s*\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}/i) || bodyText.match(/\d{1,2}\/\d{1,2}\/\d{2,4}/);
              const statusMatch = bodyText.match(/\b(Submitted|Placed|Processing|Active|Preparing|Ready|Delivered|Collected|Cancelled|Completed|Past|Refunded)\b/i);
              return {
                found: bodyText.includes(orderId) || /order details?|order summary|items/i.test(bodyText),
                id: orderId,
                status: statusMatch ? statusMatch[1] : '',
                date: dateMatch ? dateMatch[0] : '',
                total: totals.length ? totals[totals.length - 1] : '',
                item_count: items.length,
                declared_item_count: declaredMatch ? Number(declaredMatch[1]) : null,
                items,
                summary: bodyText.slice(0, 1000),
              };
            }
            """,
            order_id,
        )
    except PlaywrightError as exc:
        raise OrderNotFoundError(f"Could not read Coles order {order_id}: {exc}") from exc
