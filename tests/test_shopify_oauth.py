"""Connect with Shopify: the callback trusts only a reply signed with the app secret for a state we issued."""

import hashlib
import hmac
import urllib.parse
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from main import app
from routers import shopify_api
from secret_store import encrypt_secret

SECRET = "shpss_test_secret"


class States:
    def __init__(self, docs):
        self.docs = {doc["_id"]: doc for doc in docs}

    def find_one_and_delete(self, query):
        return self.docs.pop(query["_id"], None)


@pytest.fixture
def flow(monkeypatch):
    saved, configured = {}, {}
    states = States([{"_id": "good-state", "shop": "house-mart1.myshopify.com", "client_id": "cid", "client_secret": encrypt_secret(SECRET),
                      "return_to": "http://localhost:3000", "by": "admin@example.com", "created_at": datetime.utcnow()},
                     {"_id": "old-state", "shop": "house-mart1.myshopify.com", "client_id": "cid", "client_secret": encrypt_secret(SECRET),
                      "return_to": "http://localhost:3000", "created_at": datetime.utcnow() - timedelta(hours=1)}])
    monkeypatch.setattr(shopify_api.mongo_client, "shopify_oauth_states", states, raising=False)
    monkeypatch.setattr(shopify_api, "_upsert_setting", lambda key, value: saved.update({key: value}))
    monkeypatch.setattr(shopify_api, "_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(shopify_api.shopify_service, "_http", lambda url, data, headers: {"access_token": "shpat_from_oauth", "scope": "read_products"})
    monkeypatch.setattr(shopify_api.shopify_service, "configure", lambda shop, token, version, **kwargs: configured.update(shop=shop, token=token))
    return saved, configured


def signed(params, secret=SECRET):
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    return {**params, "hmac": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}


def callback(params):
    return TestClient(app).get("/api/platform/shopify/oauth/callback?" + urllib.parse.urlencode(params), follow_redirects=False)


def test_a_signed_reply_connects_the_store(flow):
    saved, configured = flow
    response = callback(signed({"code": "abc", "shop": "house-mart1.myshopify.com", "state": "good-state", "timestamp": "1700000000"}))
    assert response.status_code == 302 and response.headers["location"] == "http://localhost:3000/integrations/shopify?shopify_connected=1"
    assert configured == {"shop": "house-mart1.myshopify.com", "token": "shpat_from_oauth"}
    assert saved["shopify"]["connected_via"] == "oauth" and saved["shopify"]["client_secret"] == ""
    assert "shpat_from_oauth" not in str(saved["shopify"]["access_token"])  # stored encrypted


def test_a_forged_signature_is_refused(flow):
    saved, configured = flow
    response = callback(signed({"code": "abc", "shop": "house-mart1.myshopify.com", "state": "good-state", "timestamp": "1"}, secret="wrong"))
    assert "shopify_error=" in response.headers["location"] and not saved and not configured


def test_an_unknown_or_expired_state_is_refused(flow):
    saved, _ = flow
    for state in ("missing", "old-state"):
        response = callback(signed({"code": "abc", "shop": "house-mart1.myshopify.com", "state": state, "timestamp": "1"}))
        assert "shopify_error=" in response.headers["location"]
    assert not saved


def test_another_store_is_refused(flow):
    saved, _ = flow
    response = callback(signed({"code": "abc", "shop": "someone-else.myshopify.com", "state": "good-state", "timestamp": "1"}))
    assert "shopify_error=" in response.headers["location"] and not saved
