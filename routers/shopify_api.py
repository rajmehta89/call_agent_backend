"""Shopify connection and data endpoints for the dashboard (/api/platform/shopify/...)."""

from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth_service import require_permission, require_user
from mongo_client import mongo_client
from routers.platform_api import _audit, _require_db, _serialize, _upsert_setting
from secret_store import decrypt_secret, encrypt_secret
from shopify_service import shopify_service

router = APIRouter(prefix="/api/platform/shopify", tags=["shopify"], dependencies=[Depends(require_user)])


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
async def shopify_status(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    _apply_saved_shopify_config()
    data = shopify_service.status()
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
async def configure_shopify(payload: ShopifyConfigPayload, user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    _require_db()
    before = shopify_service.status()
    saved = mongo_client.platform_settings.find_one({"key": "shopify"}) or {}
    saved_values = saved.get("value", {}) if saved else {}
    # Blank secret fields keep what is already saved, so the form never has to show secrets again.
    if payload.auth_mode == "admin_token":
        token = payload.access_token.strip() or decrypt_secret(str(saved_values.get("access_token") or ""))
        client_id = client_secret = ""
    else:
        token = ""
        client_id = payload.client_id.strip() or str(saved_values.get("client_id") or "")
        client_secret = payload.client_secret.strip() or decrypt_secret(str(saved_values.get("client_secret") or ""))
    shopify_service.configure(payload.store_domain.strip(), token, payload.api_version.strip() or "2025-10", client_id=client_id, client_secret=client_secret)
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
async def shopify_scopes(user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
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
async def shopify_products(search: str = "", limit: int = Query(25, ge=1, le=100), user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    try:
        return {"success": True, "data": shopify_service.products(search, limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/orders")
async def shopify_orders(search: str = "", limit: int = Query(25, ge=1, le=100), user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
    try:
        return {"success": True, "data": shopify_service.orders(search, limit)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/sync")
async def shopify_sync(scope: str = "all", user: Dict[str, Any] = Depends(require_permission("manage_integrations"))):
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
