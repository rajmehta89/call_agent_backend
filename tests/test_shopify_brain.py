import pytest

import shopify_service as shopify_module
from brain_service import brain_service
from shopify_service import normalize_store_domain, phones_match, shopify_service as shop

ORDER = {"name": "#1042", "created_at": "2026-10-10T09:00:00Z", "cancelled": False, "financial_status": "paid",
         "fulfillment_status": "fulfilled", "total": "2499.00 INR", "items": ["1 x Trail Runner Shoe - 9"],
         "tracking": [{"company": "Delhivery", "number": "DLV123456", "url": None, "status": "SUCCESS"}],
         "_emails": ["priya@example.com"], "_phones": ["+91 98765 43210"]}
PRODUCTS = [{"title": "Trail Runner Shoe", "product_type": "Shoes", "tags": ["running"], "currency": "INR",
             "min_price": "2499.00", "max_price": "2499.00", "total_inventory": 7,
             "variants": [{"title": "8", "price": "2499.00", "inventory_quantity": 0, "available": False},
                          {"title": "9", "price": "2499.00", "inventory_quantity": 4, "available": True}]}]
ALL_TOOLS = {"search_shopify_products": True, "check_inventory": True, "get_order": True, "get_customer_orders": True, "get_store_policy": True}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(shop, "store_domain", "demo.myshopify.com")
    monkeypatch.setattr(shop, "static_token", "shpat_test")
    monkeypatch.setattr(shop, "client_id", "")
    monkeypatch.setattr(shop, "client_secret", "")
    monkeypatch.setattr(shop, "_scopes", ["read_customers", "read_inventory", "read_legal_policies", "read_orders", "read_products"])
    monkeypatch.setattr(shop, "orders", lambda q="", limit=25: [ORDER] if q.lstrip("#") == "1042" else [])
    monkeypatch.setattr(shop, "orders_for_phone", lambda phone, limit=3: [ORDER] if phones_match(phone, "+919876543210") else [])
    monkeypatch.setattr(shop, "products", lambda q="", limit=25: PRODUCTS)
    monkeypatch.setattr(shop, "catalog_words", lambda: frozenset({"runner", "shoe", "trail"}))  # the store's own catalogue words
    monkeypatch.setattr(shop, "catalog_index", lambda: [])  # no background build reaching the network
    monkeypatch.setattr(shop, "cached_orders_for_phone", lambda phone: None)
    monkeypatch.setattr(shop, "policies", lambda: [{"type": "REFUND_POLICY", "title": "Refund policy", "text": "Returns within 14 days."}])
    tools = dict(ALL_TOOLS)
    monkeypatch.setattr(brain_service, "tools", lambda: tools)
    return tools


def context(question, phone=None, conversation=None):
    return brain_service._shopify_context(question, phone, conversation or question)


def test_helpers():
    assert normalize_store_domain("https://Demo-Store/admin") == "demo-store.myshopify.com"
    assert phones_match("+91 98765 43210", "9876543210")
    assert not phones_match("+1 555 000 1111", "9876543210")


def test_order_details_for_the_caller_on_the_order(store):
    text, tool = context("Where is my order 1042?", "+919876543210")
    assert tool == "get_order" and "Verified order" in text and "DLV123456" in text


def test_order_details_withheld_from_an_unverified_caller(store):
    text, _ = context("Where is my order 1042?", "+15550001111")
    assert "not verified" in text and "DLV123456" not in text and "2499" not in text


def test_spoken_email_verifies_the_caller(store):
    text, _ = context("it's priya at example dot com", "+15550001111", "where is my order 1042 it's priya at example dot com")
    assert "Verified order" in text


def test_caller_id_finds_orders_only_when_allowed(store):
    assert "found by their caller ID" in context("Has my order shipped?", "+919876543210")[0]
    store["get_customer_orders"] = False
    assert "Ask for their order number" in context("Has my order shipped?", "+919876543210")[0]


def test_product_answers_respect_the_stock_switch(store):
    text, tool = context("Do you have trail running shoes in size 8?")
    assert tool == "search_shopify_products" and "sold out" in text and "4 left" in text
    store["check_inventory"] = False
    text, _ = context("Do you have trail running shoes in size 8?")
    assert "4 left" not in text and "do not quote stock counts" in text


@pytest.mark.parametrize("tool, question", [("get_order", "Where is my order 1042?"),
                                            ("get_store_policy", "What is your return policy?"),
                                            ("search_shopify_products", "Do you have trail shoes?")])
def test_each_switch_turns_its_feature_off(store, tool, question):
    store[tool] = False
    assert context(question, "+919876543210")[1] is None


def test_unrelated_questions_never_touch_the_store(store):
    assert context("What services do you offer?") == ("", None)


