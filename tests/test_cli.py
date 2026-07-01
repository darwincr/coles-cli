from coles_cli.actions.cart import public_cart
from coles_cli.actions.auth import ensure_logged_in
from coles_cli.actions.orders import _expand_order_items, order_items, public_order_items
from coles_cli.actions.products import _set_product_quantity, search_products, search_url, public_product
from coles_cli.cli import _json_payload, _orders_status, _parse_args, _query_text, _render
from coles_cli.exceptions import CartError
from coles_cli.worker import _request_existing_worker, _route_connection
import pytest


class FakeSocket:
    def __init__(self, request: bytes):
        self._request = request
        self.sent = b""
        self.closed = False

    def recv(self, _size):
        chunk = self._request
        self._request = b""
        return chunk

    def sendall(self, data):
        self.sent += data

    def close(self):
        self.closed = True


class FakePage:
    def __init__(self):
        self.url = ""
        self.goto_urls = []
        self.clicked_view_all = False

    def goto(self, url, **_kwargs):
        self.url = url
        self.goto_urls.append(url)

    def wait_for_load_state(self, _state):
        pass

    def evaluate(self, _script, *_args):
        self.clicked_view_all = True
        return True

    def wait_for_timeout(self, _timeout):
        pass


class FakeSearchSession:
    def __init__(self):
        self.page = FakePage()


def test_orders_list_accepts_status_flag():
    args = _parse_args(["orders", "list", "--status", "past", "--json"])
    assert args.verb == "orders-list"
    assert _orders_status(args) == "past"
    assert args.json is True


def test_orders_list_accepts_positional_status():
    args = _parse_args(["orders", "list", "current"])
    assert args.verb == "orders-list"
    assert _orders_status(args) == "current"


def test_orders_list_json_includes_order_id_and_hides_internal_fields(capsys):
    _render(
        "orders-list",
        {
            "ok": True,
            "status": "current",
            "count": 1,
            "url": "https://www.coles.com.au/account/orders?status=past",
            "orders": [
                {
                    "index": 1,
                    "id": "262470014",
                    "url": "https://www.coles.com.au/account/orders/262470014?fromstatus=past",
                    "status": "Delivered",
                    "date": "1 Jan 2026",
                    "total": "$10.00",
                    "summary": "internal extraction text",
                }
            ],
        },
        True,
    )

    assert capsys.readouterr().out == '{"ok": true, "count": 1, "orders": [{"index": 1, "id": "262470014", "status": "Delivered", "date": "1 Jan 2026", "total": "$10.00"}]}\n'


def test_json_payload_removes_urls_recursively():
    assert _json_payload(
        "cart-list",
        {
            "ok": True,
            "url": "https://www.coles.com.au/",
            "cart": {"items": [{"title": "Milk", "url": "https://example.test", "text": "internal"}]},
        },
    ) == {"ok": True, "cart": {"items": [{"title": "Milk"}]}}


def test_order_items_uses_direct_order_url(monkeypatch):
    page = FakePage()
    session = type("Session", (), {"page": page})()
    target = "https://www.coles.com.au/account/orders/262470014?fromstatus=past"

    def fake_ensure_logged_in(_session, *, url):
        page.goto(url)
        return {"ok": True}

    monkeypatch.setattr("coles_cli.actions.orders.ensure_logged_in", fake_ensure_logged_in)
    monkeypatch.setattr("coles_cli.actions.orders.dismiss_cookie_banner", lambda _page: None)
    monkeypatch.setattr("coles_cli.actions.orders._wait_for_order_detail", lambda _page, _order_id: None)
    monkeypatch.setattr(
        "coles_cli.actions.orders._collect_order_detail",
        lambda _page, order_id: {"found": True, "id": order_id, "items": [{"title": "Milk", "url": "https://example.test", "text": "internal"}]},
    )

    result = order_items(session, "262470014", from_status="past")

    assert page.goto_urls == [target]
    assert "url" not in result
    assert result["items"] == [{"title": "Milk"}]


def test_expand_order_items_clicks_visible_view_all_button():
    page = FakePage()

    assert _expand_order_items(page) is True
    assert page.clicked_view_all is True


