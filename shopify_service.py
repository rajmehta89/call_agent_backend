"""Live Shopify access for the AI Brain (GraphQL Admin API).

Two ways to connect:
  * client credentials - an app created in the Shopify Dev Dashboard (the only option for apps
    created from 2026). We exchange Client ID + secret for a 24-hour token and renew it automatically.
  * admin token        - a legacy custom app's permanent Admin API token (shpat_...).
"""

import hashlib
import json
import os
import logging
import re
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

from logging_setup import timed

_SHOPIFY_HTTP: Optional[httpx.Client] = None
_SHOPIFY_HTTP_LOCK = threading.Lock()


def _shopify_http() -> httpx.Client:
    """The kept-alive connection pool every Shopify request shares (kept open between calls by keep_warm)."""
    global _SHOPIFY_HTTP
    with _SHOPIFY_HTTP_LOCK:
        if _SHOPIFY_HTTP is None:
            _SHOPIFY_HTTP = httpx.Client(limits=httpx.Limits(max_connections=20, max_keepalive_connections=8, keepalive_expiry=120))
        return _SHOPIFY_HTTP


logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2025-10"
SEARCH_STOP_WORDS = {
    "the", "and", "for", "with", "what", "how", "are", "can", "you", "your", "our", "this", "that", "from",
    "about", "have", "has", "any", "some", "price", "prices", "cost", "much", "stock", "available", "availability",
    "sell", "carry", "buy", "want", "need", "looking", "there", "they", "them", "does", "did", "please", "tell",
    "show", "know", "would", "like", "get", "got", "product", "products", "item", "items", "still", "left", "many",
    "size", "sizes", "colour", "color", "colors", "colours", "which", "where", "when", "is", "in", "do", "of", "a", "an", "it",
    "just", "also", "then", "okay", "yeah", "yes", "hello", "hi", "thanks", "thank", "maybe", "really", "something", "anything",
    "one", "ones", "looking", "find", "check", "see", "send", "give", "could", "should", "will", "are", "were", "was", "been",
    "list", "down", "all", "pricing", "priced", "cheap", "cheapest", "options", "range", "models", "need", "know", "business", "okay", "also",
}


def normalize_store_domain(value: str) -> str:
    domain = (value or "").strip().lower()
    if not domain:
        return ""
    from urllib.parse import urlsplit
    parsed = urlsplit(domain if "://" in domain else "https://" + domain)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Use the store's canonical myshopify.com domain")
    if parsed.port is not None:
        raise ValueError("Shopify store domains must not include a port")
    domain = parsed.hostname
    if "." not in domain:
        domain = f"{domain}.myshopify.com"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", domain):
        raise ValueError("Use the store's canonical myshopify.com domain")
    return domain


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _order_number(name: Any) -> Optional[int]:
    """The number at the end of an order name: "#1001", "DFO001001" and "1001" are all 1001."""
    match = re.search(r"(\d+)\D*$", str(name or ""))
    return int(match.group(1)) if match else None


def _day(value: Any) -> str:
    """"23 Sep 2026" (with "today" / "yesterday" / "N days ago" when recent) from a Shopify time."""
    if not value:
        return ""
    try:
        from datetime import datetime, timezone
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc).date() - when.date()).days
        relative = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago" if 1 < days <= 14 else ""
        return f"{when.day} {when.strftime('%b %Y')}" + (f" ({relative})" if relative else "")
    except ValueError:
        return str(value)[:10]


CANCEL_REASONS = {"customer": "at the customer's request", "declined": "because the payment was declined", "fraud": "by the store",
                  "inventory": "because the item was out of stock", "staff": "by the store", "other": ""}
IN_TRANSIT = ("in_transit", "confirmed", "label_printed", "label_purchased", "attempted_delivery", "ready_for_pickup", "picked_up")


def order_status(order: Dict[str, Any]) -> str:
    """Where the order is, as a customer would want to hear it."""
    if order.get("cancelled"):
        reason = CANCEL_REASONS.get(str(order.get("cancel_reason") or "").lower(), "")
        refunded = " and refunded" if order.get("refunds") else ""
        return f"Cancelled {_day(order.get('cancelled_at'))} {reason}{refunded}".replace("  ", " ").strip()
    shipments = order.get("shipments") or []
    delivered = [shipment for shipment in shipments if shipment.get("delivered_at") or shipment.get("status") == "delivered"]
    if shipments and len(delivered) == len(shipments) and not order.get("unshipped"):
        when = delivered[-1].get("delivered_at") or (delivered[-1].get("latest") or {}).get("at")
        state = f"Delivered {_day(when)}" if when else "Delivered"
    elif any(shipment.get("status") == "out_for_delivery" for shipment in shipments):
        state = "Out for delivery"
    elif any(shipment.get("status") in IN_TRANSIT for shipment in shipments):
        moving = next(shipment for shipment in shipments if shipment.get("status") in IN_TRANSIT)
        state = {"attempted_delivery": "Delivery was attempted", "ready_for_pickup": "Ready for pickup"}.get(moving.get("status"), "On the way")
        state += f" (shipped {_day(moving.get('shipped_at'))})" if moving.get("shipped_at") else ""
    elif shipments:
        state = f"Shipped {_day(shipments[-1].get('shipped_at'))}".strip() + ("; no carrier updates yet" if not any(item.get("latest") and item["latest"].get("status") for item in shipments) else "")
    else:
        state = "Not shipped yet"
    if order.get("unshipped") and shipments:
        state += ", part of the order still to ship"
    financial = str(order.get("financial_status") or "")
    if "refund" in financial:
        state += f" ({financial})"
    elif financial and financial != "paid":
        state += f" (payment {financial})"
    return state


SPEC_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9 &/().,'-]{1,40}?)\s*[:\u2013-]\s+(.{1,160})$")


