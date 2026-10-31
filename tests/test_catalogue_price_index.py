"""Exercise the real catalogue build, not just an already-priced fake index."""

import time

import pytest

from shopify_service import ShopifyService


def price_node(identity, amount, stock=4):
    return {"id": identity, "totalInventory": stock, "priceRangeV2": {
        "minVariantPrice": {"amount": amount, "currencyCode": "AUD"},
        "maxVariantPrice": {"amount": amount, "currencyCode": "AUD"},
    }}


@pytest.mark.parametrize("inventory_scope", [True, False])
def test_catalogue_build_includes_prices_in_one_paginated_pass(monkeypatch, inventory_scope):
    service = ShopifyService()
    service._scopes = ["read_inventory"] if inventory_scope else []
    monkeypatch.setenv("STORE_SEARCH_MEANING_PERCENT", "0")
    calls = []

    def graphql(query, variables):
        calls.append((query, variables))
        assert "products(first: 250" in query and "priceRangeV2" in query and "variants(" not in query
        assert ("totalInventory" in query) is inventory_scope
        if variables["after"] is None:
            return {"products": {"nodes": [{**price_node("a", "130"), "title": "Basin"}],
                                 "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}
        assert variables["after"] == "next"
        return {"products": {"nodes": [{**price_node("b", "699"), "title": "Bath"}], "pageInfo": {"hasNextPage": False}}}

    monkeypatch.setattr(service, "graphql", graphql)
    service._index_building.set()
    service._build_index()
    products = service._index[0]
    assert [(p["title"], p["min_price"], p["currency"]) for p in products] == [
        ("Basin", "130", "AUD"), ("Bath", "699", "AUD")]
    assert len(calls) == 2 and not service._index_building.is_set()


def test_failed_next_page_keeps_the_previous_complete_catalogue(monkeypatch):
    service = ShopifyService()
    service._scopes = []
    monkeypatch.setenv("STORE_SEARCH_MEANING_PERCENT", "0")
    old_index = ([service._normalize_product(price_node("old", "130"))], time.time() - 700)
    service._index = old_index

    def graphql(query, variables):
        if variables["after"]:
            raise RuntimeError("next page unavailable")
        return {"products": {"nodes": [price_node("a", "149")],
                             "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}

    monkeypatch.setattr(service, "graphql", graphql)
    service._build_index()
    assert service._index is old_index
    assert not service._index_building.is_set()


def test_failed_rebuild_does_not_make_old_prices_fresh(monkeypatch):
    service = ShopifyService()
    old_index = ([service._normalize_product(price_node("a", "130"))], time.time() - 700)
    service._index = old_index
    monkeypatch.setattr(service, "graphql", lambda *args: (_ for _ in ()).throw(RuntimeError("store unavailable")))
    service._build_index()
    assert service._index is old_index
    assert service._index_retry_at > time.time()


@pytest.mark.parametrize("age, max_age", [(700, "600"), (1, "0")])
def test_expired_catalogue_prices_are_read_live_before_answering(monkeypatch, age, max_age):
    service = ShopifyService()
    product = service._normalize_product(price_node("a", "130"))
    service._index = ([product], time.time() - age)
    monkeypatch.setenv("SHOPIFY_LIVE_SECONDS", "0")
    monkeypatch.setenv("SHOPIFY_LIVE_MAX_SECONDS", max_age)
    reads = []

    def read_live(ids):
        reads.extend(ids)
        return {"a": service._normalize_product(price_node("a", "149"))}

    monkeypatch.setattr(service, "_read_live", read_live)
    monkeypatch.setattr(service, "_refresh_live_later", lambda *args: pytest.fail("expired prices cannot answer first"))
    assert service._with_live_prices([product])[0]["min_price"] == "149"
    assert reads == ["a"]