def test_ensure_logged_in_uses_target_page_when_auth_is_confirmed(monkeypatch):
    page = FakePage()
    session = type("Session", (), {"page": page})()
    target = "https://www.coles.com.au/account/orders/262470014?fromstatus=past"

    monkeypatch.setattr("coles_cli.actions.auth.dismiss_cookie_banner", lambda _page: None)
    monkeypatch.setattr("coles_cli.actions.auth._blocking_state", lambda _session, timeout_ms=500: None)
    monkeypatch.setattr(
        "coles_cli.actions.auth._current_authenticated_account",
        lambda _session, timeout_ms=700: {"ok": True, "authenticated": True, "state": "logged_in", "url": page.url},
    )

    result = ensure_logged_in(session, url=target)

    assert result["authenticated"] is True
    assert page.goto_urls == [target]


def test_ensure_logged_in_falls_back_when_target_page_is_inconclusive(monkeypatch):
    page = FakePage()
    session = type("Session", (), {"page": page})()
    target = "https://www.coles.com.au/product/example-123"
    calls = []

    monkeypatch.setattr("coles_cli.actions.auth.dismiss_cookie_banner", lambda _page: None)
    monkeypatch.setattr("coles_cli.actions.auth._blocking_state", lambda _session, timeout_ms=500: None)
    monkeypatch.setattr("coles_cli.actions.auth._showing_sign_in", lambda _session, timeout_ms=700: False)

    def fake_current_account(_session, timeout_ms=700):
        calls.append(page.url)
        if page.url.endswith("/account/orders?status=active"):
            return {"ok": True, "authenticated": True, "state": "logged_in", "url": page.url}
        return None

    monkeypatch.setattr("coles_cli.actions.auth._current_authenticated_account", fake_current_account)

    result = ensure_logged_in(session, url=target)

    assert result["authenticated"] is True
    assert page.goto_urls == [target, "https://www.coles.com.au/account/orders?status=active"]
    assert calls == page.goto_urls


def test_products_query_tokens_are_joined():
    args = _parse_args(["products", "search", "Coles", "Lettuce", "Cos", "Baby", "Hearts"])
    assert args.verb == "products-search"
    assert _query_text(args) == "Coles Lettuce Cos Baby Hearts"


def test_products_search_accepts_result_size():
    args = _parse_args(["products", "search", "milk", "--result-size", "96"])
    assert args.verb == "products-search"
    assert args.result_size == 96


def test_search_url_omits_first_page_and_adds_later_pages():
    assert search_url("milk") == "https://www.coles.com.au/search/products?q=milk"
    assert search_url("milk", page=2) == "https://www.coles.com.au/search/products?q=milk&page=2"


def test_search_products_fetches_pages_until_result_size(monkeypatch):
    session = FakeSearchSession()
    pages = [
        [{"title": f"Milk {index}", "quantity": ""} for index in range(1, 49)],
        [{"title": f"Milk {index}", "quantity": ""} for index in range(49, 97)],
    ]

    monkeypatch.setattr("coles_cli.actions.products.dismiss_cookie_banner", lambda _page: None)
    monkeypatch.setattr("coles_cli.actions.products._wait_for_search_results", lambda _page: None)
    monkeypatch.setattr("coles_cli.actions.products._extract_products", lambda _page: pages.pop(0))

    result = search_products(session, "milk", result_size=50)

    assert session.page.goto_urls == [
        "https://www.coles.com.au/search/products?q=milk",
        "https://www.coles.com.au/search/products?q=milk&page=2",
    ]
    assert result["result_count"] == 50
    assert result["products"][0]["index"] == 1
    assert result["products"][-1]["index"] == 50


def test_products_add_requires_index():
    args = _parse_args(["products", "add", "milk", "--index", "2"])
    assert args.verb == "products-add"
    assert _query_text(args) == "milk"
    assert args.index == 2
    assert args.set_quantity is None


def test_products_add_accepts_set_quantity():
    args = _parse_args(["products", "add", "milk", "--index", "2", "--set-quantity", "3"])
    assert args.verb == "products-add"
    assert _query_text(args) == "milk"
    assert args.index == 2
    assert args.set_quantity == 3


