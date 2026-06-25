from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from coles_cli.conf import DEFAULT_CHECKOUT_TIMEOUT_S, load_dotenv_file
from coles_cli.exceptions import (
    AuthenticationError,
    CartError,
    CheckoutError,
    ColesUnavailableError,
    ElementNotFoundError,
    InteractiveAuthenticationRequired,
    OrderNotFoundError,
    ProductNotFoundError,
)
from coles_cli.session import ColesSession, clear_profile, session_lock

logger = logging.getLogger("coles_cli")

_ERROR_TYPES = [
    (InteractiveAuthenticationRequired, "interactive_authentication_required"),
    (AuthenticationError, "authentication"),
    (ProductNotFoundError, "product_not_found"),
    (OrderNotFoundError, "order_not_found"),
    (CartError, "cart"),
    (CheckoutError, "checkout"),
    (ElementNotFoundError, "element_not_found"),
    (ColesUnavailableError, "coles_unavailable"),
]


def _out(text: str) -> None:
    sys.stdout.write(f"{text}\n")
    sys.stdout.flush()


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _error_type(exc: Exception) -> str | None:
    for cls, name in _ERROR_TYPES:
        if isinstance(exc, cls):
            return name
    return None


def _render(command: str, result: dict, as_json: bool) -> None:
    if as_json:
        result = _json_payload(command, result)
        _out(json.dumps(result, ensure_ascii=False, default=str))
        return
    if command in {"login", "auth-interactive"}:
        _out(f"logged in: {result.get('account') or result.get('url')}")
    elif command == "auth-status":
        if result.get("authenticated"):
            _out(f"logged in: {result.get('account') or result.get('url')}")
        else:
            _out(f"not logged in: {result.get('state')} {result.get('message') or ''}".strip())
    elif command == "orders-list":
        _render_orders(result)
    elif command == "orders-items":
        _render_order_items(result)
    elif command == "products-search":
        _render_products(result)
    elif command == "products-add":
        product = result.get("product") or {}
        state = result.get("state") or {}
        suffix = f" ({state.get('message')})" if state.get("message") else ""
        prefix = "added" if result.get("added", True) else "add clicked, not confirmed"
        _out(f"{prefix}: {product.get('title') or product.get('id') or 'product'}{suffix}")
    elif command in {"cart-list", "shoppingcart-list"}:
        _render_cart(result)
    elif command in {"cart-remove", "shoppingcart-remove"}:
        item = result.get("removed_item") or {}
        _out(f"removed: {item.get('title') or item.get('id') or 'cart item'}")
    elif command in {"cart-set-quantity", "shoppingcart-set-quantity"}:
        item = result.get("item") or {}
        _out(f"set quantity: {item.get('title') or 'cart item'} x{result.get('quantity')}")
    elif command in {"cart-checkout", "shoppingcart-checkout"}:
        _out(result.get("message") or "Order placed. Retrieve it with `coles orders list --status current`.")
    elif command == "session-clear":
        _out(f"cleared {result.get('name')}")
    elif command == "session-stop":
        _out(f"stopped {result.get('name')}")
    else:
        _out("\n".join(f"{key}: {value}" for key, value in result.items()))


def _render_orders(result: dict) -> None:
    orders = result.get("orders") or []
    if not orders:
        _out("(no orders)")
        return
    lines = []
    for order in orders:
        details = " ".join(part for part in [order.get("status"), order.get("date"), order.get("total")] if part)
        lines.append(f"{order.get('index')}. {order.get('id')} {details}".strip())
    _out("\n".join(lines))


def _without_keys(value, keys: set[str]):
    if isinstance(value, dict):
        return {key: _without_keys(item, keys) for key, item in value.items() if key not in keys}
    if isinstance(value, list):
        return [_without_keys(item, keys) for item in value]
    return value


def _json_payload(command: str, result: dict) -> dict:
    payload = _without_keys(result, {"url", "summary", "text"})
    if command == "orders-list":
        payload.pop("status", None)
    return payload


