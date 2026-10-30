"""Shopify connection and data endpoints for the dashboard (/api/platform/shopify/...)."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from auth_service import require_permission, require_user
from mongo_client import mongo_client
from routers.platform_api import _audit, _require_db, _serialize, _upsert_setting
from secret_store import decrypt_secret, encrypt_secret
from shopify_service import normalize_store_domain, shopify_service

logger = logging.getLogger(__name__)
SHOP_DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]*\.myshopify\.com")
router = APIRouter(prefix="/api/platform/shopify", tags=["shopify"], dependencies=[Depends(require_user)])
# Shopify's redirect after "Connect with Shopify" carries no RelayDesk login; it is checked by HMAC and a one-time state.
public_router = APIRouter(prefix="/api/platform/shopify", tags=["shopify"])


class ShopifyConfigPayload(BaseModel):
    store_domain: str
    auth_mode: str = "client_credentials"  # or "admin_token"
    access_token: str = ""
    client_id: str = ""
    client_secret: str = ""
    api_version: str = "2025-10"
    test_connection: bool = True


def _apply_saved_shopify_config() -> None:
    if not mongo_client.is_connected():
        return
    saved = mongo_client.platform_settings.find_one({"key": "shopify"})
    values = dict(saved.get("value", {})) if saved else {}
    for field in ("access_token", "client_secret"):
        values[field] = decrypt_secret(values.get(field) or "")
    if values.get("store_domain") and (values.get("access_token") or (values.get("client_id") and values.get("client_secret"))):
        signature = (values.get("store_domain"), values.get("access_token"), values.get("client_id"), values.get("client_secret"), values.get("api_version"))
        current = (shopify_service.store_domain, shopify_service.static_token, shopify_service.client_id, shopify_service.client_secret, shopify_service.api_version)
        if signature != current:  # re-configuring would throw away a cached 24-hour token
            shopify_service.configure(
                values["store_domain"],
                values.get("access_token", ""),
                values.get("api_version", "2025-10"),
                client_id=values.get("client_id", ""),
                client_secret=values.get("client_secret", ""),
            )


_apply_saved_shopify_config()


@router.get("/status")
def shopify_status(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    _apply_saved_shopify_config()
    saved = (mongo_client.platform_settings.find_one({"key": "shopify"}) or {}).get("value", {}) if mongo_client.is_connected() else {}
    data = {**shopify_service.status(), "oauth_redirect_uri": _oauth_redirect_uri(), "connected_via": saved.get("connected_via"),
            "app_client_id": saved.get("app_client_id") or saved.get("client_id") or ""}
    if shopify_service.configured:
        try:
            shop = shopify_service.shop()
            data.update(shop_name=shop.get("name"), currency=shop.get("currencyCode"), products_count=shop.get("products_count"), api_status="connected")
            data = {**data, **shopify_service.status()}  # token expiry is known after the first call
        except Exception as exc:
            data["api_status"] = "error"
            data["error"] = str(exc)
    return {"success": True, "data": data}


@router.put("/configure")
def configure_shopify(payload: ShopifyConfigPayload, user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    _require_db()
    try:
        domain = normalize_store_domain(payload.store_domain)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not domain or payload.auth_mode not in {"admin_token", "client_credentials"}:
        raise HTTPException(status_code=400, detail="Choose a Shopify store and a supported authentication mode")
    before = shopify_service.status()
    saved = mongo_client.platform_settings.find_one({"key": "shopify"}) or {}
    saved_values = saved.get("value", {}) if saved else {}
    # Secrets belong to one store. A host change requires fresh authorization.
    same_store = domain == normalize_store_domain(saved_values.get("store_domain") or shopify_service.store_domain)
    if payload.auth_mode == "admin_token":
        token = payload.access_token.strip() or (decrypt_secret(str(saved_values.get("access_token") or "")) if same_store else "")
        client_id = client_secret = ""
    else:
        token = ""
        client_id = payload.client_id.strip() or (str(saved_values.get("client_id") or "") if same_store else "")
        client_secret = payload.client_secret.strip() or (decrypt_secret(str(saved_values.get("client_secret") or "")) if same_store else "")
    if not token and not (client_id and client_secret):
        raise HTTPException(status_code=400, detail="Enter credentials for this store or connect with Shopify")
    try:
        shopify_service.configure(domain, token, payload.api_version.strip() or "2025-10", client_id=client_id, client_secret=client_secret)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    stored = {"store_domain": shopify_service.store_domain, "access_token": encrypt_secret(token), "client_id": client_id, "client_secret": encrypt_secret(client_secret), "api_version": shopify_service.api_version}
    _upsert_setting("shopify", stored)
    data = shopify_service.status()
    if payload.test_connection and shopify_service.configured:
        try:
            shop = shopify_service.shop()
            data = {**shopify_service.status(), "shop_name": shop.get("name"), "products_count": shop.get("products_count"), "api_status": "connected"}
        except Exception as exc:
            data["api_status"] = "error"
            data["error"] = str(exc)
    _audit("update", "shopify", before=before, after={"store_domain": shopify_service.store_domain, "api_version": shopify_service.api_version, "auth_mode": shopify_service.auth_mode})
    return {"success": True, "data": data}


@router.get("/scopes")
def shopify_scopes(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    if not shopify_service.configured:
        return {"success": True, "data": {"granted": [], "required": ["read_products", "read_inventory"]}}
    try:
        return {"success": True, "data": {
            "granted": shopify_service.access_scopes(),
            "required": ["read_products", "read_inventory"],
        }}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/products")
def shopify_products(search: str = "", limit: int = Query(25, ge=1, le=100), user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    try:
        return {"success": True, "data": shopify_service.products(search, limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/orders")
def shopify_orders(search: str = "", limit: int = Query(25, ge=1, le=100), user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    try:
        # Emails and phones on an order are only for verifying callers inside the backend; never sent to the browser.
        return {"success": True, "data": [{key: value for key, value in order.items() if not key.startswith("_")} for order in shopify_service.orders(search, limit)]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


OAUTH_SCOPES = "read_products,read_inventory,read_orders,read_customers,read_legal_policies"
OAUTH_STATE_MINUTES = 15


def _oauth_redirect_uri() -> str:
    """Where Shopify sends the browser back after approval: this backend's public HTTPS address. Add it to the app's
    Allowed redirection URL(s) in the Shopify Dev Dashboard."""
    base = (os.getenv("SHOPIFY_PUBLIC_BASE_URL") or os.getenv("TWILIO_PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/api/platform/shopify/oauth/callback" if base else ""


def _app_credentials() -> tuple[str, str]:
    """The app's Client ID and secret: saved by the form, kept separately once a connection token replaces them."""
    saved = (mongo_client.platform_settings.find_one({"key": "shopify"}) or {}).get("value", {})
    client_id = saved.get("client_id") or saved.get("app_client_id") or ""
    secret = decrypt_secret(str(saved.get("client_secret") or saved.get("app_client_secret") or ""))
    return client_id, secret


