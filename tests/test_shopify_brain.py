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
    assert "linked to the caller's phone" in context("Has my order shipped?", "+919876543210")[0]
    store["get_customer_orders"] = False
    assert "Ask the caller for their order number" in context("Has my order shipped?", "+919876543210")[0]


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


def test_search_query_brackets_the_or_terms(monkeypatch, store):
    seen = {}

    def capture(q="", limit=25):
        seen["q"] = q
        return []

    monkeypatch.setattr(shop, "products", capture)
    shop.search_products("red running shoes")
    assert seen["q"].startswith("(") and seen["q"].endswith(") status:active")


def test_no_store_means_no_lookup(monkeypatch):
    monkeypatch.setattr(shop, "store_domain", "")
    assert context("Where is my order 1042?") == ("", None)
    assert shopify_module.ShopifyService().auth_mode in {"none", "admin_token", "client_credentials"}