def _render_order_items(result: dict) -> None:
    order = result.get("order") or {}
    items = result.get("items") or []
    if not items:
        _out(f"order {order.get('id')}: (no visible items)")
        return
    lines = [f"order {order.get('id')}: {len(items)} item(s)"]
    for item in items:
        qty = f" x{item.get('quantity')}" if item.get("quantity") else ""
        price = f" {item.get('line_total') or item.get('price')}" if item.get("line_total") or item.get("price") else ""
        lines.append(f"{item.get('index')}. {item.get('title') or item.get('id') or 'cart item'}{qty}{price}")
    _out("\n".join(lines))


def _render_products(result: dict) -> None:
    products = result.get("products") or []
    if not products:
        _out("(no products)")
        return
    lines = []
    for product in products:
        price = f" {product.get('price')}" if product.get("price") else ""
        unit = f" ({product.get('unit_price')})" if product.get("unit_price") else ""
        availability = "" if product.get("available", True) else " [unavailable]"
        lines.append(f"{product.get('index')}. {product.get('title') or product.get('id')}{price}{unit}{availability}")
    _out("\n".join(lines))


def _render_cart(result: dict) -> None:
    cart = result.get("cart") or {}
    items = cart.get("items") or []
    if cart.get("empty") or not items:
        summary = f" total {cart.get('total')}" if cart.get("total") else ""
        _out(f"(cart empty){summary}" if cart.get("empty") else "(no visible cart items)")
        return
    lines = []
    total = f" total {cart.get('total')}" if cart.get("total") else ""
    lines.append(f"cart: {len(items)} item(s){total}")
    for item in items:
        qty = f" x{item.get('quantity')}" if item.get("quantity") else ""
        price = f" {item.get('line_total') or item.get('price')}" if item.get("line_total") or item.get("price") else ""
        lines.append(f"{item.get('index')}. {item.get('title') or item.get('id')}{qty}{price}")
    _out("\n".join(lines))


def _verb_login(session, args) -> dict:
    if args.interactive:
        from coles_cli.actions.auth import interactive_auth

        return interactive_auth(session, wait=args.wait, timeout=args.timeout)

    from coles_cli.actions.auth import ensure_logged_in

    return ensure_logged_in(session)


def _verb_auth_interactive(session, args) -> dict:
    from coles_cli.actions.auth import interactive_auth

    return interactive_auth(session, wait=args.wait, timeout=args.timeout)


def _verb_auth_status(session, args) -> dict:
    from coles_cli.actions.auth import auth_status

    return auth_status(session)


def _verb_orders_list(session, args) -> dict:
    from coles_cli.actions.orders import list_orders

    return list_orders(session, status=_orders_status(args))


def _verb_orders_items(session, args) -> dict:
    from coles_cli.actions.orders import order_items

    return order_items(session, args.order_id, from_status=args.from_status)


def _verb_products_search(session, args) -> dict:
    from coles_cli.actions.products import search_products

    return search_products(session, _query_text(args), result_size=args.result_size)


def _verb_products_add(session, args) -> dict:
    from coles_cli.actions.products import add_product_to_cart

    return add_product_to_cart(session, _query_text(args), index=args.index, quantity=args.set_quantity)


def _verb_cart_list(session, args) -> dict:
    from coles_cli.actions.cart import list_cart

    return list_cart(session)


def _verb_cart_checkout(session, args) -> dict:
    from coles_cli.actions.cart import checkout

    return checkout(session, timeout=args.timeout)


def _verb_cart_remove(session, args) -> dict:
    from coles_cli.actions.cart import remove_cart_item

    return remove_cart_item(session, index=args.index)


def _verb_cart_set_quantity(session, args) -> dict:
    from coles_cli.actions.cart import set_cart_item_quantity

    return set_cart_item_quantity(session, index=args.index, quantity=args.quantity)