class ShopifyConnectPayload(BaseModel):
    store_domain: str
    client_id: str = ""
    client_secret: str = ""
    return_to: str = ""
    all_orders: bool = False  # also ask for orders older than 60 days (read_all_orders; Shopify may ask the app to request it)


@router.post("/oauth/start")
def shopify_oauth_start(payload: ShopifyConnectPayload, user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    """Step 1 of "Connect with Shopify": the store owner approves the app in Shopify, which works whichever Shopify
    organization the app belongs to (the Client ID + secret method needs app and store in the same one)."""
    _require_db()
    shop = normalize_store_domain(payload.store_domain)
    if not SHOP_DOMAIN.fullmatch(shop):
        raise HTTPException(status_code=400, detail="Use the store's .myshopify.com domain")
    saved_id, saved_secret = _app_credentials()
    client_id, client_secret = payload.client_id.strip() or saved_id, payload.client_secret.strip() or saved_secret
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Enter the app's Client ID and Client secret first")
    redirect_uri = _oauth_redirect_uri()
    if not redirect_uri.startswith("https://"):
        raise HTTPException(status_code=500, detail="The backend has no public HTTPS address (SHOPIFY_PUBLIC_BASE_URL or TWILIO_PUBLIC_BASE_URL)")
    allowed = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if origin.strip()]
    return_to = payload.return_to.rstrip("/") if payload.return_to.rstrip("/") in allowed + [os.getenv("FRONTEND_ORIGIN", "").rstrip("/")] else (os.getenv("FRONTEND_ORIGIN") or allowed[0]).rstrip("/")
    state = secrets.token_urlsafe(24)
    mongo_client.shopify_oauth_states.insert_one({"_id": state, "shop": shop, "client_id": client_id, "client_secret": encrypt_secret(client_secret),
                                                  "return_to": return_to, "by": user.get("email"), "created_at": datetime.utcnow()})
    scopes = OAUTH_SCOPES + (",read_all_orders" if payload.all_orders else "")
    url = f"https://{shop}/admin/oauth/authorize?" + urllib.parse.urlencode({"client_id": client_id, "scope": scopes, "redirect_uri": redirect_uri, "state": state})
    logger.info("shopify: %s started Connect with Shopify for %s (redirect %s)", user.get("email"), shop, redirect_uri)
    return {"success": True, "data": {"url": url, "redirect_uri": redirect_uri}}