def _spec_lines(html: Any) -> List[str]:
    """The specification lines of a product description ("Material: Solid Brass", "Warranty: 15 Year Warranty"),
    from its HTML: one per line, list item or table row."""
    text = str(html or "")
    if not text:
        return []
    text = re.sub(r"(?i)<\s*(br|/p|/li|/tr|/h\d|/div)\s*/?>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>\s*<t[dh][^>]*>", ": ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    found: List[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" •-*\t")
        match = SPEC_LINE.match(line)
        if match and len(match.group(1).split()) <= 5:
            entry = f"{match.group(1).strip()}: {match.group(2).strip()}"
            if entry not in found:
                found.append(entry)
    return found[:20]


def _names_other_product(title: str, specs: List[str]) -> bool:
    """The description's "Product:" line names a different product than the title (a copied description)."""
    named = next((line.split(":", 1)[1] for line in specs if line.lower().startswith("product:")), "")
    named_words = {word for word in re.findall(r"[a-z]{3,}", named.lower()) if word not in {"with", "and", "the", "for", "series"}}
    return bool(named_words) and len(named_words - set(re.findall(r"[a-z]{3,}", (title or "").lower()))) >= 2


_DIMENSIONS = re.compile(r"(\d{2,4})\s*[x×*]\s*(\d{2,4})(?:\s*[x×*]\s*(\d{2,4}))?", re.I)


def _size_conflict(title: str, description: str) -> bool:
    """The title gives a size ("360x360x120mm") and the description gives sizes, none of them that one ("355*355*120mm")."""
    named = [tuple(part for part in match if part) for match in _DIMENSIONS.findall(title or "")]
    described = {tuple(part for part in match if part) for match in _DIMENSIONS.findall(description or "")}
    return bool(named and described) and not any(size in described for size in named)


_DIMENSION_WORDS = {"width": ("wide", "width", "widths"), "depth": ("deep", "depth"), "height": ("high", "height", "tall"), "length": ("long", "length")}


_ASKS_DIMENSION = re.compile(r"\bhow\s+(wide|deep|high|tall|long)\b|\b(width|depth|height|length)\b", re.I)


def _dimension_asked(question: str) -> str:
    """"width" for "how wide is it?" / "how far across is it?" (also depth, height, length), by meaning; "" when no
    single measurement is asked for."""
    from turn_meaning import DIMENSION
    kinds = DIMENSION.close(question)
    if kinds is not None:
        measures = [kind for kind in kinds if kind in _DIMENSION_WORDS]
        return measures[0] if len(measures) == 1 and kinds[0] in _DIMENSION_WORDS else ""  # two at once ("how wide and deep"): none
    return _keyword_dimension_asked(question)


def _keyword_dimension_asked(question: str) -> str:
    """Fallback for _dimension_asked when the local meaning model cannot load."""
    asked = {name for match in _ASKS_DIMENSION.finditer(question or "") for word in match.groups() if word
             for name, forms in _DIMENSION_WORDS.items() if word.lower() in forms or word.lower() == name}
    return next(iter(asked)) if len(asked) == 1 else ""


def _states_dimension(product: Dict[str, Any], dimension: str) -> bool:
    """Whether the product's data gives that dimension as a number ("Width: 750", "750 mm wide", "W 750"), or a full
    W x D x H size it could come from. "A wide range" is not a width."""
    text = " ".join([product.get("title") or "", product.get("description") or "", " ".join(product.get("specs") or []),
                     " ".join(" ".join(option.get("values") or []) for option in product.get("options") or [])]).lower()
    forms = "|".join([dimension, *_DIMENSION_WORDS[dimension]])
    return bool(re.search(rf"\b(?:{forms})\b\s*(?:[:=-]|of|is)?\s*\d", text)) \
        or bool(re.search(rf"\d+(?:\.\d+)?\s*(?:mm|cm|m)?\s*(?:{forms})\b", text)) \
        or bool(re.search(rf"\b{dimension[0]}\s*[:=]?\s*\d", text)) \
        or bool(re.search(r"\d{2,4}\s*[x×*]\s*\d{2,4}\s*[x×*]\s*\d{2,4}", text))


def _price_text(product: Dict[str, Any]) -> str:
    """"159.21 AUD", "699.00-899.00 AUD", or "price not listed" for a $0 price."""
    low, high, currency = product.get("min_price"), product.get("max_price"), product.get("currency") or ""
    if not _priced(high):
        return "price not listed"
    if not _priced(low) or float(low) == float(high):
        return f"{float(high):.2f} {currency}".strip()
    return f"{float(low):.2f}-{float(high):.2f} {currency}".strip()


def _priced(amount: Any) -> bool:
    """A real price (not missing or $0)."""
    try:
        return float(amount or 0) > 0
    except (TypeError, ValueError):
        return False


def _review_summary(badge_html: Any) -> str:
    """"rated 4.8 from 12 reviews" from a Judge.me review badge; "" when the product has no reviews."""
    rating = re.search(r"data-average-rating=['\"]([\d.]+)", str(badge_html or ""))
    count = re.search(r"data-number-of-reviews=['\"](\d+)", str(badge_html or ""))
    if not rating or not count or int(count.group(1)) == 0:
        return ""
    return f"rated {float(rating.group(1)):.1f} out of 5 from {count.group(1)} review{'s' if count.group(1) != '1' else ''}"


def phones_match(left: Any, right: Any) -> bool:
    """The same phone number written two ways. By the last 10 digits (+91 98765 43210 = 9876543210), or, when one is
    written in the local style ("0412 345 678") and the other with a country code ("+61 412 345 678"), by reading the
    local one in the other's country (libphonenumber; no country is assumed)."""
    a, b = _digits(left), _digits(right)
    if len(a) < 7 or len(b) < 7:
        return False
    if a[-10:] == b[-10:]:
        return True
    try:
        import phonenumbers
        for value in (str(left).strip(), str(right).strip()):
            if not value.startswith("+"):
                continue
            region = phonenumbers.region_code_for_number(phonenumbers.parse(value, None))
            first, second = phonenumbers.parse(str(left), region), phonenumbers.parse(str(right), region)
            if (first.country_code, first.national_number) == (second.country_code, second.national_number):
                return True
    except Exception:
        return False
    return False


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
        self._caller_orders: Dict[str, Any] = {}  # caller digits -> (looked up at, orders)
        self._caller_lookups: Dict[str, threading.Event] = {}  # lookups still running
        self._catalog_words: tuple = (frozenset(), 0.0)  # (words, built at)
        self._catalog_building = threading.Event()
        self._index: tuple = ([], 0.0)  # (products, built at): see catalog_index
        self._index_retry_at = 0.0
        self._index_building = threading.Event()
        self._index_vectors: Any = None  # meaning vectors, row for row with the index
        self._vector_cache: Dict[str, List[float]] = {}  # product text hash -> vector (only changed products are re-embedded)
        self._live_products: Dict[str, tuple] = {}  # product id -> (read at, live prices/stock), see _with_live_prices
        self._live_lock = threading.Lock()
        self._live_refreshing: set = set()  # product ids being re-read in the background
        self._live_removed: set = set()  # read live and gone from the store: not offered from the catalogue copy

    # ------------------------------------------------------------------ connection
    def configure(self, store_domain: str, access_token: str = "", api_version: str = DEFAULT_API_VERSION,
                  client_id: str = "", client_secret: str = "") -> None:
        domain = normalize_store_domain(store_domain)
        if not domain:
            raise ValueError("A Shopify store domain is required")
        if not re.fullmatch(r"\d{4}-\d{2}", api_version or DEFAULT_API_VERSION):
            raise ValueError("Use a Shopify API version in YYYY-MM format")
        with self._lock:
            self.store_domain = domain
            self.static_token = access_token or ""
            self.client_id = client_id or ""
            self.client_secret = client_secret or ""
            self.api_version = api_version or DEFAULT_API_VERSION
            self._token, self._token_expires_at, self._scopes = "", 0.0, None
            self._caller_orders.clear()
            self._catalog_words = (frozenset(), 0.0)  # another store: learn its catalogue again
            self._index = ([], 0.0)
            self._index_retry_at = 0.0

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
        # One kept-alive connection pool for the whole server: a new secure connection per request (urllib) added
        # ~0.1 s to every Shopify read, and a product question makes two or three of them in a row.
        try:
            response = _shopify_http().request("POST" if data is not None else "GET", url, content=data, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach {self.store_domain}: {exc}") from exc
        if response.status_code >= 400:
            hints = {
                400: "Shopify rejected the credentials. Check the Client ID and secret, and that the app is installed on this store.",
                401: "Shopify rejected the access token. Reinstall the app and paste the new token or secret.",
                403: "The app is missing a permission (scope) for this data. Add it in Shopify, release or save, then reinstall the app.",
                404: f"Store '{self.store_domain}' was not found. Use the exact myshopify.com domain from Shopify admin.",
            }
            raise RuntimeError(hints.get(response.status_code, f"Shopify request failed ({response.status_code})") + f" [details: {response.text[:160]}]")
        return json.loads(response.text or "{}")

    def keep_warm(self) -> None:
        """A tiny read (the shop's name) that keeps the kept-alive connection open between calls."""
        if self.configured:
            try:
                self.graphql("{ shop { name } }")
            except Exception as error:
                logger.debug("shopify keep-warm skipped: %s", error)

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
        operation = re.sub(r"[^a-zA-Z]+", " ", query.split("{", 2)[1] if "{" in query else query).split()[0:1]
        with timed(logger, f"Shopify {operation[0] if operation else 'query'}", warn_after_ms=2500):
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
            "id title handle status productType vendor tags onlineStoreUrl description(truncateAt: 1400) descriptionHtml "
            + ("totalInventory " if inventory else "")
            + "options { name values } collections(first: 3) { nodes { title } } "
            + 'reviews: metafield(namespace: "judgeme", key: "badge") { value } '
            + "priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } } "
            + f"variants(first: 25) {{ nodes {{ title price compareAtPrice sku availableForSale {inventory} }} }}"
        )

    @staticmethod
    def _normalize_product(node: Dict[str, Any]) -> Dict[str, Any]:
        variants = [{
            "title": item.get("title"),
            "price": item.get("price"),
            "compare_at": item.get("compareAtPrice"),  # the "was" price when it is on sale
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
            # "Key: Value" lines from the description (Material, Size, Warranty...): the facts callers ask about,
            # wherever they sit in a long description.
            "specs": _spec_lines(node.get("descriptionHtml")),
            "url": node.get("onlineStoreUrl"),
            "total_inventory": node.get("totalInventory"),
            "currency": (price_range.get("minVariantPrice") or {}).get("currencyCode"),
            "min_price": (price_range.get("minVariantPrice") or {}).get("amount"),
            "max_price": (price_range.get("maxVariantPrice") or {}).get("amount"),
            "variants": variants,
            # Choices the caller can make ("Color: Chrome, Black"), the store's collections, and real customer reviews.
            "options": [{"name": option.get("name"), "values": option.get("values") or []} for option in node.get("options") or []
                        if option.get("name") and option.get("name") != "Title"],
            "collections": [item.get("title") for item in (node.get("collections") or {}).get("nodes", []) if item.get("title")],
            "reviews": _review_summary((node.get("reviews") or {}).get("value")),
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
        """The caller's product words: filler removed, plurals made singular ("black sinks" -> black, sink)."""
        terms: List[str] = []
        for word in re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower()):
            word = word.strip("'-")
            if word.endswith("ies") and len(word) > 4:
                word = word[:-3] + "y"
            elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
                word = word[:-1]
            if (len(word) >= 3 or word.isdigit()) and word not in SEARCH_STOP_WORDS and word not in terms:
                terms.append(word)
        return terms[:6]

    # ---- the whole catalogue in memory, with bounded-age prices and background live refreshes
    INDEX_SECONDS = 600

    def catalog_index(self) -> List[Dict[str, Any]]:
        """Every active product's descriptive data (title, type, brand, tags, options, collections, description, specs,
        reviews), kept in memory and refreshed every 10 minutes in the background. Empty until the first build."""
        products, built = self._index
        if (self.configured and time.time() - built > self.INDEX_SECONDS
                and time.time() >= self._index_retry_at and not self._index_building.is_set()):
            self._index_building.set()
            threading.Thread(target=self._build_index, name="shopify-catalogue-index", daemon=True).start()
        return products

    def _build_index(self) -> None:
        try:
            started, found, after = time.time(), [], None
            stock = "totalInventory " if self.has_scope("read_inventory") else ""
            for _ in range(12):  # up to 3,000 products
                data = self.graphql(
                    "query($after: String) { products(first: 250, after: $after, query: \"status:active\") { pageInfo { hasNextPage endCursor } nodes { "
                    "id title handle status productType vendor tags onlineStoreUrl description(truncateAt: 1400) descriptionHtml " + stock +
                    "priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } } "
                    "options { name values } collections(first: 3) { nodes { title } } "
                    'reviews: metafield(namespace: "judgeme", key: "badge") { value } } } }', {"after": after})
                products = data.get("products") or {}
                found.extend(self._normalize_product(node) for node in products.get("nodes", []))
                page = products.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
            self._index_vectors = None  # until the vectors for this copy are ready, ranking is by words
            self._index = (found, time.time())
            self._index_retry_at = 0.0
            with self._live_lock:
                self._live_removed.clear()  # this copy no longer lists what was removed
            logger.info("shopify: catalogue index of %d products built in %.1fs", len(found), time.time() - started)
            if self.meaning_share > 0:  # vectors only when meaning takes part in the ranking
                self._embed_index(found)
        except Exception as exc:
            logger.warning("shopify: catalogue index not built: %s", exc)
            # Retrying must not make old catalogue prices appear fresh again.
            self._index_retry_at = time.time() + 120
        finally:
            self._index_building.clear()

    # How products are ranked for a question: this share by meaning (local embeddings of each product's title, type,
    # options, specs and description against the question), the rest by the caller's words found in the product.
    # Chosen by measurement (scripts/tune_store_search.py, 34 questions over the live catalogue, 2026-10-22): with the
    # local bge-small model, words alone ranked best (85% right first, 91% in the first three); every share of meaning
    # lowered it (10-50%: 79%), and meaning alone reached 59%. So 0 until a stronger embedding model measures better.
    # Understanding WHAT the caller wants (product / order / policy) is by meaning: see store_intent.
    MEANING_PERCENT_DEFAULT = 0
    SIMILARITY_FLOOR, SIMILARITY_CEILING = 0.55, 0.85  # bge-small: unrelated texts ~0.55, near-identical ~0.85

    @property
    def meaning_share(self) -> float:
        try:
            percent = float(os.getenv("STORE_SEARCH_MEANING_PERCENT", self.MEANING_PERCENT_DEFAULT))
        except ValueError:
            percent = self.MEANING_PERCENT_DEFAULT
        return min(max(percent, 0.0), 100.0) / 100.0

    def _word_score(self, product: Dict[str, Any], terms: List[str], nouns: List[str]) -> float:
        """The caller's words found in the product: title 3, type / brand / tags / options / collections 2,
        description and specs 1; plus 6 when the product IS what they named ("Above Counter Basin" for "basin")."""
        title = (product.get("title") or "").lower()
        about = " ".join([product.get("product_type") or "", product.get("vendor") or "", " ".join(product.get("tags") or []),
                          " ".join(" ".join(option.get("values") or []) for option in product.get("options") or []),
                          " ".join(product.get("collections") or [])]).lower()
        body = " ".join([product.get("description") or "", " ".join(product.get("specs") or [])]).lower()
        score = sum(3 if term in title else 2 if term in about else 1 if term in body else 0 for term in terms)
        kind = self._product_is(title)
        if kind and any(kind == noun or kind.startswith(noun) or noun.startswith(kind) for noun in nouns):  # "bath" ~ "bathtub"
            score += 6
        return float(score)

    def _local_search(self, text: str, terms: List[str], limit: int, meaning_share: Optional[float] = None, kind_only: bool = True) -> List[Dict[str, Any]]:
        """Rank the in-memory catalogue: final = meaning_share x meaning + (1 - meaning_share) x words, each 0..1.
        When the caller names a kind of thing the store sells ("sink"), only products of that kind are considered.
        Products found by meaning alone (none of the caller's words in them) are marked, so the AI offers them as the
        closest options instead of stating them as what was asked for."""
        share = self.meaning_share if meaning_share is None else meaning_share
        products = self._index[0]
        catalog = self.catalog_words()
        nouns = [term for term in terms if term in catalog]
        # When the caller names a kind of thing ("sink"), only that kind is considered (kind_only), so "black sink" never
        # returns black basins; with kind_only off every product competes and the named kind only gets a word bonus.
        candidates = [index for index, product in enumerate(products)
                      if not (kind_only and nouns) or any(noun in (product.get("title") or "").lower()
                                                           or noun in " ".join([product.get("product_type") or "", " ".join(product.get("tags") or [])]).lower() for noun in nouns)]
        if not candidates:
            return []
        words = {index: self._word_score(products[index], terms, nouns) for index in candidates}
        best_words = max(words.values()) or 1.0
        meaning = self._meaning_scores(text) if share > 0 else None
        if meaning is not None:
            # The small model rates almost everything 0.6-0.8 alike: spread the scores over this catalogue (the closest
            # product 1, the furthest 0) so meaning is a real signal next to the words.
            low, high = float(meaning.min()), float(meaning.max())
            meaning = (meaning - low) / ((high - low) or 1.0)
        scored = []
        for index in candidates:
            word_part = words[index] / best_words
            meaning_part = float(meaning[index]) if meaning is not None else 0.0
            final = share * meaning_part + (1 - share) * word_part if meaning is not None else word_part
            if final > 0:
                scored.append((final, index))
        scored.sort(key=lambda item: item[0], reverse=True)  # stable: the store's own order breaks ties
        found = []
        for final, index in scored[:limit]:
            product = dict(products[index])
            product["_score"] = round(final, 3)
            if not words[index]:
                product["_match"] = "meaning"
            found.append(product)
        return found

    def _meaning_scores(self, text: str):
        """How close in meaning the question is to every product in the catalogue (None if not available yet)."""
        vectors = self._index_vectors
        if vectors is None or len(vectors) != len(self._index[0]):
            return None
        from brain_service import local_embeddings
        query = local_embeddings([text], query=True)
        if not query:
            return None
        import numpy as np
        return vectors @ np.array(query[0])

    @staticmethod
    def _product_text(product: Dict[str, Any]) -> str:
        """What a product's meaning vector is made from."""
        options = "; ".join(f"{option['name']}: {', '.join(option['values'][:6])}" for option in product.get("options") or [] if option.get("values") != ["Default Title"])
        parts = [product.get("title") or "", product.get("product_type") or "", options, "; ".join((product.get("specs") or [])[:8]), (product.get("description") or "")[:400]]
        return ". ".join(part for part in parts if part)

    def _embed_index(self, products: List[Dict[str, Any]]) -> None:
        """Meaning vectors for the catalogue, reusing the ones for products whose text has not changed."""
        try:
            import numpy as np
            from brain_service import local_embeddings
            texts = [self._product_text(product) for product in products]
            keys = [hashlib.sha1(text.encode("utf-8")).hexdigest() for text in texts]
            missing = [index for index, key in enumerate(keys) if key not in self._vector_cache]
            started = time.time()
            for start in range(0, len(missing), 64):
                batch = missing[start:start + 64]
                vectors = local_embeddings([texts[index] for index in batch])
                if not vectors:
                    return  # the local model is unavailable: ranking stays by words
                for index, vector in zip(batch, vectors):
                    self._vector_cache[keys[index]] = vector
            self._index_vectors = np.array([self._vector_cache[key] for key in keys])
            logger.info("shopify: meaning vectors for %d products (%d new) in %.1fs", len(products), len(missing), time.time() - started)
        except Exception as exc:
            logger.warning("shopify: product meaning vectors not built: %s", exc)

    def _product_is(self, title: str) -> str:
        """The thing a product is, from its title: the last word in it that names something the store sells."""
        # "Wall Mixer with Diverter", "Spout for Bath & Basin": what follows "with" / "for" describes it, it is not the thing.
        name = re.split(r"\s+(?:with|for|and|incl\.?|including|suits?|to suit)\s+|\s+-\s+", title.lower(), maxsplit=1)[0]
        nouns = self.catalog_nouns(re.sub(r"\d+\s*[x×*]\s*\d+[^ ]*", " ", name))  # sizes like 360x360x120mm are not things
        return nouns[-1] if nouns else ""

    @staticmethod
    def _seconds_setting(name: str, default: float) -> float:
        try:
            return max(0.0, float(os.getenv(name, str(default))))
        except ValueError:
            return default

    def _read_live(self, product_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Current prices, sale prices, stock and options for these products, in one request; remembered with the time."""
        stock = "inventoryQuantity" if self.has_scope("read_inventory") else ""
        data = self.graphql(
            "query($ids: [ID!]!) { nodes(ids: $ids) { ... on Product { id " + ("totalInventory " if stock else "")
            + "priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } } "
            + f"variants(first: 25) {{ nodes {{ title price compareAtPrice sku availableForSale {stock} }} }} }} }} }}",
            {"ids": product_ids})
        fetched = {node["id"]: self._normalize_product(node) for node in data.get("nodes") or [] if node}
        read_at = time.time()
        with self._live_lock:
            for product_id in product_ids:
                if product_id in fetched:
                    self._live_products[product_id] = (read_at, fetched[product_id])
                    self._live_removed.discard(product_id)
                else:
                    self._live_products.pop(product_id, None)  # removed from the store: never offered from memory
                    self._live_removed.add(product_id)
        return fetched

    def _refresh_live_later(self, product_ids: List[str]) -> None:
        """Re-read these products in the background (once at a time each); the caller is already answered."""
        with self._live_lock:
            ids = [product_id for product_id in product_ids if product_id not in self._live_refreshing]
            self._live_refreshing.update(ids)
        if not ids:
            return

        def refresh() -> None:
            try:
                self._read_live(ids)
            except Exception as error:  # the remembered data stays until it is too old to use (SHOPIFY_LIVE_MAX_SECONDS)
                logger.warning("shopify: background price refresh failed: %s", error)
            finally:
                with self._live_lock:
                    self._live_refreshing.difference_update(ids)

        threading.Thread(target=refresh, name="shopify-live-refresh", daemon=True).start()

    def _with_live_prices(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The same products with their current prices, sale prices, stock and options.
        Every product turn used to read these live (Shopify "nodes", 0.4-0.9 s), six times in one production turn for
        the same products. Now, per product:
        - read within SHOPIFY_LIVE_SECONDS (60): used as is;
        - older, up to SHOPIFY_LIVE_MAX_SECONDS (600): used at once and re-read in the background for the next turn;
        - never read (also after a restart, when memory starts empty): the catalogue copy's price range is used at once
          and the product is read live in the background (the full variants come next turn);
        - older than SHOPIFY_LIVE_MAX_SECONDS, or with no price known at all: read live before answering."""
        if not products:
            return []
        fresh_for = self._seconds_setting("SHOPIFY_LIVE_SECONDS", 60)
        usable_for = max(fresh_for, self._seconds_setting("SHOPIFY_LIVE_MAX_SECONDS", 600))
        now_time = time.time()
        live: Dict[str, Dict[str, Any]] = {}
        stale: List[str] = []
        with self._live_lock:
            for product in products:
                cached = self._live_products.get(product["id"])
                if not cached:
                    continue
                age = now_time - cached[0]
                if age < usable_for:
                    live[product["id"]] = cached[1]
                    if age >= fresh_for:
                        stale.append(product["id"])
        with self._live_lock:
            removed = set(self._live_removed)
            known = set(self._live_products)
        index_usable = now_time - self._index[1] < usable_for
        indexed = [product for product in products if index_usable and product["id"] not in live and _priced(product.get("max_price"))
                   and product["id"] not in known and product["id"] not in removed]
        for product in indexed:
            live[product["id"]] = product
        stale.extend(product["id"] for product in indexed)
        missing = [product["id"] for product in products if product["id"] not in live and product["id"] not in removed]
        if missing:
            live.update(self._read_live(missing))
        if stale:
            self._refresh_live_later(stale)
        merged = []
        for product in products:
            now = live.get(product["id"])
            if now:  # a product removed since the index was built is left out
                merged.append({**product, **{key: now[key] for key in ("total_inventory", "currency", "min_price", "max_price", "variants")}})
        return merged

    def search_products(self, text: str, limit: int = 5) -> List[Dict[str, Any]]:
        terms = self._search_terms(text)
        if terms and self.catalog_index():
            found = self._local_search(text, terms, limit)
            if found:
                live = {product["id"]: product for product in self._with_live_prices(found)}
                return [{**live[product["id"]], **{key: product[key] for key in ("_score", "_match") if key in product}}
                        for product in found if product["id"] in live]
        return self._shopify_search(text, limit)

    def _shopify_search(self, text: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Find what the caller means, most specific first: all their product words together ("matte black basin");
        if nothing has all of them, just the words that name things this store sells ("sink"); then any title with
        any of the words. Results are ranked by how many of the caller's words they match."""
        terms = self._search_terms(text)
        if not terms:
            return []
        catalog = self.catalog_words()
        nouns = [term for term in terms if term in catalog] if catalog else []
        attempts = [" ".join(terms)]
        if nouns and nouns != terms:
            attempts.append(" ".join(nouns))
        attempts += [noun for noun in nouns if len(nouns) > 1]
        attempts.append("(" + " OR ".join(f"title:*{term}*" for term in terms if not term.isdigit()) + ")")
        found: List[Dict[str, Any]] = []
        for attempt in attempts:
            if attempt in ("()", ""):
                continue
            found = self.products(f"{attempt} status:active", limit=10)
            if found:
                break

        def score(product: Dict[str, Any]) -> int:
            haystack = " ".join([str(product.get("title") or ""), str(product.get("product_type") or ""), " ".join(product.get("tags") or []),
                                 " ".join(" ".join(option.get("values") or []) for option in product.get("options") or [])]).lower()
            return sum(1 for term in terms if term in haystack)

        found.sort(key=score, reverse=True)  # stable: Shopify's relevance order breaks ties
        return found[:limit]

    def product_context(self, query: str, include_stock: bool = True, question: str = "") -> str:
        """What the store has for the caller's question, at the level of detail the question needs (decided by meaning,
        turn_meaning.PRODUCT_DETAIL): a general question ("what basins do you have?") gets an overview of several
        products (price, stock, choices, key measurements); a question about one product's details ("what's the warranty
        on that one?") gets that product in full (every spec, the description, every option) and the rest as overview.
        `question` is the caller's own words (`query` may also carry the product named earlier in the call)."""
        found = self.search_products(query, limit=10)
        if not found:
            return "Live Shopify catalog: no product matched what the caller asked for."
        detailed = self._wants_detail(question or query)
        products, more = found[:5], found[5:]
        blocks = [self._product_block(product, include_stock, full=detailed and rank == 0) for rank, product in enumerate(products)]
        # More matches in one line each, and the price range, so "what do you have and what do they cost?" gets a real answer.
        if more:
            blocks.append("Also in the store: " + "; ".join(f"{product['title']} ({_price_text(product)})" for product in more))
        prices = [float(product[key]) for product in found for key in ("min_price", "max_price") if _priced(product.get(key))]
        if len(found) > 1 and prices:
            blocks.append(f"Prices across these {len(found)} matches: from {min(prices):.2f} to {max(prices):.2f} {found[0].get('currency') or ''}".rstrip())
        # The caller asked for one dimension ("how wide is it?") that the closest matches do not state: say so outright.
        asked = _dimension_asked(query)
        if asked and not _states_dimension(products[0], asked):  # the product the caller named
            blocks.insert(0, f"The caller asked for the {asked}. No {asked} is written for these products: tell them plainly that the {asked} "
                             f"isn't listed (you may give the sizes that are written) and offer to have the team confirm the {asked}.")
        heading = "current prices and stock" if include_stock else "current prices; do not quote stock counts"
        if not detailed:
            heading += "; an overview: if the caller then asks about one of these in detail, its full details are looked up then"
        return ("These prices are live from the store right now: when the caller asks what something costs, tell them (this replaces any "
                "instruction to send callers to the website for prices). For a broad question, give two or three examples with prices and the "
                "price range, then ask one short question to narrow it down (such as the style, size, finish or budget they have in mind) "
                f"instead of sending them to the website.\nLive Shopify catalog ({heading}; describe products only with these details, and summarise rather than read "
                "descriptions out. If the caller asks for a size, measurement or detail that is not written here, say it is not listed "
                "and offer to have the team confirm; never estimate it. A size written without saying which dimension it is (\"Size: 1500\") "
                "is said as written, \"the 1500 mm model\": never call it the width, depth or height, and when they asked for a width, depth or "
                "height that is not written, say so in words, for example \"I don't have the width listed\"):\n" + "\n".join(blocks))

    def _wants_detail(self, question: str) -> bool:
        """Whether the caller wants one product's details (by meaning), rather than an overview of what there is."""
        try:
            from turn_meaning import PRODUCT_DETAIL
            from voice_turns import kind_of_turn_text
            # Product names made generic first ("tell me more about the matte black above counter basin" is about
            # wanting detail, whatever the product is called).
            return PRODUCT_DETAIL.best(kind_of_turn_text(question)) == "detail"
        except Exception:
            return True  # unsure: give the detail, never less than the question needs

    def _product_block(self, product: Dict[str, Any], include_stock: bool, full: bool) -> str:
        """One product for the AI. Overview: price, sale, stock, brand, type, choices, up to 6 options with their price
        and stock, and the specifications that carry a measurement (size, capacity, warranty years). Full: also every
        specification, every option and the whole description."""
        currency = product.get("currency") or ""
        low, high = product.get("min_price"), product.get("max_price")
        # A $0 price in the store means "not priced online" (quote items, placeholders): never say "$0".
        if not _priced(high):
            price = "not listed online (offer a quote from the team)"
        elif not _priced(low) or low == high:
            price = f"{high if not _priced(low) else low} {currency}"
        else:
            price = f"{low}-{high} {currency}"
        facts = [f"price {price}"]
        sale = [variant for variant in product["variants"] if variant.get("compare_at") and float(variant["compare_at"] or 0) > float(variant.get("price") or 0)]
        if sale:
            facts.append(f"on sale (was {sale[0]['compare_at']}, now {sale[0]['price']} {currency})")
        stock = product.get("total_inventory") if include_stock else None
        if stock is not None:
            facts.append(f"{stock} in stock" if stock > 0 else "out of stock")
        if product.get("vendor"):
            facts.append(f"brand {product['vendor']}")
        if product.get("product_type") and product["product_type"].strip().lower() not in ("default", "unclassified"):
            facts.append(f"type {product['product_type']}")
        if full and product.get("collections"):
            facts.append("in " + ", ".join(product["collections"][:3]))
        if product.get("reviews"):
            facts.append(product["reviews"])
        lines = [f"- {product['title']}: " + "; ".join(facts)]
        # Shopify's "Default Title" placeholder is not a real choice.
        choices = [option for option in product.get("options") or [] if [value for value in option["values"] if value != "Default Title"]]
        if choices:
            lines.append("  Choices: " + "; ".join(f"{option['name']}: {', '.join(option['values'][:10])}" for option in choices))
        options = [v for v in product["variants"] if v.get("title") and v["title"] != "Default Title"]
        if options:
            parts = []
            for variant in options[:10 if full else 6]:
                qty = variant.get("inventory_quantity")
                if not include_stock:
                    state = "unavailable" if variant.get("available") is False else "available"
                elif isinstance(qty, int) and qty > 0:
                    state = f"{qty} left"
                else:
                    state = "sold out" if qty == 0 or variant.get("available") is False else "available"
                parts.append(f"{variant['title']} {variant['price'] if _priced(variant.get('price')) else 'price not listed'} ({state})")
            more_options = len(options) - len(parts)
            lines.append("  Each option: " + ", ".join(parts) + (f" (+{more_options} more)" if more_options > 0 else ""))
        if _names_other_product(product.get("title") or "", product.get("specs") or []):
            # Its description is for another product: give name, price and options only.
            lines.append("  (Its online description is for a different product: do not describe its features, material or warranty; "
                         "offer to have the team confirm the details.)")
            return "\n".join(lines)
        specs = product.get("specs") or []
        if full:
            if specs:
                lines.append("  Specifications: " + "; ".join(specs))
            description = re.sub(r"\s+", " ", product.get("description") or "").strip()
            if description:
                lines.append("  About it: " + description[:1400] + ("..." if len(description) > 1400 else ""))
        else:
            measured = [spec for spec in specs if re.search(r"\d", spec)][:4]  # sizes, capacity, warranty years
            if measured:
                lines.append("  Key specs: " + "; ".join(measured))
            if not specs and not measured and product.get("description"):
                lines.append("  About it: " + re.sub(r"\s+", " ", product["description"]).strip()[:160] + "...")
        return "\n".join(lines)

    # ------------------------------------------------------------------ orders & customers
    ORDER_FIELDS = (
        "name createdAt cancelledAt cancelReason returnStatus displayFinancialStatus displayFulfillmentStatus email phone "
        "totalPriceSet { shopMoney { amount currencyCode } } "
        "customer { phone email } shippingAddress { phone } billingAddress { phone } "
        "refunds { createdAt totalRefundedSet { shopMoney { amount currencyCode } } } "
        "lineItems(first: 10) { nodes { title quantity unfulfilledQuantity } } "
        "fulfillments(first: 5) { status displayStatus createdAt inTransitAt deliveredAt estimatedDeliveryAt trackingInfo { company number url } "
        "events(first: 1, sortKey: HAPPENED_AT, reverse: true) { nodes { status happenedAt message city } } }"
    )

    @staticmethod
    def _normalize_order(node: Dict[str, Any]) -> Dict[str, Any]:
        total = ((node.get("totalPriceSet") or {}).get("shopMoney") or {})
        customer = node.get("customer") or {}
        line_items = (node.get("lineItems") or {}).get("nodes", [])
        shipments = []
        for fulfillment in node.get("fulfillments") or []:
            latest = ((fulfillment.get("events") or {}).get("nodes") or [{}])[0]
            shipments.append({"status": str(fulfillment.get("displayStatus") or fulfillment.get("status") or "").lower(),
                              "shipped_at": fulfillment.get("createdAt"), "in_transit_at": fulfillment.get("inTransitAt"),
                              "delivered_at": fulfillment.get("deliveredAt"), "estimated_at": fulfillment.get("estimatedDeliveryAt"),
                              "latest": {"status": str(latest.get("status") or "").lower(), "at": latest.get("happenedAt"),
                                         "message": latest.get("message"), "city": latest.get("city")} if latest else None,
                              "tracking": [{"company": t.get("company"), "number": t.get("number")} for t in fulfillment.get("trackingInfo") or []]})
        order = {
            "name": node.get("name"),
            "created_at": node.get("createdAt"),
            "cancelled": bool(node.get("cancelledAt")),
            "financial_status": str(node.get("displayFinancialStatus") or "").replace("_", " ").lower(),
            "fulfillment_status": str(node.get("displayFulfillmentStatus") or "").replace("_", " ").lower(),
            "total": f"{float(total.get('amount') or 0):.2f} {total.get('currencyCode')}" if total else None,
            "items": [f"{item.get('quantity')} x {item.get('title')}" for item in line_items],
            # Items not sent yet, when only part of the order has shipped.
            "unshipped": [f"{item.get('unfulfilledQuantity')} x {item.get('title')}" for item in line_items if item.get("unfulfilledQuantity")]
                         if any(not item.get("unfulfilledQuantity") for item in line_items) else [],
            "cancelled_at": node.get("cancelledAt"), "cancel_reason": node.get("cancelReason"),
            "return_status": str(node.get("returnStatus") or "").lower().replace("_", " "),
            "refunds": [{"at": refund.get("createdAt"), "amount": ((refund.get("totalRefundedSet") or {}).get("shopMoney") or {}).get("amount"),
                         "currency": ((refund.get("totalRefundedSet") or {}).get("shopMoney") or {}).get("currencyCode")} for refund in node.get("refunds") or []
                        if float(((refund.get("totalRefundedSet") or {}).get("shopMoney") or {}).get("amount") or 0) > 0],  # money actually returned
            "shipments": shipments,
            "tracking": [
                {"company": t.get("company"), "number": t.get("number"), "url": t.get("url"), "status": f.get("status")}
                for f in node.get("fulfillments") or [] for t in f.get("trackingInfo") or []
            ],
            # Used only for verification; never returned to callers.
            "_emails": {e.lower() for e in (node.get("email"), customer.get("email")) if e},
            # Stores often keep the number only on the billing or shipping address, not on the order or customer.
            "_phones": [p for p in (node.get("phone"), customer.get("phone"), (node.get("shippingAddress") or {}).get("phone"),
                                    (node.get("billingAddress") or {}).get("phone")) if p],
        }
        order["status"] = order_status(order)
        return order

    def orders(self, query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
        """Recent orders, or the order a caller named. Stores choose their own order-name format ("#1001", "DFO001001"),
        so the name is tried as said, then with "#", then matched by its number among recent orders ("1001" finds
        "DFO001001")."""
        limit = min(max(limit, 1), 100)
        query = re.sub(r"[\s-]", "", query or "")
        if not query:
            return self._orders(None, limit)
        for search in dict.fromkeys([f"name:{query}", f"name:#{query.lstrip('#')}"]):
            found = self._orders(search, limit)
            if found:
                return found
        number = _order_number(query)
        if number is None:
            return []
        return [order for order in self._orders(None, 100) if _order_number(order.get("name")) == number][:limit]

    def _orders(self, search: Optional[str], limit: int) -> List[Dict[str, Any]]:
        data = self.graphql(
            f"query($n: Int!, $q: String) {{ orders(first: $n, query: $q, sortKey: CREATED_AT, reverse: true) {{ nodes {{ {self.ORDER_FIELDS} }} }} }}",
            {"n": limit, "q": search},
        )
        orders = [self._normalize_order(node) for node in (data.get("orders") or {}).get("nodes", [])]
        for order in orders:  # sets are not JSON serializable for API callers
            order["_emails"] = sorted(order["_emails"])
        return orders

    def order_page(self, after: str = "", limit: int = 50) -> Dict[str, Any]:
        """A page of orders for the Store data page, newest first, with the cursor for the next page."""
        data = self.graphql(
            f"query($n: Int!, $after: String) {{ orders(first: $n, after: $after, sortKey: CREATED_AT, reverse: true) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {self.ORDER_FIELDS} }} }} }}",
            {"n": min(max(limit, 1), 100), "after": after or None},
        )
        orders = data.get("orders") or {}
        rows = [self._normalize_order(node) for node in orders.get("nodes", [])]
        page = orders.get("pageInfo") or {}
        return {"orders": rows, "next": page.get("endCursor") if page.get("hasNextPage") else None}

    CALLER_ORDERS_SECONDS = 120  # a caller's orders, looked up when the call starts, stay ready for the whole call

    def orders_for_phone(self, phone: str, limit: int = 5, _waiting: bool = True) -> List[Dict[str, Any]]:
        """The caller's orders, newest first: recent orders whose order, customer, billing or shipping phone is the
        caller's number, plus orders on a customer profile with that number. Cached per caller (see prefetch)."""
        digits = _digits(phone)
        if len(digits) < 7:
            return []
        running = self._caller_lookups.get(digits)
        if _waiting and running is not None:
            running.wait(timeout=4)  # the lookup started when the call began is nearly done: use it
        cached = self._caller_orders.get(digits)
        if cached and time.time() - cached[0] < self.CALLER_ORDERS_SECONDS:
            return cached[1][:limit]
        started = time.time()
        found = [order for order in self._orders(None, 100) if any(phones_match(number, phone) for number in order["_phones"])]
        if len(found) < limit and self.has_scope("read_customers"):
            try:
                data = self.graphql(
                    f"query($q: String) {{ customers(first: 3, query: $q) {{ nodes {{ phone orders(first: {limit}, sortKey: CREATED_AT, reverse: true) {{ nodes {{ {self.ORDER_FIELDS} }} }} }} }} }}",
                    {"q": f"phone:*{digits[-9:]}"},
                )
            except Exception as exc:  # the orders already found by phone still stand
                logger.warning("shopify: customer search for caller ...%s failed: %s", digits[-3:], exc)
                data = {}
            names = {order["name"] for order in found}
            for customer in (data.get("customers") or {}).get("nodes", []):
                if phones_match(customer.get("phone"), phone):
                    for node in (customer.get("orders") or {}).get("nodes", []):
                        order = self._normalize_order(node)
                        if order["name"] not in names:
                            order["_emails"] = sorted(order["_emails"]); found.append(order); names.add(order["name"])
        found.sort(key=lambda order: order.get("created_at") or "", reverse=True)
        self._caller_orders[digits] = (time.time(), found)
        logger.info("shopify: caller ...%s has %d order(s) (%.0fms)", digits[-3:], len(found), (time.time() - started) * 1000)
        return found[:limit]

    def prefetch_orders_for_phone(self, phone: str) -> None:
        """Start looking up a caller's orders in the background (at the start of a call), so a question about their
        order is answered without waiting for Shopify."""
        digits = _digits(phone)
        if not self.configured or not self.has_scope("read_orders") or len(digits) < 7:
            return
        cached = self._caller_orders.get(digits)
        if digits in self._caller_lookups or (cached and time.time() - cached[0] < self.CALLER_ORDERS_SECONDS):
            return
        done = self._caller_lookups[digits] = threading.Event()  # one lookup at a time per caller

        def run() -> None:
            try:
                self.orders_for_phone(phone, _waiting=False)
            except Exception as exc:
                logger.warning("shopify: caller order lookup failed: %s", exc)
            finally:
                self._caller_lookups.pop(digits, None)
                done.set()
        threading.Thread(target=run, name="shopify-caller-orders", daemon=True).start()

    def cached_orders_for_phone(self, phone: str) -> Optional[List[Dict[str, Any]]]:
        """The caller's orders if already looked up (never waits on Shopify; starts the lookup if not), else None."""
        digits = _digits(phone)
        cached = self._caller_orders.get(digits)
        if cached and time.time() - cached[0] < self.CALLER_ORDERS_SECONDS:
            return cached[1]
        self.prefetch_orders_for_phone(phone)
        return None

    CATALOG_WORDS_SECONDS = 3600
    _NOT_PRODUCT_WORDS = {"with", "series", "only", "size", "each", "pack", "free", "standing", "style", "type", "item", "single", "double",
                          "left", "right", "hand", "inch", "mm", "cm", "set", "kit", "new", "black", "white", "gold", "grey", "gray", "matt",
                          "gloss", "brushed", "chrome", "nickel", "brass", "and", "the", "for", "unclassified"}

    def catalog_words(self) -> frozenset:
        """Words that name things this store sells (from its own product titles and types: "mixer", "basin",
        "rangehood"), so a caller asking about any of them gets a live product lookup, however they phrase it.
        Built in the background and refreshed hourly; empty until the first build finishes."""
        words, built = self._catalog_words
        if self.configured and time.time() - built > self.CATALOG_WORDS_SECONDS and not self._catalog_building.is_set():
            self._catalog_building.set()
            threading.Thread(target=self._build_catalog_words, name="shopify-catalog-words", daemon=True).start()
        return words

    def _build_catalog_words(self) -> None:
        try:
            found: set = set()
            after = None
            for _ in range(8):  # up to 2,000 products
                data = self.graphql("query($after: String) { products(first: 250, after: $after) { pageInfo { hasNextPage endCursor } nodes { title productType } } }", {"after": after})
                products = data.get("products") or {}
                for node in products.get("nodes", []):
                    for word in re.findall(r"[a-z]{4,}", f"{node.get('title') or ''} {node.get('productType') or ''}".lower()):
                        if word not in self._NOT_PRODUCT_WORDS and word not in SEARCH_STOP_WORDS:
                            found.add(word[:-1] if word.endswith("s") and not word.endswith("ss") else word)
                page = products.get("pageInfo") or {}
                if not page.get("hasNextPage"):
                    break
                after = page.get("endCursor")
            self._catalog_words = (frozenset(found), time.time())
            logger.info("shopify: %d catalogue words for product questions", len(found))
        except Exception as exc:
            logger.warning("shopify: catalogue words not built: %s", exc)
            self._catalog_words = (self._catalog_words[0], time.time() - self.CATALOG_WORDS_SECONDS + 300)  # retry in 5 minutes
        finally:
            self._catalog_building.clear()

    def product_kinds(self) -> frozenset:
        """The kinds of things this store sells, from its products' types ("Bathroom Basins" -> basin, "Stainless Steel
        Sinks" -> sink): the last word of each type, singular. Empty until the catalogue copy is loaded."""
        products, built = self._index
        cached = getattr(self, "_kinds", (None, frozenset()))
        if cached[0] != built:
            kinds = set()
            for product in products:
                words = re.findall(r"[a-z]{4,}", (product.get("product_type") or "").lower())
                if words and words[-1] not in ("default", "unclassified", "accessories"):
                    kind = words[-1]
                    kinds.add(kind[:-1] if kind.endswith("s") and not kind.endswith("ss") else kind)
            self._kinds = (built, frozenset(kinds))
        return self._kinds[1]

    def catalog_nouns(self, text: str) -> List[str]:
        """The words in a text that name things this store sells, in the order said (singular)."""
        words = self.catalog_words()
        found: List[str] = []
        for word in re.findall(r"[a-z]{4,}", (text or "").lower()):
            word = word[:-1] if word.endswith("s") and not word.endswith("ss") else word
            if word in words and word not in found:
                found.append(word)
        return found

    def mentions_catalog(self, text: str) -> bool:
        """Whether the caller names something the store sells (plural or singular)."""
        words = self.catalog_words()
        return bool(words) and any((word[:-1] if word.endswith("s") and not word.endswith("ss") else word) in words
                                   for word in re.findall(r"[a-z]{4,}", (text or "").lower()))

    def orders_for_email(self, email: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Orders placed with this email (the caller said it, so it verifies them for these orders)."""
        email = (email or "").strip().lower()
        if "@" not in email:
            return []
        return [order for order in self._orders(f"email:{email}", limit) if email in order["_emails"]]

    def customers(self, limit: int = 25) -> List[Dict[str, Any]]:
        data = self.graphql("query($n: Int!) { customers(first: $n) { nodes { id displayName numberOfOrders } } }", {"n": min(max(limit, 1), 100)})
        return (data.get("customers") or {}).get("nodes", [])

    @staticmethod
    def describe_order(order: Dict[str, Any]) -> str:
        """Everything a customer may ask about their order, in plain words: what it is, where it is now, and any
        cancellation, refund or return (never addresses or payment details)."""
        total = f"{order['total']}" if order.get("total") else "unknown total"
        lines = [f"Order {order['name']}, placed {_day(order.get('created_at'))}: {', '.join(order['items']) or 'no items listed'}; total {total}.",
                 f"Status now: {order.get('status') or order_status(order)}."]
        if not order.get("shipments") and order.get("tracking"):  # an order without shipment details still has its tracking
            lines.append("Tracking: " + ", ".join(f"{t.get('company') or 'carrier'} {t.get('number') or ''}".strip() for t in order["tracking"]) + ".")
        for shipment in order.get("shipments") or []:
            parts = [f"shipped {_day(shipment['shipped_at'])}" if shipment.get("shipped_at") else ""]
            if shipment.get("tracking"):
                parts.append("tracking " + ", ".join(f"{t['company'] or 'carrier'} {t['number'] or ''}".strip() for t in shipment["tracking"]))
            latest = shipment.get("latest") or {}
            if latest.get("status") and not shipment.get("delivered_at"):
                parts.append(f"latest carrier update: {latest['status'].replace('_', ' ')} {_day(latest.get('at'))}" + (f" in {latest['city']}" if latest.get("city") else ""))
            if shipment.get("estimated_at") and not shipment.get("delivered_at"):
                parts.append(f"estimated delivery {_day(shipment['estimated_at'])}")
            if any(parts):
                lines.append("Shipment: " + "; ".join(part for part in parts if part) + ".")
        if order.get("unshipped"):
            lines.append(f"Still to ship: {', '.join(order['unshipped'])}.")
        for refund in order.get("refunds") or []:
            lines.append(f"Refund of {float(refund.get('amount') or 0):.2f} {refund.get('currency') or ''} issued {_day(refund.get('at'))}.".replace("  ", " "))
        if order.get("return_status") and order["return_status"] != "no return":
            lines.append(f"Return: {order['return_status']}.")
        return " ".join(lines)

    # ------------------------------------------------------------------ store policies
    # ------------------------------------------------------------------ store data page (dashboard only, not the AI)
    def overview(self) -> Dict[str, Any]:
        """Store totals for the Store data page. Counts the store does not allow (missing scope) come back as None."""
        data = self.graphql("{ shop { name currencyCode myshopifyDomain primaryDomain { url } } productsCount { count } }")
        result = {**(data.get("shop") or {}), "products": (data.get("productsCount") or {}).get("count"), "orders": None, "customers": None}
        for key, query in (("orders", "{ ordersCount { count } }"), ("customers", "{ customersCount { count } }")):
            try:
                result[key] = (self.graphql(query).get(f"{key}Count") or {}).get("count")
            except Exception as exc:
                logger.info("shopify: %s count unavailable: %s", key, exc)
        return result

    def catalog(self, query: str = "", after: str = "", limit: int = 24) -> Dict[str, Any]:
        """A page of products with their main image, newest updated first (or by relevance when searching)."""
        sort = "RELEVANCE" if query else "UPDATED_AT"
        data = self.graphql(
            "query($n: Int!, $q: String, $after: String) { products(first: $n, query: $q, after: $after, sortKey: " + sort + (", reverse: true" if not query else "") + ") { "
            "pageInfo { hasNextPage endCursor } nodes { id title status productType vendor onlineStoreUrl "
            + ("totalInventory " if self.has_scope("read_inventory") else "")
            + "variantsCount { count } featuredMedia { preview { image { url altText } } } "
            "priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount } } } } }",
            {"n": min(max(limit, 1), 50), "q": query or None, "after": after or None},
        )
        products = data.get("products") or {}
        rows = []
        for node in products.get("nodes", []):
            price = node.get("priceRangeV2") or {}
            image = (((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {})
            rows.append({"id": node.get("id"), "title": node.get("title"), "status": str(node.get("status") or "").lower(),
                         "product_type": node.get("productType"), "vendor": node.get("vendor"), "url": node.get("onlineStoreUrl"),
                         "inventory": node.get("totalInventory"), "options": (node.get("variantsCount") or {}).get("count"),
                         "image": image.get("url"), "currency": (price.get("minVariantPrice") or {}).get("currencyCode"),
                         "min_price": (price.get("minVariantPrice") or {}).get("amount"), "max_price": (price.get("maxVariantPrice") or {}).get("amount")})
        page = products.get("pageInfo") or {}
        return {"products": rows, "next": page.get("endCursor") if page.get("hasNextPage") else None}

    HEALTH_SECONDS = 600
    HEALTH_CHECKS = {
        "zero_price": "Priced at $0",
        "no_measurements": "No measurements listed",
        "other_product_text": "Description may belong to another product",
        "size_conflict": "Size in the title differs from the description",
        "no_description": "No description",
        "no_image": "No image",
        "no_type": "No product type",
        "sold_out": "Sold out",
    }
    _MEASURE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|l|litres?|liters?|inch|in)\b|\d{2,4}\s*[x×*]\s*\d{2,4}", re.I)

    def catalog_health(self, refresh: bool = False) -> Dict[str, Any]:
        """Every active product checked for what makes the AI's answers weak or wrong (a $0 price, no measurements,
        a description that seems copied from another product, no description, image or type, sold out), each with a
        link to fix it in Shopify admin. Cached for 10 minutes."""
        cached = getattr(self, "_health", None)
        if cached and not refresh and time.time() - cached[0] < self.HEALTH_SECONDS:
            return cached[1]
        store = self.store_domain.split(".")[0]
        rows: List[Dict[str, Any]] = []
        total, after = 0, None
        for _ in range(12):  # up to 3,000 products
            data = self.graphql(
                "query($after: String) { products(first: 250, after: $after, query: \"status:active\") { pageInfo { hasNextPage endCursor } nodes { "
                "id title productType totalInventory description descriptionHtml featuredMedia { id } "
                "options { name values } priceRangeV2 { minVariantPrice { amount currencyCode } } } } }", {"after": after})
            products = data.get("products") or {}
            for node in products.get("nodes", []):
                total += 1
                issues = self._product_issues(node)
                if issues:
                    number = str(node.get("id") or "").rsplit("/", 1)[-1]
                    rows.append({"id": node.get("id"), "title": node.get("title"), "issues": issues,
                                 "price": ((node.get("priceRangeV2") or {}).get("minVariantPrice") or {}).get("amount"),
                                 "edit_url": f"https://admin.shopify.com/store/{store}/products/{number}"})
            page = products.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        counts = {key: sum(1 for row in rows if key in row["issues"]) for key in self.HEALTH_CHECKS}
        # The issues that make answers wrong first, then those that make them thin.
        order = list(self.HEALTH_CHECKS)
        rows.sort(key=lambda row: min(order.index(issue) for issue in row["issues"]))
        result = {"checked": total, "with_issues": len(rows), "counts": counts, "labels": self.HEALTH_CHECKS, "products": rows,
                  "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self._health = (time.time(), result)
        logger.info("shopify: catalogue health %d of %d products with issues %s", len(rows), total, {k: v for k, v in counts.items() if v})
        return result

    def _product_issues(self, node: Dict[str, Any]) -> List[str]:
        title = str(node.get("title") or "")
        description = str(node.get("description") or "")
        options = " ".join(" ".join(option.get("values") or []) for option in node.get("options") or [])
        issues = []
        if float(((node.get("priceRangeV2") or {}).get("minVariantPrice") or {}).get("amount") or 0) <= 0:
            issues.append("zero_price")
        if not self._MEASURE.search(f"{title} {options} {description}"):
            issues.append("no_measurements")
        # A "Product:" spec line naming a different product than the title: the description was probably copied.
        if _names_other_product(title, _spec_lines(node.get("descriptionHtml"))):
            issues.append("other_product_text")
        if _size_conflict(title, description):
            issues.append("size_conflict")
        if len(description.strip()) < 40:
            issues.append("no_description")
        if not node.get("featuredMedia"):
            issues.append("no_image")
        if not str(node.get("productType") or "").strip() or str(node.get("productType")).strip().lower() == "unclassified":
            issues.append("no_type")
        if isinstance(node.get("totalInventory"), int) and node["totalInventory"] <= 0:
            issues.append("sold_out")
        return issues

    def customer_list(self, query: str = "", after: str = "", limit: int = 50) -> Dict[str, Any]:
        """Recent customers for the Store data page: name, orders, spend, where. Email and phone only if Shopify
        approved protected customer data for the app (otherwise Shopify returns them empty)."""
        data = self.graphql(
            "query($n: Int!, $q: String, $after: String) { customers(first: $n, query: $q, after: $after, sortKey: UPDATED_AT, reverse: true) { pageInfo { hasNextPage endCursor } nodes { "
            "id displayName email phone numberOfOrders createdAt amountSpent { amount currencyCode } defaultAddress { city province country phone } } } }",
            {"n": min(max(limit, 1), 100), "q": query or None, "after": after or None},
        )
        rows = []
        customers = data.get("customers") or {}
        for node in customers.get("nodes", []):
            address = node.get("defaultAddress") or {}
            spent = node.get("amountSpent") or {}
            rows.append({"id": node.get("id"), "name": node.get("displayName"), "email": node.get("email"), "phone": node.get("phone") or address.get("phone"),
                         "orders": int(node.get("numberOfOrders") or 0), "spent": spent.get("amount"), "currency": spent.get("currencyCode"),
                         "since": node.get("createdAt"), "place": ", ".join(part for part in (address.get("city"), address.get("province"), address.get("country")) if part)})
        page = customers.get("pageInfo") or {}
        return {"customers": rows, "next": page.get("endCursor") if page.get("hasNextPage") else None}

    def policies(self) -> List[Dict[str, str]]:
        data = self.graphql("{ shop { shopPolicies { type title body } } }")
        rows = []
        for policy in (data.get("shop") or {}).get("shopPolicies", []):
            text = re.sub(r"<[^>]+>", " ", policy.get("body") or "")
            rows.append({"type": policy.get("type"), "title": policy.get("title"), "text": re.sub(r"\s+", " ", text).strip()})
        return rows


shopify_service = ShopifyService()