_VERBS = {
    "login": _verb_login,
    "auth-interactive": _verb_auth_interactive,
    "auth-status": _verb_auth_status,
    "orders-list": _verb_orders_list,
    "orders-items": _verb_orders_items,
    "products-search": _verb_products_search,
    "products-add": _verb_products_add,
    "cart-list": _verb_cart_list,
    "cart-remove": _verb_cart_remove,
    "cart-set-quantity": _verb_cart_set_quantity,
    "cart-checkout": _verb_cart_checkout,
    "shoppingcart-list": _verb_cart_list,
    "shoppingcart-remove": _verb_cart_remove,
    "shoppingcart-set-quantity": _verb_cart_set_quantity,
    "shoppingcart-checkout": _verb_cart_checkout,
}


def _error_payload(exc: Exception, error_type: str) -> dict:
    payload = {
        "ok": False,
        "authenticated": False,
        "error": {
            "type": error_type,
            "message": str(exc),
        },
    }
    if error_type == "interactive_authentication_required":
        payload["state"] = "login_required"
        payload["next_command"] = "coles login --interactive --wait --timeout 300"
    return payload


def _execute_verb(args, session) -> int:
    try:
        _render(args.verb, _VERBS[args.verb](session, args), args.json)
        return 0
    except Exception as exc:  # noqa: BLE001
        error_type = _error_type(exc)
        if error_type is None:
            raise
        if args.json:
            _out(json.dumps(_error_payload(exc, error_type), ensure_ascii=False, default=str))
            return 1
        _err(f"error: {error_type}: {exc}")
        return 1


def _run_verb_local(args) -> int:
    with session_lock(args.name):
        session = ColesSession(args.name)
        with session:
            return _execute_verb(args, session)


def _run_verb(args, argv: list[str]) -> int:
    if os.environ.get("COLES_CLI_WORKER") == "1":
        return _run_verb_local(args)
    from coles_cli.worker import run_via_worker

    return run_via_worker(args.name, argv)


def _cmd_session_clear(args) -> int:
    from coles_cli.worker import stop_worker

    stop_worker(args.name)
    with session_lock(args.name):
        clear_profile(args.name)
    _render("session-clear", {"name": args.name, "cleared": True}, args.json)
    return 0


def _cmd_session_stop(args) -> int:
    from coles_cli.worker import stop_worker

    stop_worker(args.name)
    _render("session-stop", {"name": args.name, "stopped": True}, args.json)
    return 0


def _orders_status(args) -> str:
    return args.status or args.status_arg or "current"


def _query_text(args) -> str:
    return " ".join(args.query)


def _add_query_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="+", help="Product search query. Multiple words are joined with spaces.")


