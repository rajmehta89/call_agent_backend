"""Regressions for the access, credential and fetching findings in the CRM review."""

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import auth_service
import safe_http
import twilio_security
from routers import calls_api, platform_api, retell_api, shopify_api
from shopify_service import normalize_store_domain


@pytest.fixture
def agent_client(monkeypatch):
    user = {"_id": ObjectId(), "role": "agent", "workspace_id": "default"}
    monkeypatch.setattr(auth_service, "role_document", lambda name: next(r for r in auth_service.DEFAULT_ROLES if r["name"] == name))
    app = FastAPI()
    app.include_router(platform_api.router)
    app.include_router(calls_api.router)
    app.include_router(shopify_api.router)
    app.dependency_overrides[auth_service.require_user] = lambda: user
    return TestClient(app), user


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/platform/export/leads.csv", None),
    ("put", "/api/platform/settings", {"value": {"business": {"name": "changed"}}}),
    ("put", "/api/platform/email-policy", {"value": {"approval_required": False}}),
    ("post", "/api/platform/email-campaign/action", {"value": {"action": "start"}}),
    ("post", "/api/platform/campaigns", {"value": {"name": "changed"}}),
    ("post", "/api/platform/customers", {"name": "changed", "phone": "+15550001111"}),
    ("get", "/api/platform/activity", None),
])
def test_agent_cannot_reach_administrative_operations(agent_client, method, path, body):
    client, _ = agent_client
    response = client.request(method, path, json=body)
    assert response.status_code == 403


def test_search_uses_the_same_record_scopes_as_primary_lists(agent_client, monkeypatch):
    client, user = agent_client
    database = MagicMock()
    owned = ObjectId()
    database.leads.find.return_value = [{"_id": owned, "phone": "+15550001111"}]
    # Return empty cursor results while retaining each actual Mongo query for assertions.
    class Cursor(list):
        def limit(self, count): return self
    def leads(query, projection=None):
        return Cursor([{"_id": owned, "phone": "+15550001111"}]) if projection else Cursor()
    database.leads.find.side_effect = leads
    database.calls.find.return_value = Cursor()
    database.customers.find.return_value = Cursor()
    monkeypatch.setattr(platform_api, "mongo_client", database)
    response = client.get("/api/platform/search", params={"q": ".*"})
    assert response.status_code == 200
    lead_queries = [call.args[0] for call in database.leads.find.call_args_list if len(call.args) == 1]
    assert lead_queries and {"assigned_to": str(user["_id"])} in lead_queries[0]["$and"]
    assert {"lead_id": {"$in": [owned, str(owned)]}} in database.calls.find.call_args.args[0]["$and"]
    assert lead_queries[0]["$and"][0]["$or"][0]["name"]["$regex"] == r"\.\*"  # user search is literal, not executable regex
    database.whatsapp_conversations.find.assert_not_called()


def test_provider_record_writes_are_not_a_user_api(agent_client, monkeypatch):
    client, user = agent_client
    logger = MagicMock()
    monkeypatch.setattr(calls_api, "log_call", logger)
    for role in ("agent", "admin"):
        user["role"] = role
        response = client.post("/api/calls", json={"phone_number": "+15550001111", "call_data": {"call_session_id": "other-call", "analysis_status": "complete"}})
        assert response.status_code == 403
    logger.assert_not_called()


def test_twilio_rejects_requests_when_verification_is_unconfigured(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    app = FastAPI()
    @app.post("/hook", dependencies=[Depends(twilio_security.verify_twilio_request)])
    def hook(): return {"ok": True}
    assert TestClient(app).post("/hook", data={"CallSid": "CA-test"}).status_code == 503


def test_stream_tokens_cannot_be_signed_with_an_empty_secret(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    with pytest.raises(RuntimeError):
        twilio_security.stream_token("CA-test")
    assert not twilio_security.valid_stream_token("CA-test", "0.forged")


def test_retell_socket_rejects_missing_verification_secret(monkeypatch):
    monkeypatch.setenv("RETELL_CUSTOM_LLM_SECRET", "")
    monkeypatch.setattr(retell_api, "_enabled", lambda: True)
    class Socket:
        query_params = {}
        accepted = False
        async def accept(self): self.accepted = True
        async def close(self, **kwargs): self.closed = kwargs
    socket = Socket()
    asyncio.run(retell_api.retell_custom_llm_websocket(socket, "unverified-call"))
    assert not socket.accepted and socket.closed["code"] == 1008


@pytest.mark.parametrize("host", ["receiver.example.com", "good.myshopify.com@receiver.example.com", "good.myshopify.com:443", "127.0.0.1", "good.myshopify.com.evil.com"])
def test_shopify_rejects_untrusted_authorities(host):
    with pytest.raises(ValueError): normalize_store_domain(host)


def test_shopify_store_change_requires_new_credentials(agent_client, monkeypatch):
    client, user = agent_client
    user["role"] = "admin"
    database = MagicMock()
    database.platform_settings.find_one.return_value = {"value": {"store_domain": "old.myshopify.com", "access_token": "saved-dummy-token"}}
    service = SimpleNamespace(store_domain="old.myshopify.com", status=lambda: {}, configure=MagicMock())
    monkeypatch.setattr(shopify_api, "mongo_client", database)
    monkeypatch.setattr(shopify_api, "shopify_service", service)
    monkeypatch.setattr(shopify_api, "_require_db", lambda: None)
    response = client.put("/api/platform/shopify/configure", json={"store_domain": "other.myshopify.com", "auth_mode": "admin_token"})
    assert response.status_code == 400
    service.configure.assert_not_called()


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fc00::1", "::ffff:127.0.0.1"])
def test_crawler_rejects_internal_dns_answers(monkeypatch, address):
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 80))])
    with pytest.raises(ValueError): safe_http.public_destination("http://public.example/page")


def test_crawler_revalidates_redirects_and_pins_public_dns(monkeypatch):
    resolved, connected = [], []
    def resolve(host, *a, **k):
        resolved.append(host)
        return [(2, 1, 6, "", ("127.0.0.1" if host == "internal.example" else "8.8.8.8", 80))]
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(safe_http.socket, "create_connection", lambda address, **k: connected.append(address))
    class Connection:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): self._create_connection(("public.example", 80), 15)
        def getresponse(self): return SimpleNamespace(status=302, getheader=lambda key, default=None: "http://internal.example/private")
        def close(self): pass
    monkeypatch.setattr(safe_http.http.client, "HTTPConnection", Connection)
    with pytest.raises(ValueError): safe_http.fetch_public_text("http://public.example/page")
    assert resolved == ["public.example", "internal.example"] and connected == [("8.8.8.8", 80)]


def test_crawler_bounds_response_size(monkeypatch):
    monkeypatch.setattr(safe_http, "public_destination", lambda url: (safe_http.urlsplit(url), 80, "8.8.8.8"))
    monkeypatch.setattr(safe_http, "MAX_WEBSITE_BYTES", 4)
    class Connection:
        def __init__(self, *a, **k): pass
        def request(self, *a, **k): pass
        def getresponse(self):
            return SimpleNamespace(status=200, getheader=lambda key, default="": "text/html" if key == "Content-Type" else default, read=io.BytesIO(b"12345").read)
        def close(self): pass
    monkeypatch.setattr(safe_http.http.client, "HTTPConnection", Connection)
    with pytest.raises(ValueError, match="too large"): safe_http.fetch_public_text("http://public.example/page")