def test_products_add_without_set_quantity_increments_existing_quantity(monkeypatch):
    page = FakePage()
    actions = []

    monkeypatch.setattr("coles_cli.actions.products._product_tile_state", lambda _page, *, index: {"quantity": 3})
    monkeypatch.setattr("coles_cli.actions.products._wait_for_product_quantity", lambda _page, *, index, target=None, minimum=None: {"quantity": target or minimum})
    monkeypatch.setattr("coles_cli.actions.products._click_quantity_button", lambda _page, *, index, action: actions.append(action))

    result = _set_product_quantity(page, index=1, quantity=None)

    assert result == {"quantity": 4, "actions": ["increase"]}


def test_products_add_without_set_quantity_adds_new_item_once(monkeypatch):
    page = FakePage()
    actions = []

    monkeypatch.setattr("coles_cli.actions.products._product_tile_state", lambda _page, *, index: {"quantity": 0})
    monkeypatch.setattr("coles_cli.actions.products._wait_for_product_quantity", lambda _page, *, index, target=None, minimum=None: {"quantity": target or minimum})
    monkeypatch.setattr("coles_cli.actions.products._click_add_button", lambda _page, *, index: actions.append("add"))
    monkeypatch.setattr("coles_cli.actions.products._click_quantity_button", lambda _page, *, index, action: actions.append(action))

    result = _set_product_quantity(page, index=1, quantity=None)

    assert result == {"quantity": 1, "actions": ["add"]}


def test_products_add_without_set_quantity_errors_when_increment_not_confirmed(monkeypatch):
    page = FakePage()

    monkeypatch.setattr("coles_cli.actions.products._product_tile_state", lambda _page, *, index: {"quantity": 3})
    monkeypatch.setattr("coles_cli.actions.products._wait_for_product_quantity", lambda _page, *, index, target=None, minimum=None: {"quantity": 3})
    monkeypatch.setattr("coles_cli.actions.products._click_quantity_button", lambda _page, *, index, action: None)

    with pytest.raises(CartError, match="Could not increment product index 1 quantity to 4"):
        _set_product_quantity(page, index=1, quantity=None)


def test_shoppingcart_alias():
    args = _parse_args(["shoppingcart", "list", "--json"])
    assert args.verb == "shoppingcart-list"
    assert args.json is True


def test_worker_queues_command_while_action_busy():
    import json
    import queue
    import threading

    work_queue = queue.Queue()
    action_busy = threading.Event()
    shutdown_event = threading.Event()
    action_busy.set()
    conn = FakeSocket(b'{"argv":["products","search","milk","--json"]}\n')

    _route_connection(conn, work_queue, action_busy, shutdown_event)

    queued_conn, argv = work_queue.get_nowait()
    assert queued_conn is conn
    assert argv == ["products", "search", "milk", "--json"]
    assert conn.sent == b""
    assert conn.closed is False


def test_worker_ping_reports_busy_when_queue_has_work():
    import json
    import queue
    import threading

    work_queue = queue.Queue()
    action_busy = threading.Event()
    shutdown_event = threading.Event()
    work_queue.put((FakeSocket(b""), ["cart", "list"]))
    conn = FakeSocket(b'{"ping":true}\n')

    _route_connection(conn, work_queue, action_busy, shutdown_event)

    assert json.loads(conn.sent.decode("utf-8"))["busy"] is True
    assert conn.closed is True


