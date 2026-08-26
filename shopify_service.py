import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional


class ShopifyService:
    def __init__(self) -> None:
        self.store_domain = (os.getenv("SHOPIFY_STORE_DOMAIN") or "").replace("https://", "").rstrip("/")
        self.access_token = os.getenv("SHOPIFY_ACCESS_TOKEN") or ""
        self.api_version = os.getenv("SHOPIFY_API_VERSION", "2025-10")

    def configure(self, store_domain: str, access_token: str, api_version: str = "2025-10") -> None:
        self.store_domain = (store_domain or "").replace("https://", "").replace("http://", "").rstrip("/")
        self.access_token = access_token or ""
        self.api_version = api_version or "2025-10"

    @property
    def configured(self) -> bool:
        return bool(self.store_domain and self.access_token)

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.configured,
            "store_domain": self.store_domain,
            "api_version": self.api_version,
            "mode": "live" if self.configured else "not_configured",
            "access_token_configured": bool(self.access_token),
        }

    def _get(self, resource: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Shopify is not configured")
        query = urllib.parse.urlencode(params or {})
        suffix = f"?{query}" if query else ""
        url = f"https://{self.store_domain}/admin/api/{self.api_version}/{resource}.json{suffix}"
        request = urllib.request.Request(
            url,
            headers={"X-Shopify-Access-Token": self.access_token, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Shopify request failed ({exc.code}): {body[:300]}") from exc

    def products(self, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        payload = self._get("products", {"limit": min(max(limit, 1), 100), "title": query} if query else {"limit": min(max(limit, 1), 100)})
        return payload.get("products", [])

    def orders(self, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": min(max(limit, 1), 100), "status": "any"}
        if query:
            params["name"] = query
        return self._get("orders", params).get("orders", [])

    def customers(self, limit: int = 25) -> List[Dict[str, Any]]:
        return self._get("customers", {"limit": min(max(limit, 1), 100)}).get("customers", [])

    def product_context(self, query: str) -> str:
        products = self.products(query=query, limit=5)
        if not products:
            return "No matching Shopify products were found."
        lines = []
        for product in products:
            variants = product.get("variants") or []
            prices = sorted({str(item.get("price")) for item in variants if item.get("price")})
            inventory = sum(int(item.get("inventory_quantity") or 0) for item in variants)
            lines.append(f"{product.get('title')}: prices {', '.join(prices) or 'unavailable'}, inventory {inventory}, status {product.get('status', 'unknown')}")
        return "Live Shopify matches:\n" + "\n".join(lines)


shopify_service = ShopifyService()