def _add_cart_subcommands(parser: argparse.ArgumentParser, common: argparse.ArgumentParser) -> None:
    cart_sub = parser.add_subparsers(dest="cart_cmd", required=True)
    cart_sub.add_parser("list", parents=[common], help="Open the trolley and list visible items")
    p_remove = cart_sub.add_parser("remove", parents=[common], help="Remove a visible trolley item by list index")
    p_remove.add_argument("--index", type=int, required=True, help="1-based cart item index to remove from `cart list`")
    p_set_quantity = cart_sub.add_parser("set-quantity", parents=[common], help="Set a visible trolley item quantity by list index")
    p_set_quantity.add_argument("--index", type=int, required=True, help="1-based cart item index from `cart list`")
    p_set_quantity.add_argument("--quantity", type=int, required=True, help="Final trolley quantity for the cart item")
    p_checkout = cart_sub.add_parser("checkout", parents=[common], help="Complete checkout and place the order")
    p_checkout.add_argument("--timeout", type=int, default=DEFAULT_CHECKOUT_TIMEOUT_S, help=f"Maximum seconds to spend in checkout (default: {DEFAULT_CHECKOUT_TIMEOUT_S})")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--session",
        "--name",
        dest="name",
        default=os.environ.get("COLES_CLI_SESSION", "default"),
        help="Session/profile name (default: $COLES_CLI_SESSION or 'default')",
    )
    common.add_argument("--json", action="store_true", help="Emit full JSON instead of a short summary")

    parser = argparse.ArgumentParser(prog="coles", description="Drive Coles shopping through Camoufox")
    sub = parser.add_subparsers(dest="cmd", required=True)

    session_cmd = sub.add_parser("session", help="Manage local browser session state")
    session_sub = session_cmd.add_subparsers(dest="subcmd", required=True)
    session_sub.add_parser("clear", parents=[common], help="Delete the local browser profile for a session")
    session_sub.add_parser("stop", parents=[common], help="Stop the background worker without deleting the browser profile")

    p_login = sub.add_parser("login", parents=[common], help="Log in or verify the current Coles session")
    p_login.add_argument("--interactive", action="store_true", help="Open Coles and wait while you complete login manually")
    p_login.add_argument("--wait", action="store_true", help="With --interactive, poll until login completes instead of waiting for Enter")
    p_login.add_argument("--timeout", type=int, default=300, help="Maximum seconds to wait with --interactive --wait (default: 300)")

    auth_cmd = sub.add_parser("auth", help="Authenticate the persistent browser profile")
    auth_sub = auth_cmd.add_subparsers(dest="auth_cmd", required=True)
    auth_sub.add_parser("status", parents=[common], help="Report the current authentication state")
    p_auth_interactive = auth_sub.add_parser("interactive", parents=[common], help="Open Coles and wait while you log in manually")
    p_auth_interactive.add_argument("--wait", action="store_true", help="Poll until login completes instead of waiting for Enter")
    p_auth_interactive.add_argument("--timeout", type=int, default=300, help="Maximum seconds to wait with --wait (default: 300)")

    orders_cmd = sub.add_parser("orders", help="List Coles orders and order items")
    orders_sub = orders_cmd.add_subparsers(dest="orders_cmd", required=True)
    p_orders_list = orders_sub.add_parser("list", parents=[common], help="List current or past orders")
    p_orders_list.add_argument("status_arg", nargs="?", choices=["current", "past"], help="Optional positional status")
    p_orders_list.add_argument("--status", choices=["current", "past"], help="Order status to list")
    p_orders_items = orders_sub.add_parser("items", parents=[common], help="List visible items from an order id")
    p_orders_items.add_argument("order_id", help="Coles order id")
    p_orders_items.add_argument("--from-status", choices=["current", "past"], default="past", help="Source order tab for the detail page (default: past)")

    products_cmd = sub.add_parser("products", help="Search products and add results to the trolley")
    products_sub = products_cmd.add_subparsers(dest="products_cmd", required=True)
    p_products_search = products_sub.add_parser("search", parents=[common], help="Search Coles products")
    _add_query_arg(p_products_search)
    p_products_search.add_argument("--result-size", type=int, default=48, help="Maximum products to return across search pages (default: 48)")
    p_products_add = products_sub.add_parser("add", parents=[common], help="Add a search result to the trolley")
    _add_query_arg(p_products_add)
    p_products_add.add_argument("--index", type=int, required=True, help="1-based search result index to add")
    p_products_add.add_argument("--set-quantity", type=int, default=1, help="Final trolley quantity for the product (default: 1)")

    cart_cmd = sub.add_parser("cart", help="Read and checkout the Coles trolley")
    _add_cart_subcommands(cart_cmd, common)
    shoppingcart_cmd = sub.add_parser("shoppingcart", help="Alias for cart")
    _add_cart_subcommands(shoppingcart_cmd, common)
    return parser


def _configure_logging() -> None:
    level = os.environ.get("COLES_CLI_LOG", "INFO").upper()
    logging.basicConfig(level=level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _parse_args(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "auth":
        args.verb = f"auth-{args.auth_cmd}"
    elif args.cmd == "orders":
        args.verb = f"orders-{args.orders_cmd}"
    elif args.cmd == "products":
        args.verb = f"products-{args.products_cmd}"
    elif args.cmd in {"cart", "shoppingcart"}:
        args.verb = f"{args.cmd}-{args.cart_cmd}"
    else:
        args.verb = args.cmd
    return args


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    load_dotenv_file()
    args = _parse_args(argv)
    _configure_logging()
    if args.cmd == "session":
        if args.subcmd == "stop":
            return _cmd_session_stop(args)
        return _cmd_session_clear(args)
    return _run_verb(args, argv)


if __name__ == "__main__":
    raise SystemExit(main())