def test_product_search_tries_the_callers_words_together_first(monkeypatch, store):
    seen = []

    def capture(q="", limit=25):
        seen.append(q)
        return PRODUCTS if q.startswith("runner") else []

    monkeypatch.setattr(shop, "products", capture)
    monkeypatch.setattr(shop, "catalog_words", lambda: frozenset({"runner", "shoe"}))
    found = shop.search_products("do you have red running shoes")
    assert seen[0] == "red running shoe status:active"  # filler gone, plural made singular, all words together
    assert seen[1] == "running shoe status:active" or seen[1].startswith("shoe")  # then only words naming things the store sells
    assert shop._search_terms("do you have black sinks?") == ["black", "sink"]


def test_no_store_means_no_lookup(monkeypatch):
    monkeypatch.setattr(shop, "store_domain", "")
    assert context("Where is my order 1042?") == ("", None)
    assert shopify_module.ShopifyService().auth_mode in {"none", "admin_token", "client_credentials"}


def test_order_names_in_the_stores_own_format(monkeypatch):
    """Stores set their own order prefix ("DFO00" makes DFO001001); a caller may say it in full or just the number."""
    named = {"name": "DFO001001"}
    searches = []

    def fake_orders(search, limit):
        searches.append(search)
        if search is None:
            return [{"name": "DFO001000"}, named]
        return [named] if search == "name:DFO001001" else []

    monkeypatch.setattr(shop, "_orders", fake_orders)
    assert shop.orders("DFO 001001") == [named]
    assert shop.orders("dfo-001001") == [named]  # not an exact name match: found by its number
    assert shop.orders("1001") == [named]
    assert shop.orders("#1001") == [named]
    assert shop.orders("9999") == []
    assert shopify_module._order_number("DFO001001") == 1001 and shopify_module._order_number("#1042") == 1042


def test_order_number_is_heard_with_letters():
    find = lambda text: (brain_service.ORDER_NUMBER.search(text) or [None, None])[1]
    assert find("my order is DFO001001") == "DFO001001"
    assert find("order number dfo 001001 please") == "dfo 001001"
    assert find("where is order #1042") == "1042"
    assert find("order number is 1042") == "1042"


def test_local_and_international_numbers_match():
    assert phones_match("0412 345 678", "+61 412 345 678") and phones_match("+61412345678", "(04) 1234 5678")
    assert phones_match("+91 98765 43210", "9876543210") and not phones_match("0412 345 678", "+61 499 999 999")


def test_caller_orders_found_by_the_billing_or_shipping_phone(monkeypatch):
    node = {"name": "DFO001114", "createdAt": "2026-10-19T00:00:00Z", "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "FULFILLED",
            "email": "c@example.com", "phone": None, "customer": {"phone": None, "email": None},
            "billingAddress": {"phone": "0412 345 678"}, "shippingAddress": None}
    order = shop._normalize_order(node); order["_emails"] = sorted(order["_emails"])
    other = {**order, "name": "DFO001113", "_phones": ["0499 999 999"]}
    monkeypatch.setattr(shop, "_orders", lambda search, limit: [order, other])
    monkeypatch.setattr(shop, "_caller_orders", {})
    monkeypatch.setattr(shop, "_scopes", ["read_orders"])  # no read_customers: the order scan alone finds it
    assert [found["name"] for found in shop.orders_for_phone("+61412345678")] == ["DFO001114"]
    assert shop.orders_for_phone("+61400000000") == []


def test_caller_id_order_question_without_a_number(store):
    assert "DFO" not in context("what did I buy?", phone="+911234567890")[0]
    text = context("when will my order arrive?", phone="+919876543210")[0]
    assert "#1042" in text and "caller ID" in text


def test_the_email_alone_finds_the_callers_orders(monkeypatch, store):
    monkeypatch.setattr(shop, "orders_for_email", lambda email, limit=5: [ORDER] if email == "priya@example.com" else [])
    assert "#1042" in context("where is my order? my email is priya at example dot com")[0]
    assert "No recent order uses the email" in context("where is my order? it's other at example dot com")[0]


def test_a_product_named_in_any_words_is_looked_up(store):
    text, tool = context("got any trail runners left?")
    assert tool == "search_shopify_products" and "Trail Runner" in text


def test_the_callers_orders_are_known_whatever_they_ask(monkeypatch, store):
    monkeypatch.setattr(shop, "cached_orders_for_phone", lambda phone: [ORDER] if phones_match(phone, "+919876543210") else None)
    text, tool = context("the thing I got last week is leaking", phone="+919876543210")
    assert tool == "get_order" and "#1042" in text and "found by their caller ID" in text  # understood as being about their order
    text, _ = context("the thing I got last week is leaking", phone="+911111111111")
    assert "#1042" not in text and "Ask for their order number" in text  # someone else: no orders, asked for the number
    store["get_customer_orders"] = False
    assert "#1042" not in context("the thing I got last week is leaking", phone="+919876543210")[0]