def test_client_sends_command_when_worker_ping_reports_busy(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr("coles_cli.worker.socket_path", lambda _name: tmp_path / "worker.sock")
    (tmp_path / "worker.sock").touch()

    def fake_send_request(_path, payload, *, timeout=None):
        calls.append((payload, timeout))
        if payload.get("ping"):
            return {"returncode": 0, "stdout": "", "stderr": "", "busy": True}
        return {"returncode": 0, "stdout": "queued\n", "stderr": ""}

    monkeypatch.setattr("coles_cli.worker._send_request", fake_send_request)

    response = _request_existing_worker("default", ["orders", "list", "--json"], {"argv": ["orders", "list", "--json"]})

    assert response == {"returncode": 0, "stdout": "queued\n", "stderr": ""}
    assert calls == [
        ({"ping": True}, 1),
        ({"argv": ["orders", "list", "--json"]}, 120),
    ]


def test_worker_restarts_browser_once_when_driver_connection_closes(monkeypatch):
    import coles_cli.cli as cli_module

    from coles_cli.worker import _execute_request

    calls = []
    session = type(
        "Session",
        (),
        {
            "ensure_browser": lambda self: calls.append("ensure"),
            "close": lambda self: calls.append("close"),
        },
    )()

    def fake_execute_verb(_args, _session):
        calls.append("execute")
        if calls.count("execute") == 1:
            raise Exception("Page.goto: Connection closed while reading from the driver")
        return 0

    monkeypatch.setattr(cli_module, "_execute_verb", fake_execute_verb)
    monkeypatch.setattr(cli_module, "_parse_args", lambda _argv: type("Args", (), {"verb": "login"})())

    result = _execute_request(session, ["login", "--interactive", "--wait", "--timeout", "300"])

    assert result["returncode"] == 0
    assert calls == ["ensure", "execute", "close", "ensure", "execute"]


def test_cart_remove_requires_index():
    args = _parse_args(["cart", "remove", "--index", "2"])
    assert args.verb == "cart-remove"
    assert args.index == 2


def test_shoppingcart_remove_alias():
    args = _parse_args(["shoppingcart", "remove", "--index", "1", "--json"])
    assert args.verb == "shoppingcart-remove"
    assert args.index == 1
    assert args.json is True


def test_cart_set_quantity_requires_index_and_quantity():
    args = _parse_args(["cart", "set-quantity", "--index", "2", "--quantity", "4"])
    assert args.verb == "cart-set-quantity"
    assert args.index == 2
    assert args.quantity == 4


def test_shoppingcart_set_quantity_alias():
    args = _parse_args(["shoppingcart", "set-quantity", "--index", "1", "--quantity", "2", "--json"])
    assert args.verb == "shoppingcart-set-quantity"
    assert args.index == 1
    assert args.quantity == 2
    assert args.json is True


def test_public_cart_hides_internal_item_fields():
    cart = {
        "open": True,
        "items": [
            {
                "index": 1,
                "id": "1039770",
                "title": "Coles Organic Banana | approx. 170g",
                "url": "https://www.coles.com.au/product/coles-organic-banana-approx.-170g-1039770",
                "quantity": "1",
                "price": "$1.00",
                "line_total": "$1.00",
                "text": "internal extraction text",
            }
        ],
    }

    public = public_cart(cart)

    assert public["items"] == [
        {
            "index": 1,
            "title": "Coles Organic Banana | approx. 170g",
            "quantity": "1",
            "price": "$1.00",
            "line_total": "$1.00",
        }
    ]


def test_public_order_items_hide_internal_fields():
    items = [
        {
            "index": 1,
            "id": "1039770",
            "title": "Coles Organic Banana | approx. 170g",
            "url": "https://www.coles.com.au/product/coles-organic-banana-approx.-170g-1039770",
            "quantity": "1",
            "price": "$1.00",
            "line_total": "$1.00",
            "text": "internal extraction text",
        }
    ]

    assert public_order_items(items) == [
        {
            "index": 1,
            "title": "Coles Organic Banana | approx. 170g",
            "quantity": "1",
            "price": "$1.00",
            "line_total": "$1.00",
        }
    ]


def test_public_product_hides_internal_fields():
    product = {
        "index": 1,
        "id": "1039770",
        "title": "Coles Organic Banana | approx. 170g",
        "url": "https://www.coles.com.au/product/coles-organic-banana-approx.-170g-1039770",
        "price": "$1.00",
        "unit_price": "$5.88 per 1kg",
        "image": "https://example.test/image.jpg",
        "available": True,
        "in_trolley": True,
        "quantity": "2",
        "status": "Product is in your trolley",
        "add_label": "Add to trolley",
    }

    public = public_product(product)

    assert public == {
        "index": 1,
        "title": "Coles Organic Banana | approx. 170g",
        "price": "$1.00",
        "unit_price": "$5.88 per 1kg",
        "available": True,
        "in_trolley": True,
        "cart_quantity": "2",
    }