@public_router.get("/oauth/callback")
def shopify_oauth_callback(request: Request):
    """Step 2: Shopify sends the browser here with a one-time code; check it really came from Shopify (HMAC with the
    app secret, the state we issued) and swap it for a permanent store access token."""
    params = dict(urllib.parse.parse_qsl(request.url.query, keep_blank_values=True))
    state = mongo_client.shopify_oauth_states.find_one_and_delete({"_id": params.get("state", "")}) if params.get("state") else None
    back = (state or {}).get("return_to") or (os.getenv("FRONTEND_ORIGIN") or "http://localhost:3000").rstrip("/")
    page = f"{back}/integrations/shopify"

    def fail(reason: str) -> RedirectResponse:
        logger.warning("shopify: Connect with Shopify failed: %s", reason)
        return RedirectResponse(f"{page}?shopify_error={urllib.parse.quote(reason)}", status_code=302)

    if not state or datetime.utcnow() - state["created_at"] > timedelta(minutes=OAUTH_STATE_MINUTES):
        return fail("The connection request expired. Click Connect with Shopify again.")
    shop, secret = params.get("shop", ""), decrypt_secret(state["client_secret"])
    if shop != state["shop"] or not SHOP_DOMAIN.fullmatch(shop):
        return fail("Shopify returned a different store than the one requested.")
    message = "&".join(f"{key}={value}" for key, value in sorted(params.items()) if key not in ("hmac", "signature"))
    if not hmac.compare_digest(hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest(), params.get("hmac", "")):
        return fail("The reply did not come from Shopify (signature check failed). Check the Client secret.")
    try:
        body = json.dumps({"client_id": state["client_id"], "client_secret": secret, "code": params.get("code", "")}).encode()
        token = shopify_service._http(f"https://{shop}/admin/oauth/access_token", body, {"Content-Type": "application/json", "Accept": "application/json"})
    except Exception as exc:
        return fail(f"Shopify did not issue an access token: {exc}")
    if not token.get("access_token"):
        return fail("Shopify did not issue an access token.")
    # Connected with the store token; the app's ID and secret are kept (encrypted) only to reconnect later.
    shopify_service.configure(shop, token["access_token"], shopify_service.api_version or "2025-10")
    _upsert_setting("shopify", {"store_domain": shop, "access_token": encrypt_secret(token["access_token"]), "client_id": "", "client_secret": "",
                                "app_client_id": state["client_id"], "app_client_secret": state["client_secret"], "api_version": shopify_service.api_version,
                                "connected_via": "oauth", "scopes": token.get("scope", "")})
    _audit("update", "shopify", before={}, after={"store_domain": shop, "auth_mode": "oauth", "by": state.get("by")})
    logger.info("shopify: connected %s with Connect with Shopify (scopes %s)", shop, token.get("scope"))
    return RedirectResponse(f"{page}?shopify_connected=1", status_code=302)


