"""Live Shopify access for the AI Brain (GraphQL Admin API).

Two ways to connect:
  * client credentials - an app created in the Shopify Dev Dashboard (the only option for apps
    created from 2026). We exchange Client ID + secret for a 24-hour token and renew it automatically.
  * admin token        - a legacy custom app's permanent Admin API token (shpat_...).
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_API_VERSION = "2025-10"
SEARCH_STOP_WORDS = {
    "the", "and", "for", "with", "what", "how", "are", "can", "you", "your", "our", "this", "that", "from",
    "about", "have", "has", "any", "some", "price", "prices", "cost", "much", "stock", "available", "availability",
    "sell", "carry", "buy", "want", "need", "looking", "there", "they", "them", "does", "did", "please", "tell",
    "show", "know", "would", "like", "get", "got", "product", "products", "item", "items", "still", "left", "many",
    "size", "sizes", "colour", "color", "colors", "colours", "which", "where", "when", "is", "in", "do", "of", "a", "an", "it",
}


def normalize_store_domain(value: str) -> str:
    domain = (value or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    if domain and "." not in domain:
        domain = f"{domain}.myshopify.com"
    return domain


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def phones_match(left: Any, right: Any) -> bool:
    """Compare phone numbers by their last 10 digits so +91 98765 43210 matches 9876543210."""
    a, b = _digits(left), _digits(right)
    return len(a) >= 7 and len(b) >= 7 and a[-10:] == b[-10:]


class ShopifyService:
    def __init__(self) -> None:
        import os

        self.store_domain = normalize_store_domain(os.getenv("SHOPIFY_STORE_DOMAIN") or "")
        self.static_token = os.getenv("SHOPIFY_ACCESS_TOKEN") or ""
        self.client_id = os.getenv("SHOPIFY_CLIENT_ID") or ""
        self.client_secret = os.getenv("SHOPIFY_CLIENT_SECRET") or ""
        self.api_version = os.getenv("SHOPIFY_API_VERSION", DEFAULT_API_VERSION)
        self._token = ""
        self._token_expires_at = 0.0
        self._scopes: Optional[List[str]] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ connection
    def configure(self, store_domain: str, access_token: str = "", api_version: str = DEFAULT_API_VERSION,
                  client_id: str = "", client_secret: str = "") -> None:
        with self._lock:
            self.store_domain = normalize_store_domain(store_domain)
            self.static_token = access_token or ""
            self.client_id = client_id or ""
            self.client_secret = client_secret or ""
            self.api_version = api_version or DEFAULT_API_VERSION
            self._token, self._token_expires_at, self._scopes = "", 0.0, None

    @property
    def auth_mode(self) -> str:
        if self.client_id and self.client_secret:
            return "client_credentials"
        return "admin_token" if self.static_token else "none"

    @property
    def configured(self) -> bool:
        return bool(self.store_domain and self.auth_mode != "none")

    @property
    def access_token(self) -> str:
        """Kept for callers that only check whether a token exists."""
        return self.static_token or self._token

    def status(self) -> Dict[str, Any]:
        return {
            "connected": self.configured,
            "store_domain": self.store_domain,
            "api_version": self.api_version,
            "auth_mode": self.auth_mode,
            "mode": "live" if self.configured else "not_configured",
            "access_token_configured": bool(self.static_token),
            "client_id": self.client_id,
            "client_secret_configured": bool(self.client_secret),
            "token_expires_in": max(0, int(self._token_expires_at - time.time())) if self.auth_mode == "client_credentials" and self._token else None,
        }

    def _http(self, url: str, data: Optional[bytes], headers: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            hints = {
                400: "Shopify rejected the credentials. Check the Client ID and secret, and that the app is installed on this store.",
                401: "Shopify rejected the access token. Reinstall the app and paste the new token or secret.",
                403: "The app is missing a permission (scope) for this data. Add it in Shopify, release or save, then reinstall the app.",
                404: f"Store '{self.store_domain}' was not found. Use the exact myshopify.com domain from Shopify admin.",
            }
            raise RuntimeError(hints.get(exc.code, f"Shopify request failed ({exc.code})") + f" [details: {body[:160]}]") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach {self.store_domain}: {exc.reason}") from exc

    def _bearer(self) -> str:
        if not self.configured:
            raise RuntimeError("Shopify is not configured")
        if self.auth_mode == "admin_token":
            return self.static_token
        with self._lock:
            # Client-credential tokens last 24 hours; renew 10 minutes early.
            if self._token and time.time() < self._token_expires_at - 600:
                return self._token
            payload = self._http(
                f"https://{self.store_domain}/admin/oauth/access_token",
                urllib.parse.urlencode({
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }).encode(),
                {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            )
            token = payload.get("access_token")
            if not token:
                raise RuntimeError(f"Shopify did not return an access token: {str(payload)[:200]}")
            self._token = token
            self._token_expires_at = time.time() + int(payload.get("expires_in") or 86399)
            if payload.get("scope"):
                self._scopes = sorted({item.strip() for item in str(payload["scope"]).split(",") if item.strip()})
            return self._token

    def graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = self._http(
            f"https://{self.store_domain}/admin/api/{self.api_version}/graphql.json",
            json.dumps({"query": query, "variables": variables or {}}).encode(),
            {"X-Shopify-Access-Token": self._bearer(), "Content-Type": "application/json", "Accept": "application/json"},
        )
        if payload.get("errors") and not payload.get("data"):
            raise RuntimeError("Shopify: " + "; ".join(str(item.get("message", item)) for item in payload["errors"])[:300])
        return payload.get("data") or {}

    def access_scopes(self) -> List[str]:
        """Permissions actually granted to the installed app."""
        if self._scopes is not None:
            return self._scopes
        data = self.graphql("{ currentAppInstallation { accessScopes { handle } } }")
        self._scopes = sorted({item["handle"] for item in (data.get("currentAppInstallation") or {}).get("accessScopes", [])})
        return self._scopes

    def has_scope(self, scope: str) -> bool:
        try:
            granted = self.access_scopes()
        except Exception:
            return False
        # write_x implies read_x in Shopify.
        return scope in granted or scope.replace("read_", "write_", 1) in granted

    def shop(self) -> Dict[str, Any]:
        data = self.graphql("{ shop { name currencyCode myshopifyDomain contactEmail primaryDomain { url } } productsCount { count } }")
        shop = data.get("shop") or {}
        shop["products_count"] = (data.get("productsCount") or {}).get("count")
        return shop

    # ------------------------------------------------------------------ products
    def _product_fields(self) -> str:
        inventory = "inventoryQuantity" if self.has_scope("read_inventory") else ""
        return (
            "id title handle status productType vendor tags onlineStoreUrl description(truncateAt: 240) "
            + ("totalInventory " if inventory else "")
            + "priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } } "
            + f"variants(first: 25) {{ nodes {{ title price sku availableForSale {inventory} }} }}"
        )

    @staticmethod
    def _normalize_product(node: Dict[str, Any]) -> Dict[str, Any]:
        variants = [{
            "title": item.get("title"),
            "price": item.get("price"),
            "sku": item.get("sku"),
            "available": item.get("availableForSale"),
            "inventory_quantity": item.get("inventoryQuantity"),
        } for item in (node.get("variants") or {}).get("nodes", [])]
        price_range = node.get("priceRangeV2") or {}
        return {
            "id": node.get("id"),
            "title": node.get("title"),
            "handle": node.get("handle"),
            "status": str(node.get("status") or "").lower(),
            "product_type": node.get("productType"),
            "vendor": node.get("vendor"),
            "tags": node.get("tags") or [],
            "description": node.get("description"),
            "url": node.get("onlineStoreUrl"),
            "total_inventory": node.get("totalInventory"),
            "currency": (price_range.get("minVariantPrice") or {}).get("currencyCode"),
            "min_price": (price_range.get("minVariantPrice") or {}).get("amount"),
            "max_price": (price_range.get("maxVariantPrice") or {}).get("amount"),
            "variants": variants,
        }

    def products(self, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        data = self.graphql(
            f"query($n: Int!, $q: String) {{ products(first: $n, query: $q, sortKey: RELEVANCE) {{ nodes {{ {self._product_fields()} }} }} }}"
            if query else
            f"query($n: Int!) {{ products(first: $n, sortKey: TITLE) {{ nodes {{ {self._product_fields()} }} }} }}",
            {"n": limit, "q": query} if query else {"n": limit},
        )
        return [self._normalize_product(node) for node in (data.get("products") or {}).get("nodes", [])]

    @staticmethod
    def _search_terms(text: str) -> List[str]:
        words = re.findall(r"[a-z0-9][a-z0-9'-]{1,}", (text or "").lower())
        terms: List[str] = []
        for word in words:
            word = word.strip("'-")
            if len(word) >= 3 and word not in SEARCH_STOP_WORDS and word not in terms:
                terms.append(word)
        return terms[:5]

    def search_products(self, text: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Natural-language product search: OR the meaningful words, then rank by how many match."""
        terms = self._search_terms(text)
        if not terms:
            return []
        # Brackets matter: without them Shopify reads "a OR b status:active" as "a OR (b AND active)".
        found = self.products("(" + " OR ".join(f"{term}*" for term in terms) + ") status:active", limit=15)

        def score(product: Dict[str, Any]) -> int:
            haystack = " ".join([str(product.get("title") or ""), str(product.get("product_type") or ""), " ".join(product.get("tags") or [])]).lower()
            return sum(1 for term in terms if term in haystack)

        found.sort(key=score, reverse=True)
        return [item for item in found if score(item) > 0][:limit] or found[:limit]

    def product_context(self, query: str, include_stock: bool = True) -> str:
        products = self.search_products(query)
        if not products:
            return "Live Shopify catalog: no product matched what the caller asked for."
        lines = []
        for product in products:
            currency = product.get("currency") or ""
            low, high = product.get("min_price"), product.get("max_price")
            price = f"{low} {currency}" if low == high else f"{low}-{high} {currency}"
            line = f"- {product['title']}: price {price}"
            stock = product.get("total_inventory") if include_stock else None
            if stock is not None:
                line += f", {stock} in stock" if stock > 0 else ", out of stock"
            options = [v for v in product["variants"] if v.get("title") and v["title"] != "Default Title"]
            if options:
                parts = []
                for variant in options[:8]:
                    qty = variant.get("inventory_quantity")
                    if not include_stock:
                        state = "unavailable" if variant.get("available") is False else "available"
                    elif isinstance(qty, int) and qty > 0:
                        state = f"{qty} left"
                    else:
                        state = "sold out" if qty == 0 or variant.get("available") is False else "available"
                    parts.append(f"{variant['title']} {variant['price']} ({state})")
                line += "; options: " + ", ".join(parts)
            lines.append(line)
        heading = "current prices and stock" if include_stock else "current prices; do not quote stock counts"
        return f"Live Shopify catalog ({heading}):\n" + "\n".join(lines)

    # ------------------------------------------------------------------ orders & customers
    ORDER_FIELDS = (
        "name createdAt cancelledAt displayFinancialStatus displayFulfillmentStatus email phone "
        "totalPriceSet { shopMoney { amount currencyCode } } "
        "customer { phone email } "
        "lineItems(first: 10) { nodes { title quantity } } "
        "fulfillments(first: 5) { status updatedAt trackingInfo { company number url } }"
    )

    @staticmethod
    def _normalize_order(node: Dict[str, Any]) -> Dict[str, Any]:
        total = ((node.get("totalPriceSet") or {}).get("shopMoney") or {})
        customer = node.get("customer") or {}
        return {
            "name": node.get("name"),
            "created_at": node.get("createdAt"),
            "cancelled": bool(node.get("cancelledAt")),
            "financial_status": str(node.get("displayFinancialStatus") or "").replace("_", " ").lower(),
            "fulfillment_status": str(node.get("displayFulfillmentStatus") or "").replace("_", " ").lower(),
            "total": f"{total.get('amount')} {total.get('currencyCode')}" if total else None,
            "items": [f"{item.get('quantity')} x {item.get('title')}" for item in (node.get("lineItems") or {}).get("nodes", [])],
            "tracking": [
                {"company": t.get("company"), "number": t.get("number"), "url": t.get("url"), "status": f.get("status")}
                for f in node.get("fulfillments") or [] for t in f.get("trackingInfo") or []
            ],
            # Used only for verification; never returned to callers.
            "_emails": {e.lower() for e in (node.get("email"), customer.get("email")) if e},
            "_phones": [p for p in (node.get("phone"), customer.get("phone")) if p],
        }

    def orders(self, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        limit = min(max(limit, 1), 100)
        search = f"name:{query if query.startswith('#') else '#' + query}" if query else None
        data = self.graphql(
            f"query($n: Int!, $q: String) {{ orders(first: $n, query: $q, sortKey: CREATED_AT, reverse: true) {{ nodes {{ {self.ORDER_FIELDS} }} }} }}",
            {"n": limit, "q": search},
        )
        orders = [self._normalize_order(node) for node in (data.get("orders") or {}).get("nodes", [])]
        for order in orders:  # sets are not JSON serializable for API callers
            order["_emails"] = sorted(order["_emails"])
        return orders

    def orders_for_phone(self, phone: str, limit: int = 3) -> List[Dict[str, Any]]:
        digits = _digits(phone)
        if len(digits) < 7:
            return []
        data = self.graphql(
            f"query($q: String) {{ customers(first: 3, query: $q) {{ nodes {{ phone orders(first: {limit}, sortKey: CREATED_AT, reverse: true) {{ nodes {{ {self.ORDER_FIELDS} }} }} }} }} }}",
            {"q": f"phone:*{digits[-10:]}"},
        )
        orders: List[Dict[str, Any]] = []
        for customer in (data.get("customers") or {}).get("nodes", []):
            if phones_match(customer.get("phone"), phone):
                orders.extend(self._normalize_order(node) for node in (customer.get("orders") or {}).get("nodes", []))
        return orders[:limit]

    def customers(self, limit: int = 25) -> List[Dict[str, Any]]:
        data = self.graphql("query($n: Int!) { customers(first: $n) { nodes { id displayName numberOfOrders } } }", {"n": min(max(limit, 1), 100)})
        return (data.get("customers") or {}).get("nodes", [])

    @staticmethod
    def describe_order(order: Dict[str, Any]) -> str:
        if order["cancelled"]:
            state = "cancelled"
        else:
            state = f"payment {order['financial_status'] or 'unknown'}, fulfilment {order['fulfillment_status'] or 'unfulfilled'}"
        line = f"Order {order['name']} placed {str(order['created_at'])[:10]}: {state}; total {order['total']}; items: {', '.join(order['items']) or 'n/a'}"
        if order["tracking"]:
            line += "; tracking: " + ", ".join(f"{t['company'] or 'carrier'} {t['number'] or ''}".strip() for t in order["tracking"])
        return line

    # ------------------------------------------------------------------ store policies
    def policies(self) -> List[Dict[str, str]]:
        data = self.graphql("{ shop { shopPolicies { type title body } } }")
        rows = []
        for policy in (data.get("shop") or {}).get("shopPolicies", []):
            text = re.sub(r"<[^>]+>", " ", policy.get("body") or "")
            rows.append({"type": policy.get("type"), "title": policy.get("title"), "text": re.sub(r"\s+", " ", text).strip()})
        return rows


shopify_service = ShopifyService()