def _store_read(read, *args):
    """Run a Store data page read; Shopify errors come back as a readable 503."""
    _apply_saved_shopify_config()
    if not shopify_service.configured:
        raise HTTPException(status_code=400, detail="Connect the store first")
    try:
        return {"success": True, "data": read(*args)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/overview")
def shopify_overview(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return _store_read(shopify_service.overview)


@router.get("/catalog")
def shopify_catalog(search: str = "", after: str = "", user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return _store_read(shopify_service.catalog, search.strip()[:200], after[:400])


def _public_order(order: Dict[str, Any]) -> Dict[str, Any]:
    """An order without the emails and phones kept for verifying callers: those never go to the browser."""
    return {key: value for key, value in order.items() if not key.startswith("_")}


@router.get("/order-list")
def shopify_order_list(search: str = "", after: str = "", user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    """Orders for the Store data page: one order by number when searching, else a page of the newest."""
    def read() -> Dict[str, Any]:
        if search.strip():
            return {"orders": [_public_order(order) for order in shopify_service.orders(search.strip()[:60], 10)], "next": None}
        page = shopify_service.order_page(after[:400])
        return {"orders": [_public_order(order) for order in page["orders"]], "next": page["next"]}
    return _store_read(read)


@router.get("/customers")
def shopify_customers(search: str = "", after: str = "", user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return _store_read(shopify_service.customer_list, search.strip()[:200], after[:400])


@router.get("/health")
def shopify_catalog_health(refresh: bool = False, user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    """Products with problems that make the AI's answers weak or wrong, each with its Shopify admin link."""
    return _store_read(shopify_service.catalog_health, refresh)


@router.get("/policies")
def shopify_policies(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    return _store_read(shopify_service.policies)


class ShopifyAskPayload(BaseModel):
    question: str
    phone: str = ""


@router.post("/ask")
def shopify_ask(payload: ShopifyAskPayload, user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    """What the AI would get from the store for a caller's question (and caller ID): the same lookup as on a call."""
    from brain_service import brain_service
    question = payload.question.strip()[:500]
    if not question:
        raise HTTPException(status_code=400, detail="Type a question")
    phone = payload.phone.strip() or None
    if phone:
        shopify_service.orders_for_phone(phone)  # on a call this lookup runs while the caller says hello
    context, tool = brain_service._shopify_context(question, phone, question)
    logger.info("shopify: %s tested %r -> %s", user.get("email"), question, tool or "no lookup")
    return {"success": True, "data": {"tool": tool, "context": context}}


@router.post("/sync")
def shopify_sync(scope: str = "all", user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    _require_db()
    if not shopify_service.configured:
        raise HTTPException(status_code=400, detail="Shopify is not configured")
    started = datetime.utcnow()
    try:
        counts = {
            "products": len(shopify_service.products(limit=100)) if scope in {"all", "products", "inventory"} else 0,
            "orders": len(shopify_service.orders(limit=100)) if scope in {"all", "orders"} else 0,
            "customers": len(shopify_service.customers(limit=100)) if scope in {"all", "customers"} else 0,
        }
        record = {"scope": scope, "status": "success", "counts": counts, "created_at": started, "completed_at": datetime.utcnow()}
        mongo_client.shopify_sync_history.insert_one(record)
        return {"success": True, "data": _serialize(record)}
    except Exception as exc:
        mongo_client.shopify_sync_history.insert_one({"scope": scope, "status": "error", "error": str(exc), "created_at": started})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
