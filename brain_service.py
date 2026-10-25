from datetime import datetime
import hashlib
import io
import json
import os
import re
import requests
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import threading
from time import monotonic, perf_counter
from typing import Any, Dict, List, Optional

from ai_services import AIServices
from agent_config import agent_config
from conversation_memory import bounded_history
from logging_setup import timed
from mongo_client import mongo_client
from qa_engine import DynamicQA
from response_renderer import render_response
from shopify_service import phones_match, shopify_service
import meaning
import store_intent
import logging

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
    from pypdf import PdfReader
    from llama_index.core import Document
    from llama_index.core.ingestion import IngestionPipeline
    from llama_index.core.node_parser import SentenceSplitter
    from llama_index.embeddings.openai import OpenAIEmbedding
except ImportError:
    BeautifulSoup = None
    PdfReader = None
    Document = None
    IngestionPipeline = None
    SentenceSplitter = None
    OpenAIEmbedding = None


DEFAULT_TOOLS = {
    "search_shopify_products": True,
    "check_inventory": True,
    "get_product_price": True,
    "get_product_details": True,
    "get_order": True,
    "get_customer_orders": True,
    "get_store_policy": True,
    "create_lead": True,
    "update_lead": True,
    "send_whatsapp_message": True,
    "send_gmail_email": False,
    "transfer_voice_call": True,
    "human_handoff": True,
    "custom_api_calls": False,
}


VOICE_REPLY_CHECKLIST = (
    "Before you answer this caller, check: (1) Answer every part of their question in your first sentence; "
    "if they asked about price, state the price facts from the knowledge. (2) Use only facts written in the "
    "knowledge: if a timeline, model name, number, or availability is not written there, say the team will "
    "confirm it on a callback. (3) Speak as the assistant and refer to the owner by name, never 'I build' or "
    "'my rate'. (4) Under 30 words, no opener like 'Sure' or 'Great', and no question unless you truly need one."
)


# Live-call speed: a voice turn must not wait on slow network lookups that rarely change.
# - query embeddings are cached by text (prefetched while the caller is still speaking, see retell_api);
# - small reads (channel settings, caller context, the knowledge documents) are cached for a few seconds.
_EMBEDDING_CACHE: "OrderedDict[str, List[float]]" = OrderedDict()
_EMBEDDING_CACHE_SIZE = 512
_EMBEDDING_LOCK = threading.Lock()
_EMBEDDING_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="embed")
_EMBEDDING_INFLIGHT: Dict[str, Future] = {}
_TTL_CACHE: Dict[str, tuple] = {}
_TTL_LOCK = threading.Lock()


def _ttl_cached(key: str, ttl_seconds: float, load):
    """Return a recent value for key, or load and remember it. Used only for reads that may be a few seconds stale."""
    now = monotonic()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and now - hit[0] < ttl_seconds:
            return hit[1]
    value = load()
    with _TTL_LOCK:
        _TTL_CACHE[key] = (now, value)
    return value


# Local embeddings: a small open model (BAAI/bge-small-en-v1.5, ~67 MB, CPU) turns a question into a search vector
# in ~35 ms, against 1-1.5 s for a network embedding API, and cannot be rate-limited or go down. Knowledge chunks
# keep their OpenRouter vector ("embedding", used by Atlas search) and also get a local one ("embedding_local").
# Turn off with LOCAL_EMBEDDINGS=false; if the model cannot load, search falls back to the network embeddings.
_LOCAL_EMBEDDER: Any = None
_LOCAL_EMBEDDER_FAILED = False
_LOCAL_EMBEDDER_LOCK = threading.Lock()


def _local_embedder() -> Any:
    global _LOCAL_EMBEDDER, _LOCAL_EMBEDDER_FAILED
    if _LOCAL_EMBEDDER is not None or _LOCAL_EMBEDDER_FAILED:
        return _LOCAL_EMBEDDER
    # Render's small instances cannot reliably load the ONNX model during a live call.
    # Keep semantic retrieval there through the configured network embedding provider.
    default = "false" if os.getenv("RENDER_EXTERNAL_URL") else "true"
    if os.getenv("LOCAL_EMBEDDINGS", default).strip().lower() in {"0", "false", "no", "off"}:
        return None
    with _LOCAL_EMBEDDER_LOCK:
        if _LOCAL_EMBEDDER is None and not _LOCAL_EMBEDDER_FAILED:
            try:
                from fastembed import TextEmbedding
                cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "fastembed")
                _LOCAL_EMBEDDER = TextEmbedding(os.getenv("LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"), cache_dir=cache_dir)
                logger.info("local embedding model ready: %s", os.getenv("LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"))
            except Exception as exc:
                _LOCAL_EMBEDDER_FAILED = True
                logger.warning(f"local embeddings unavailable, using network embeddings: {type(exc).__name__}: {exc}")
    return _LOCAL_EMBEDDER


def local_embeddings(texts: List[str], query: bool = False) -> List[List[float]]:
    """Normalised local vectors for texts (passages by default, or search queries); [] when unavailable."""
    model = _local_embedder()
    if model is None or not texts:
        return []
    try:
        vectors = model.query_embed(texts) if query else model.passage_embed(texts)
        return [[float(value) for value in vector] for vector in vectors]
    except Exception as exc:
        logger.warning(f"local embedding failed: {type(exc).__name__}: {exc}")
        return []


def clear_brain_caches() -> None:
    """Forget cached settings and documents (call after editing the brain so the next turn sees it at once).
    Also bumps the shared brain version, so other server processes drop their copies within seconds."""
    with _TTL_LOCK:
        _TTL_CACHE.clear()
    try:
        if mongo_client.is_connected():
            mongo_client.platform_settings.update_one({"key": "brain_version"}, {"$inc": {"value": 1}}, upsert=True)
    except Exception as exc:
        logger.warning(f"could not bump the brain version: {type(exc).__name__}")
    # The knowledge changed: rebuild the speech-recognition key terms from it (background, voice_keyterms.py).
    from voice_keyterms import schedule_refresh
    schedule_refresh()


def _brain_version() -> int:
    """The shared knowledge version, re-read at most every 5 seconds; cached knowledge is keyed by it."""
    def load() -> int:
        try:
            record = mongo_client.platform_settings.find_one({"key": "brain_version"}, {"value": 1}) if mongo_client.is_connected() else None
            return int((record or {}).get("value") or 0)
        except Exception:
            return -1
    return _swr_cached("brain_version", 5, load)


_SWR_REFRESHING: set = set()


def _swr_cached(key: str, ttl_seconds: float, load):
    """Like _ttl_cached, but once a value exists a stale one is returned at once and refreshed in the background
    (stale-while-revalidate): live call turns never wait on a database round trip for it."""
    now = monotonic()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit and now - hit[0] < ttl_seconds:
            return hit[1]
        refresh = bool(hit) and key not in _SWR_REFRESHING
        if refresh:
            _SWR_REFRESHING.add(key)
    if not hit:
        return _ttl_cached(key, ttl_seconds, load)
    if refresh:
        def run() -> None:
            try:
                value = load()
                with _TTL_LOCK:
                    _TTL_CACHE[key] = (monotonic(), value)
            finally:
                with _TTL_LOCK:
                    _SWR_REFRESHING.discard(key)
        threading.Thread(target=run, name=f"refresh-{key}", daemon=True).start()
    return hit[1]


def invalidate_customer_context(phone: Optional[str]) -> None:
    """Drop one caller's cached CRM context after the voice pipeline saves new identity data."""
    if not phone:
        return
    with _TTL_LOCK:
        _TTL_CACHE.pop(f"customer:{phone}", None)


class BrainService:
    def _setting(self, key: str, default: Any) -> Any:
        if not mongo_client.is_connected():
            return default
        record = mongo_client.platform_settings.find_one({"key": key})
        return record.get("value", default) if record else default

    def tools(self) -> Dict[str, bool]:
        value = self._setting("ai_tools", DEFAULT_TOOLS)
        return {**DEFAULT_TOOLS, **value} if isinstance(value, dict) else DEFAULT_TOOLS.copy()

    def brain_config(self) -> Dict[str, Any]:
        stored = self._setting("brain", {})
        if not stored:
            legacy = agent_config.get_knowledge_base()
            if legacy:
                profile = legacy.get("company_profile") or {}
                support = legacy.get("site_visits_and_support") or {}
                projects = legacy.get("projects") or {}
                locations = [
                    f"{name}: {details.get('location', '')}".strip(": ")
                    for name, details in projects.items()
                    if isinstance(details, dict) and details.get("location")
                ]
                stored = {
                    "name": profile.get("developer", "AgentFlow Brain"),
                    "business_description": profile.get("about", ""),
                    "company_information": "\n".join(
                        item for item in (
                            f"Developer: {profile.get('developer')}" if profile.get("developer") else "",
                            f"Headquarters: {profile.get('headquarters')}" if profile.get("headquarters") else "",
                            f"Service area: {profile.get('service_area')}" if profile.get("service_area") else "",
                            f"Phone: {profile.get('contact_phone')}" if profile.get("contact_phone") else "",
                            f"Email: {profile.get('contact_email')}" if profile.get("contact_email") else "",
                            f"Website: {profile.get('website')}" if profile.get("website") else "",
                            f"RERA ID: {profile.get('rera_id')}" if profile.get("rera_id") else "",
                        ) if item
                    ),
                    "locations": locations,
                    "working_hours": support.get("customer_service_hours", ""),
                    "services": legacy.get("what_we_help_with", []),
                    "faqs": legacy.get("frequently_asked_questions", []),
                    "policies": legacy.get("answering_policy", []),
                    "custom_knowledge": json.dumps(legacy, ensure_ascii=False, indent=2),
                }
        return {
            "name": stored.get("name", "AgentFlow Brain"),
            "business_description": stored.get("business_description", ""),
            "company_information": stored.get("company_information", ""),
            "locations": stored.get("locations", []),
            "working_hours": stored.get("working_hours", ""),
            "services": stored.get("services", []),
            "faqs": stored.get("faqs", []),
            "policies": stored.get("policies", []),
            "website_content": stored.get("website_content", ""),
            "custom_knowledge": stored.get("custom_knowledge", ""),
            "documents": stored.get("documents", []),
            "index": self.index_status(),
            "sources": self.source_list(),
        }

    def _knowledge_source_items(self, brain: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
        brain = brain or self.brain_config()
        items: List[Dict[str, str]] = []
        for key in ("company_information", "business_description", "locations", "working_hours", "services", "faqs", "policies", "website_content", "custom_knowledge"):
            value = brain.get(key)
            if isinstance(value, list):
                value = "\n".join(str(item) for item in value if item)
            elif isinstance(value, dict):
                value = "\n".join(f"{name}: {content}" for name, content in value.items())
            text = str(value or "").strip()
            if text:
                items.append({"source_key": key, "source_name": key.replace("_", " ").title(), "text": text})
        return items

    def _embedding_model(self) -> Any:
        if getattr(self, "_embed_model_checked", False):
            return getattr(self, "_embed_model", None)
        self._embed_model_checked = True
        provider = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower()
        if provider not in {"auto", "openai"} or OpenAIEmbedding is None or not os.getenv("OPENAI_API_KEY"):
            self._embed_model = None
            return None
        try:
            model = os.getenv("OPENAI_EMBEDDING_MODEL") or os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
            dimensions = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
            self._embed_model = OpenAIEmbedding(model=model, dimensions=dimensions)
        except Exception:
            self._embed_model = None
        return self._embed_model

    def _embedding_provider(self) -> str:
        """Return the configured primary provider; auto means OpenRouter, OpenAI, then Gemini."""
        provider = os.getenv("EMBEDDING_PROVIDER", "auto").strip().lower()
        return provider if provider in {"auto", "openrouter", "openai", "gemini"} else "auto"

    def _embedding_dimensions(self) -> int:
        try:
            return max(128, min(3072, int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))))
        except (TypeError, ValueError):
            return 1536

    def _embedding_model_name(self, provider: Optional[str] = None) -> str:
        provider = provider or self._embedding_provider()
        if provider == "openrouter":
            return os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
        if provider in {"auto", "openai"}:
            return os.getenv("OPENAI_EMBEDDING_MODEL") or os.getenv("RAG_EMBEDDING_MODEL", "text-embedding-3-small")
        return os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2").strip()

    def _embedding_provider_order(self) -> List[str]:
        provider = self._embedding_provider()
        if provider == "openrouter":
            return ["openrouter", "gemini"]
        if provider == "openai":
            return ["openai", "gemini"]
        if provider == "gemini":
            return ["gemini"]
        return ["openrouter", "openai", "gemini"]

    def _openrouter_embeddings(self, texts: List[str]) -> List[List[float]]:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key or not texts:
            return []
        model = os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small").strip()
        try:
            with timed(logger, f"embeddings {model}", warn_after_ms=2000):
                response = requests.post(
                    "https://openrouter.ai/api/v1/embeddings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "input": texts, "dimensions": self._embedding_dimensions(), "encoding_format": "float"},
                    timeout=30,
                )
            response.raise_for_status()
            items = response.json().get("data") or []
            items = sorted(items, key=lambda item: item.get("index", 0))
            vectors = [item.get("embedding") or [] for item in items]
            if len(vectors) != len(texts) or not all(isinstance(vector, list) and vector for vector in vectors):
                return []
            return vectors
        except Exception as exc:
            logger.warning(f"OpenRouter embedding failed; trying fallback: {type(exc).__name__}")
            return []

    def _gemini_embedding(self, text: str, task_type: str) -> List[float]:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or not text:
            return []
        model = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2").strip()
        payload = {
            "model": f"models/{model}",
            "content": {"parts": [{"text": text}]},
            "embedContentConfig": {
                "taskType": task_type,
                "outputDimensionality": self._embedding_dimensions(),
                "autoTruncate": False,
            },
        }
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent?key={api_key}",
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            values = ((response.json().get("embedding") or {}).get("values") or [])
            return values if isinstance(values, list) else []
        except Exception as exc:
            logger.error(f"Gemini embedding failed: {type(exc).__name__}")
            return []

    def _gemini_embeddings(self, chunks: List[str]) -> List[List[float]]:
        return [self._gemini_embedding(chunk, "RETRIEVAL_DOCUMENT") for chunk in chunks]

    def _chunk_text(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> List[str]:
        if Document is not None and SentenceSplitter is not None:
            try:
                document = Document(text=text, metadata=metadata or {})
                pipeline = IngestionPipeline(transformations=[SentenceSplitter(chunk_size=512, chunk_overlap=64)])
                nodes = pipeline.run(documents=[document], show_progress=False)
                chunks = [node.get_content().strip() for node in nodes if node.get_content().strip()]
                if chunks:
                    return chunks
            except Exception:
                pass
        return [text[index:index + 1200].strip() for index in range(0, len(text), 1200) if text[index:index + 1200].strip()]

    def _embeddings(self, chunks: List[str]) -> tuple[List[List[float]], Optional[str]]:
        """Embed documents with the configured primary and safe fallbacks."""
        for provider in self._embedding_provider_order():
            if provider == "openrouter":
                vectors = self._openrouter_embeddings(chunks)
                if vectors:
                    return vectors, "openrouter"
            elif provider == "openai":
                model = self._embedding_model()
                if model is not None and chunks:
                    try:
                        vectors = model.get_text_embedding_batch(chunks, show_progress=False)
                        if vectors and len(vectors) == len(chunks) and all(isinstance(vector, list) and vector for vector in vectors):
                            return vectors, "openai"
                    except Exception as exc:
                        logger.warning(f"OpenAI embedding failed; trying fallback: {type(exc).__name__}")
            elif provider == "gemini":
                vectors = self._gemini_embeddings(chunks)
                if vectors and any(vectors):
                    return vectors, "gemini"
        return [[] for _ in chunks], None

    @staticmethod
    def _section_chunks(items: List[Dict[str, str]], max_chars: int = 900) -> List[str]:
        """Chunks for the business knowledge: one section at a time, whole lines only (an FAQ question stays with
        its answer), each chunk labelled with its section. Small focused chunks let a voice turn fetch just the
        facts it needs instead of a quarter of the whole knowledge."""
        chunks: List[str] = []
        for item in items:
            label = f"{item['source_name']}:"
            current: List[str] = []
            for line in (line.strip() for line in item["text"].splitlines()):
                if not line:
                    continue
                if current and len("\n".join([label, *current, line])) > max_chars:
                    chunks.append("\n".join([label, *current]))
                    current = []
                current.append(line)
            if current:
                chunks.append("\n".join([label, *current]))
        return chunks

    def ingest_text(self, source_type: str, source_name: str, text: str, source_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                    chunks: Optional[List[str]] = None) -> Dict[str, Any]:
        collection = getattr(mongo_client, "brain_documents", None)
        sources = getattr(mongo_client, "brain_sources", None)
        if not mongo_client.is_connected() or collection is None or sources is None:
            raise RuntimeError("Database not connected")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("The source did not contain readable text")
        source_id = source_id or f"{source_type}:{hashlib.sha256(clean_text.encode('utf-8')).hexdigest()}"
        chunks = chunks or self._chunk_text(clean_text, metadata)
        vectors, embedding_provider = self._embeddings(chunks)
        local_vectors = local_embeddings(chunks)
        now = datetime.utcnow()
        collection.delete_many({"source_id": source_id})
        documents = [{
            "source_id": source_id,
            "source_key": source_id,
            "source_type": source_type,
            "source_name": source_name,
            "text": chunk,
            "chunk_index": index,
            "embedding": vectors[index] if index < len(vectors) else [],
            "embedding_model": self._embedding_model_name(embedding_provider) if vectors[index] else None,
            "embedding_local": local_vectors[index] if index < len(local_vectors) else [],
            "metadata": metadata or {},
            "updated_at": now,
        } for index, chunk in enumerate(chunks)]
        collection.insert_many(documents)
        clear_brain_caches()  # the next call turn searches the new knowledge at once
        source = {
            "source_id": source_id,
            "source_type": source_type,
            "source_name": source_name,
            "status": "ready",
            "chunks": len(documents),
            "embedding_enabled": bool(vectors and vectors[0]),
            "embedding_model": self._embedding_model_name(embedding_provider) if vectors and vectors[0] else None,
            "content_hash": hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
            "metadata": metadata or {},
            "updated_at": now,
        }
        sources.update_one({"source_id": source_id}, {"$set": source, "$setOnInsert": {"created_at": now}}, upsert=True)
        return {"source_id": source_id, "source_type": source_type, "source_name": source_name, "chunks": len(documents), "embedding_enabled": bool(vectors and vectors[0])}

    def reindex_knowledge(self, brain: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        collection = getattr(mongo_client, "brain_documents", None)
        if not mongo_client.is_connected() or collection is None:
            return {"ready": False, "documents_count": 0, "source_count": 0, "last_indexed_at": None}
        # Remove pre-RAG rows and replace only the settings source; uploaded, scraped, and Shopify sources remain intact.
        collection.delete_many({"source_id": {"$exists": False}})
        collection.delete_many({"source_id": "brain_settings"})
        clear_brain_caches()
        sources = getattr(mongo_client, "brain_sources", None)
        if sources is not None:
            sources.delete_one({"source_id": "brain_settings"})
        items = self._knowledge_source_items(brain)
        text = "\n\n".join(f"{item['source_name']}:\n{item['text']}" for item in items)
        if text:
            self.ingest_text("settings", "Business knowledge", text, source_id="brain_settings", chunks=self._section_chunks(items))
        return self.index_status()

    def ingest_pdf(self, file_bytes: bytes, filename: str) -> Dict[str, Any]:
        if PdfReader is None:
            raise RuntimeError("PDF support is not installed")
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [f"Page {index + 1}:\n{page.extract_text() or ''}" for index, page in enumerate(reader.pages)]
        source_id = f"pdf:{hashlib.sha256(file_bytes).hexdigest()}"
        sources = getattr(mongo_client, "brain_sources", None)
        collection = getattr(mongo_client, "brain_documents", None)
        if sources is not None and collection is not None:
            previous = list(sources.find({"source_type": "pdf", "metadata.filename": filename, "source_id": {"$ne": source_id}}, {"source_id": 1}))
            old_ids = [row.get("source_id") for row in previous if row.get("source_id")]
            if old_ids:
                collection.delete_many({"source_id": {"$in": old_ids}})
                sources.delete_many({"source_id": {"$in": old_ids}})
        return self.ingest_text("pdf", filename, "\n\n".join(pages), source_id=source_id, metadata={"filename": filename, "pages": len(reader.pages)})

    def ingest_url(self, url: str) -> Dict[str, Any]:
        if BeautifulSoup is None:
            raise RuntimeError("Web scraping support is not installed")
        import requests
        response = requests.get(url, timeout=20, headers={"User-Agent": "AgentFlow knowledge crawler/1.0"})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        title = " ".join(soup.title.get_text(" ", strip=True).split()) if soup.title else url
        for element in soup(["script", "style", "noscript", "svg", "nav", "header", "iframe", "form"]):
            element.decompose()
        # Keep the page's own content plus the footer (contact details usually live there); site menus repeated on
        # every page would otherwise fill each chunk and drown out what the page is actually about.
        main = soup.find("main")
        footer = soup.find("footer")
        if main is not None:
            text = "\n".join(part.get_text("\n", strip=True) for part in (main, footer) if part is not None)
        else:
            text = soup.get_text("\n", strip=True)
        return self.ingest_text("web", title[:200], text, source_id=f"web:{hashlib.sha256(url.encode('utf-8')).hexdigest()}", metadata={"url": url})

    def ingest_shopify_catalog(self) -> Dict[str, Any]:
        if not shopify_service.configured:
            raise RuntimeError("Shopify is not configured")
        products = shopify_service.products(limit=100)
        lines = []
        for product in products:
            variants = product.get("variants") or []
            variant_text = "; ".join(f"{item.get('title', 'Variant')}: price {item.get('price', 'unavailable')}, inventory {item.get('inventory_quantity', 0)}" for item in variants)
            lines.append(f"Product: {product.get('title', 'Unknown')}\nType: {product.get('product_type', '')}\nVendor: {product.get('vendor', '')}\nStatus: {product.get('status', 'unknown')}\nVariants: {variant_text}")
        return self.ingest_text("shopify", "Shopify product catalog", "\n\n".join(lines), source_id="shopify:catalog", metadata={"products": len(products), "live": True})

    def source_list(self) -> List[Dict[str, Any]]:
        sources = getattr(mongo_client, "brain_sources", None)
        if not mongo_client.is_connected() or sources is None:
            return []
        rows = list(sources.find({}).sort("updated_at", -1).limit(100))
        for row in rows:
            row.pop("_id", None)
            if isinstance(row.get("updated_at"), datetime):
                row["updated_at"] = row["updated_at"].isoformat() + "Z"
        return rows

    def resync_source(self, source_id: str) -> Dict[str, Any]:
        """Refresh one stored source, replacing its previous chunks by source id."""
        sources = getattr(mongo_client, "brain_sources", None)
        collection = getattr(mongo_client, "brain_documents", None)
        if not mongo_client.is_connected() or sources is None or collection is None:
            raise RuntimeError("Database not connected")
        if source_id == "brain_settings":
            result = self.reindex_knowledge(self.brain_config())
            return {"source_id": source_id, "source_type": "settings", "status": "ready", "index": result}
        source = sources.find_one({"source_id": source_id})
        if not source:
            raise ValueError("Source not found")
        source_type = source.get("source_type")
        if source_type == "web":
            url = (source.get("metadata") or {}).get("url")
            if not url:
                raise ValueError("This webpage does not have a saved URL")
            result = self.ingest_url(url)
        elif source_type == "pdf":
            chunks = list(collection.find({"source_id": source_id}, {"text": 1, "chunk_index": 1}).sort("chunk_index", 1))
            text = "\n\n".join(str(chunk.get("text", "")) for chunk in chunks if chunk.get("text"))
            if not text:
                raise ValueError("The stored PDF has no readable text; upload it again")
            result = self.ingest_text("pdf", source.get("source_name", "PDF document"), text, source_id=source_id, metadata=source.get("metadata") or {})
        else:
            raise ValueError("This source is live-managed and does not need a static resync")
        return {**result, "status": "ready", "resynced": True}

    def delete_source(self, source_id: str) -> Dict[str, Any]:
        """Remove a knowledge source and every chunk of it, so no channel can quote it again (takes effect at once)."""
        sources = getattr(mongo_client, "brain_sources", None)
        collection = getattr(mongo_client, "brain_documents", None)
        if not mongo_client.is_connected() or sources is None or collection is None:
            raise RuntimeError("Database not connected")
        if source_id == "brain_settings":
            raise ValueError("Business knowledge is edited on the Knowledge form: clear the text there and save")
        source = sources.find_one({"source_id": source_id}, {"source_name": 1, "source_type": 1})
        removed = collection.delete_many({"source_id": source_id}).deleted_count
        if not source and not removed:
            raise LookupError("Source not found")
        sources.delete_one({"source_id": source_id})
        clear_brain_caches()
        logger.info("knowledge source removed: %s (%d chunks)", source_id, removed)
        return {"source_id": source_id, "source_name": (source or {}).get("source_name", ""), "source_type": (source or {}).get("source_type", ""), "chunks_removed": removed}

    def resync_static_sources(self) -> Dict[str, Any]:
        """Rebuild business knowledge and refresh every stored webpage/PDF source."""
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        try:
            results.append(self.resync_source("brain_settings"))
        except Exception as exc:
            errors.append({"source_id": "brain_settings", "error": str(exc)})
        for source in self.source_list():
            if source.get("source_type") not in {"web", "pdf"}:
                continue
            try:
                results.append(self.resync_source(source["source_id"]))
            except Exception as exc:
                errors.append({"source_id": source.get("source_id", ""), "source_name": source.get("source_name", ""), "error": str(exc)})
        return {"resynced": results, "errors": errors, "index": self.index_status(), "sources": self.source_list()}

    def index_status(self) -> Dict[str, Any]:
        collection = getattr(mongo_client, "brain_documents", None)
        if not mongo_client.is_connected() or collection is None:
            return {"ready": False, "documents_count": 0, "source_count": 0, "embedded_documents": 0, "retrieval_mode": "unavailable", "last_indexed_at": None}
        documents_count = collection.count_documents({})
        source_count = len(collection.distinct("source_id")) if documents_count else 0
        embedded_documents = collection.count_documents({"embedding.0": {"$exists": True}})
        latest = collection.find_one({}, sort=[("updated_at", -1)]) if documents_count else None
        last_indexed_at = latest.get("updated_at") if latest else None
        if isinstance(last_indexed_at, datetime):
            last_indexed_at = last_indexed_at.isoformat() + "Z"
        return {"ready": documents_count > 0, "documents_count": documents_count, "source_count": source_count, "embedded_documents": embedded_documents, "retrieval_mode": "hybrid_vector_lexical" if embedded_documents else "lexical_fallback", "last_indexed_at": last_indexed_at}

    def _atlas_vector_retrieve(self, query_vector: List[float], limit: int) -> List[Dict[str, Any]]:
        collection = getattr(mongo_client, "brain_documents", None)
        if collection is None or not query_vector or not os.getenv("RAG_ATLAS_VECTOR_SEARCH", "true").lower() in {"1", "true", "yes"}:
            return []
        try:
            index_name = os.getenv("RAG_ATLAS_VECTOR_INDEX_NAME", "vector_index")
            pipeline = [
                {"$vectorSearch": {"index": index_name, "path": "embedding", "queryVector": query_vector, "numCandidates": max(limit * 20, 50), "limit": limit}},
                {"$project": {"source_name": 1, "source_type": 1, "text": 1, "score": {"$meta": "vectorSearchScore"}}},
            ]
            minimum = self._minimum_relevance()
            hits = [{"source": item.get("source_name", "Knowledge"), "source_type": item.get("source_type", "settings"), "text": item.get("text", ""), "score": round(float(item.get("score", 0)), 3)} for item in collection.aggregate(pipeline)]
            return [item for item in hits if item["score"] >= minimum]
        except Exception:
            # Atlas Search is optional; a missing index or restricted permission must not break a live call.
            return []

    def _minimum_relevance(self) -> float:
        try:
            return max(0.0, min(1.0, float(os.getenv("RAG_MIN_RELEVANCE", "0.2"))))
        except (TypeError, ValueError):
            return 0.2

    def _vector_only_min_relevance(self) -> float:
        try:
            return max(0.0, min(1.0, float(os.getenv("RAG_VECTOR_ONLY_MIN", "0.4"))))
        except (TypeError, ValueError):
            return 0.4

    def _embed_query_uncached(self, text: str) -> List[float]:
        for provider in self._embedding_provider_order():
            vector: List[float] = []
            if provider == "openrouter":
                vectors = self._openrouter_embeddings([text])
                vector = vectors[0] if vectors else []
            elif provider == "openai":
                model = self._embedding_model()
                if model is not None:
                    try:
                        vector = model.get_query_embedding(text)
                    except Exception:
                        vector = []
            elif provider == "gemini":
                vector = self._gemini_embedding(text, "RETRIEVAL_QUERY")
            if vector:
                return vector
        return []

    def _embedding_future(self, text: str) -> Future:
        """One embedding request per distinct text at a time; the result lands in the cache when it finishes."""
        with _EMBEDDING_LOCK:
            if text in _EMBEDDING_CACHE:
                done: Future = Future(); done.set_result(_EMBEDDING_CACHE[text]); return done
            running = _EMBEDDING_INFLIGHT.get(text)
            if running is not None:
                return running

            def work() -> List[float]:
                try:
                    vector = self._embed_query_uncached(text)
                    if vector:
                        with _EMBEDDING_LOCK:
                            _EMBEDDING_CACHE[text] = vector
                            _EMBEDDING_CACHE.move_to_end(text)
                            while len(_EMBEDDING_CACHE) > _EMBEDDING_CACHE_SIZE:
                                _EMBEDDING_CACHE.popitem(last=False)
                    return vector
                finally:
                    with _EMBEDDING_LOCK:
                        _EMBEDDING_INFLIGHT.pop(text, None)

            future = _EMBEDDING_POOL.submit(work)
            _EMBEDDING_INFLIGHT[text] = future
            return future

    def prefetch_query_embedding(self, text: str) -> None:
        """Start embedding a caller's words before the reply is needed (no-op when cached or already running)."""
        text = str(text or "").strip()
        # With the local model the search vector takes ~35 ms when the answer needs it, so a network request now
        # would only cost money and tie up a worker thread during the call.
        if text and _local_embedder() is None:
            self._embedding_future(text)

    def query_embedding(self, text: str, budget_ms: Optional[int] = None) -> List[float]:
        """Embedding for a search query. With a budget, give up waiting after budget_ms (the request keeps running
        and fills the cache for next time) so a live call falls back to keyword search instead of stalling."""
        future = self._embedding_future(text)
        if budget_ms is not None and not future.done():
            # Live call: the caller's final words usually extend words we embedded while they were speaking.
            # That earlier embedding is close enough to search with, and needs no wait.
            earlier = self._cached_prefix_embedding(text)
            if earlier:
                return earlier
        try:
            return future.result(timeout=None if budget_ms is None else budget_ms / 1000)
        except FutureTimeout:
            logger.info(f"query embedding exceeded {budget_ms} ms; using keyword search for this turn")
            return []
        except Exception:
            return []

    @staticmethod
    def _cached_prefix_embedding(text: str) -> List[float]:
        """Embedding of the longest cached text whose words start the given text and cover most of it (60%+)."""
        words = re.findall(r"[a-z0-9']+", text.lower())
        if len(words) < 2:
            return []
        best: List[float] = []
        best_length = 0
        with _EMBEDDING_LOCK:
            for key, vector in _EMBEDDING_CACHE.items():
                key_words = re.findall(r"[a-z0-9']+", key.lower())
                if best_length < len(key_words) <= len(words) and len(key_words) >= 0.6 * len(words) and words[:len(key_words)] == key_words:
                    best, best_length = vector, len(key_words)
        return best

    def _knowledge_documents(self) -> List[Dict[str, Any]]:
        """The knowledge chunks (text and embeddings), cached briefly: the brain changes rarely, calls ask often."""
        collection = getattr(mongo_client, "brain_documents", None)
        return _swr_cached(f"brain_documents:{_brain_version()}", 300, lambda: list(collection.find({}, {"text": 1, "embedding": 1, "embedding_local": 1, "source_name": 1, "source_type": 1}).limit(5000)))

    def ensure_local_embeddings(self) -> int:
        """Give every knowledge chunk a local vector (older chunks predate them). Returns how many were added."""
        collection = getattr(mongo_client, "brain_documents", None)
        if collection is None or not mongo_client.is_connected() or _local_embedder() is None:
            return 0
        missing = list(collection.find({"$or": [{"embedding_local": {"$exists": False}}, {"embedding_local": []}]}, {"text": 1}))
        if not missing:
            return 0
        vectors = local_embeddings([str(item.get("text", "")) for item in missing])
        for item, vector in zip(missing, vectors):
            collection.update_one({"_id": item["_id"]}, {"$set": {"embedding_local": vector}})
        clear_brain_caches()
        logger.info("added local embeddings to %d knowledge chunks", len(vectors))
        return len(vectors)

    @staticmethod
    def knowledge_meaning_share() -> float:
        """KNOWLEDGE_MEANING_PERCENT: how much of a knowledge passage's score is meaning (the rest is shared words).
        Measured (scripts/tune_knowledge_search.py, 21 caller questions + 6 off-topic, 2026-10-22): 100% meaning puts the
        right passage first 15/21 and never answers an off-topic question (0/6); any share of shared words let 5/6
        off-topic questions ("a restaurant in Mumbai") pull business passages in, just for sharing a word."""
        try:
            return min(max(float(os.getenv("KNOWLEDGE_MEANING_PERCENT", "100")), 0.0), 100.0) / 100.0
        except ValueError:
            return 1.0

    def _local_retrieve(self, user_input: str, query_tokens: set, limit: int, meaning_share: Optional[float] = None) -> Optional[List[Dict[str, Any]]]:
        """Fast path: local query vector + in-memory cosine over the cached chunks. None means "not available here"
        (model missing, or some chunks have no local vector yet), so the caller uses the network path instead."""
        if _local_embedder() is None:
            return None
        documents = self._knowledge_documents()
        if not documents:
            return []
        if any(not item.get("embedding_local") for item in documents):
            threading.Thread(target=self.ensure_local_embeddings, daemon=True).start()
            return None
        query = local_embeddings([user_input], query=True)
        if not query:
            return None
        query_vector = query[0]
        share = self.knowledge_meaning_share() if meaning_share is None else meaning_share
        try:
            vector_only_min = float(os.getenv("LOCAL_VECTOR_ONLY_MIN", "0.55"))  # bge-small: on-topic 0.52-0.68, off-topic <= 0.50
        except ValueError:
            vector_only_min = 0.55
        scored: List[tuple[float, Dict[str, Any]]] = []
        for item in documents:
            text = str(item.get("text", ""))
            vector_score = sum(left * right for left, right in zip(query_vector, item["embedding_local"]))  # both normalised
            if share >= 1.0:
                # By meaning alone: a passage must be close enough in meaning, or an off-topic question gets answered.
                if vector_score < vector_only_min:
                    continue
                scored.append((vector_score, item))
                continue
            lexical = len(query_tokens.intersection(set(re.findall(r"[a-z0-9]{3,}", text.lower())))) / max(len(query_tokens), 1)
            # Same rule as the network path: a loose semantic match alone must not make an off-topic question answerable.
            if lexical <= 0 and vector_score < vector_only_min:
                continue
            scored.append((share * vector_score + (1 - share) * lexical, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        minimum = self._minimum_relevance()
        return [{"source": item.get("source_name", "Knowledge"), "source_type": item.get("source_type", "settings"), "text": item.get("text", ""), "score": round(score, 3)} for score, item in scored if score >= minimum][:limit]

    def knowledge_text(self) -> str:
        """All knowledge as one text (cached with the documents), e.g. to check whether a place is mentioned at all."""
        documents = self._knowledge_documents() if mongo_client.is_connected() else []
        return _swr_cached(f"knowledge_text:{_brain_version()}:{len(documents)}", 300, lambda: "\n".join(str(item.get("text", "")) for item in documents))

    def retrieve_knowledge(self, user_input: str, limit: int = 4, embed_budget_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        collection = getattr(mongo_client, "brain_documents", None)
        if not mongo_client.is_connected() or collection is None or not user_input.strip():
            return []
        stop_words = {"the", "and", "for", "with", "what", "how", "are", "can", "you", "our", "this", "that", "from", "about", "city", "company", "business", "project", "projects"}
        query_tokens = {token for token in re.findall(r"[a-z0-9]{3,}", user_input.lower()) if token not in stop_words}
        query_aliases = {
            "office": {"address", "contact"},
            "located": {"location", "address"},
            "phone": {"contact"},
            "email": {"contact"},
        }
        for token in tuple(query_tokens):
            query_tokens.update(query_aliases.get(token, set()))
        if not query_tokens and self.knowledge_meaning_share() < 1.0:
            return []
        local = self._local_retrieve(user_input.strip(), query_tokens, limit)
        if local is not None:
            return local
        query_vector = self.query_embedding(user_input.strip(), embed_budget_ms)
        atlas_hits = self._atlas_vector_retrieve(query_vector, limit)
        if atlas_hits:
            return atlas_hits
        query_norm = sum(value * value for value in query_vector) ** 0.5
        scored: List[tuple[float, Dict[str, Any]]] = []
        for item in self._knowledge_documents():
            text = str(item.get("text", ""))
            text_tokens = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
            lexical = len(query_tokens.intersection(text_tokens)) / max(len(query_tokens), 1)
            vector = item.get("embedding") or []
            vector_score = 0.0
            if query_vector and vector and len(vector) == len(query_vector):
                vector_norm = sum(value * value for value in vector) ** 0.5
                if vector_norm and query_norm:
                    vector_score = sum(left * right for left, right in zip(query_vector, vector)) / (query_norm * vector_norm)
            share = self.knowledge_meaning_share()
            if share >= 1.0:  # by meaning alone (see knowledge_meaning_share)
                if vector_score < self._vector_only_min_relevance():
                    continue
                scored.append((vector_score, item))
                continue
            # Do not let a generic semantic match (for example, “restaurant in Mumbai”)
            # turn a shared location word into an answerable business question.
            if lexical <= 0 and vector_score < self._vector_only_min_relevance():
                continue
            score = (share * vector_score + (1 - share) * lexical) if vector_score else lexical
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        minimum = self._minimum_relevance()
        return [{"source": item.get("source_name", "Knowledge"), "source_type": item.get("source_type", "settings"), "text": item.get("text", ""), "score": round(score, 3)} for score, item in scored if score >= minimum][:limit]

    def channel_config(self, channel: str) -> Dict[str, Any]:
        defaults = {
            "name": "WhatsApp Concierge" if channel == "whatsapp" else "Voice Concierge",
            "instructions": agent_config.get_system_prompt(),
            "personality": "Professional and helpful",
            "tone": "Warm and concise",
            "language": "English",
            "greeting": agent_config.get_greeting_message(),
            "business_hours": "Always available",
            "human_handoff_rules": "Transfer when explicitly requested or when the AI cannot safely resolve the request.",
            "human_transfer": agent_config.get_human_transfer(),
            "knowledge_enabled": True,
            "shopify_access": True,
        }
        stored = _swr_cached(f"setting:{channel}_agent:{_brain_version()}", 60, lambda: self._setting(f"{channel}_agent", {}))
        return {**defaults, **stored} if isinstance(stored, dict) else defaults

    def _out_of_scope_response(self) -> str:
        return "I'm sorry, I don't have information on that. I can help with questions about our services, projects, and how we work together."

    @staticmethod
    def _is_capability_query(user_input: str) -> bool:
        text = re.sub(r"[^a-z0-9\s]", " ", str(user_input or "").lower())
        text = " ".join(text.split())
        return bool(re.search(
            r"^(?:so )?(?:hi|hello|hey|good morning|good afternoon|good evening|"
            r"how can you help(?: me)?|what can you help(?: me)? with|"
            r"what can you do|what do you offer|tell me about your services|"
            r"tell me about the company|who are you)(?: please)?$",
            text,
        ))

    def _capability_response(self) -> str:
        """Answer broad capability questions without requiring a RAG hit."""
        brain = self.brain_config()
        services = brain.get("services") or [
            "AI agents for customer support and sales",
            "voice AI and appointment booking",
            "WhatsApp assistants and follow-ups",
            "website and document chatbots",
            "n8n workflows and CRM integrations",
            "custom web, mobile, and backend systems",
        ]
        if isinstance(services, str):
            services = [services]
        service_text = ", ".join(str(item).strip() for item in services if str(item).strip())
        return (
            "I can help design AI agents, voice and WhatsApp automations, chatbots, n8n workflows, "
            "CRM/calendar/email/API integrations, and custom web or mobile software. "
            f"The main services available are {service_text}. What process would you like to improve?"
        )

    def customer_context(self, phone: Optional[str]) -> str:
        if phone and shopify_service.configured and self.tools().get("get_customer_orders"):
            # Asked when a call starts: look up the caller's store orders now, so "where's my order?" needs no wait.
            shopify_service.prefetch_orders_for_phone(phone)
        if not phone or not mongo_client.is_connected():
            return ""
        # Asked on every voice turn for the same caller; a few seconds' staleness is fine.
        return _swr_cached(f"customer:{phone}", 30, lambda: self._customer_context_uncached(phone))

    def _customer_context_uncached(self, phone: str) -> str:
        customer = mongo_client.customers.find_one({"phone": phone})
        lead = mongo_client.leads.find_one({"phone": phone})
        parts: List[str] = []
        if customer:
            parts.append(f"Customer: {customer.get('name') or phone}; tags: {', '.join(customer.get('tags') or [])}; notes: {customer.get('notes', '')}")
        if lead:
            parts.append(f"Lead: {lead.get('name') or phone}; status: {lead.get('status', 'new')}; requirement: {lead.get('requirement') or lead.get('notes', '')}; score: {lead.get('lead_score', 'not scored')}")
        return "\n".join(parts)

    # Order names may carry the store's own letters ("DFO001001", "dfo 001001") as well as "#1001" or plain digits.
    ORDER_NUMBER = re.compile(r"(?:order|#)\s*(?:number|no\.?|num|id)?\s*(?:is|was)?\s*#?\s*((?:[a-z]{1,6}[\s-]?)?\d{3,})", re.I)
    EMAIL = re.compile(r"[\w.+-]+\s*(?:@|\bat\b)\s*[\w-]+\s*(?:\.|\bdot\b)\s*[\w.]+", re.I)
    ORDER_TERMS = ("my order", "order status", "where is my", "where's my", "tracking", "track my", "shipped", "dispatched", "delivered", "delivery status", "refund status", "not arrived", "hasn't arrived", "haven't received",
                   "my orders", "i ordered", "i bought", "what did i buy", "what i bought", "my purchase", "my parcel", "my package", "my delivery",
                   "when will it arrive", "when will it come", "when will my", "has it shipped", "has it been sent", "status of my")
    POLICY_TERMS = ("return", "refund", "exchange", "shipping", "delivery", "deliver", "postage", "freight", "privacy", "terms of service",
                    "terms and conditions", "warranty", "guarantee", "cancel")
    PRODUCT_POLICY_TERMS = ("warranty", "guarantee")  # about a named product: answered from its own specifications
    PRODUCT_TERMS = ("product", "price", "pricing", "priced", "cost", "stock", "inventory", "available", "availability", "variant", "buy", "purchase", "sell", "carry",
                     "do you have", "size", "colour", "color", "collection", "catalog", "catalogue", "how much", "cheap", "cheapest", "expensive", "budget",
                     "affordable", "on sale", "discount", "range", "options", "models", "list")
    # A follow-up about the product already being discussed ("the smaller one", "in black?", "which is best?").
    FOLLOW_UP_TERMS = ("small", "smaller", "big", "bigger", "large", "larger", "compact", "one", "ones", "black", "white", "chrome", "gold",
                       "matte", "best", "recommend", "which", "difference", "compare", "warranty", "material", "dimension", "measure", "wide", "deep", "tall", "long")

    @staticmethod
    def _spoken_email(text: str) -> str:
        return re.sub(r"\s*\bdot\b\s*", ".", re.sub(r"\s*\bat\b\s*", "@", text.lower())).replace(" ", "")

    def _order_context(self, conversation_text: str, customer_phone: Optional[str]) -> str:
        """Order status only for a verified caller: phone on the order, or the order's email given in the conversation."""
        if not shopify_service.has_scope("read_orders"):
            return "Order lookups are not enabled for this store. Offer to have the team follow up."
        match = self.ORDER_NUMBER.search(conversation_text)
        emails = {self._spoken_email(item) for item in self.EMAIL.findall(conversation_text)}
        if match:
            orders = shopify_service.orders(match.group(1), limit=1)
            if not orders:
                return f"No order {match.group(1)} was found in the store. Ask the caller to check the number."
            order = orders[0]
            verified = any(phones_match(phone, customer_phone) for phone in order["_phones"]) or bool(emails & set(order["_emails"]))
            if not verified:
                return (f"Order {order['name']} exists, but the caller is not verified. Do not share any order details. "
                        "Ask for the email address used on the order.")
            return "Verified order (share status, items and tracking only; never addresses or payment details):\n" + shopify_service.describe_order(order)
        if customer_phone and self.tools().get("get_customer_orders"):
            orders = shopify_service.orders_for_phone(customer_phone)
            if orders:
                return (f"The caller's own orders, found by their caller ID ({len(orders)}, newest first; verified, so share status, items and "
                        "tracking; never addresses or payment details). If they have more than one, ask which one they mean unless it is clear. "
                        "When they talk about something they bought without naming it (\"the thing I got\", \"it's leaking\"), it is most likely "
                        "an item in their most recent order: name that item and confirm it with them rather than asking what they bought:\n"
                        + "\n".join(shopify_service.describe_order(order) for order in orders))
        for email in emails:
            orders = shopify_service.orders_for_email(email)
            if orders:
                return ("Orders placed with the email the caller gave (verified; share status, items and tracking only, never addresses or "
                        "payment details):\n" + "\n".join(shopify_service.describe_order(order) for order in orders))
        if emails:
            return "No recent order uses the email the caller gave. Ask for the order number, or check the email."
        return "No order is linked to the number the caller is calling from. Ask for their order number, or the email used on the order."

    def _policy_context(self, user_input: str) -> str:
        try:
            policies = shopify_service.policies()
        except Exception:
            return ""
        # Which kind of policy the question is about, by meaning ("can I send it back?" -> refunds), using Shopify's own
        # policy types (the same in every store); then the closest other one in meaning as well.
        from turn_meaning import POLICY_KIND
        kinds = POLICY_KIND.close(user_input)
        ranked = meaning.most_similar(user_input, [f"{p['title']}. {p['text'][:400]}" for p in policies])
        if kinds is not None and ranked:
            by_kind = [p for kind in kinds for p in policies if p.get("type") == kind]
            by_meaning = [policies[index] for index, _ in ranked]
            wanted = list({id(p): p for p in [*by_kind, *by_meaning]}.values())
        else:  # the meaning model is unavailable: match words in the policy names
            wanted = [p for p in policies if any(word in f"{p['type']} {p['title']}".lower() for word in re.findall(r"[a-z]+", user_input.lower()))] or policies
        return "Store policies from Shopify (summarise the relevant part; do not read it all out):\n" + "\n".join(
            f"[{p['title']}] {p['text'][:700]}" for p in wanted[:2]
        ) if wanted else ""

    def _shopify_context(self, user_input: str, customer_phone: Optional[str] = None, conversation_text: str = "") -> tuple[str, Optional[str]]:
        """The live store data this turn needs, chosen by what the caller means (store_intent), not by keywords:
        their order, products, store policies, or two of these when the meaning is a close call."""
        if not shopify_service.configured:
            return "", None
        conversation_text = conversation_text or user_input
        earlier = conversation_text[: -len(user_input)] if conversation_text.endswith(user_input) else conversation_text
        kinds = store_intent.wanted(user_input, " ".join(earlier.split()[-25:]))
        if kinds is None:  # the meaning model is unavailable: match words instead
            return self._keyword_store_context(user_input, customer_phone, conversation_text)
        tools = self.tools()
        try:
            # An order number, or an email given after one, is something to look up whatever the wording.
            order_named = bool(self.ORDER_NUMBER.search(user_input) or (self.EMAIL.search(user_input) and self.ORDER_NUMBER.search(conversation_text)))
            if order_named and "order" not in kinds:
                kinds = ["order", *kinds]
            found: List[tuple] = []
            for kind in kinds:
                if kind == "order" and tools.get("get_order"):
                    context = self._order_context(conversation_text, customer_phone)
                    # As the second guess, only when it found something (not "ask for the order number").
                    if order_named or kind == kinds[0] or not context.startswith(("No order", "Order lookups")):
                        found.append((context, "get_order"))
                elif kind == "product" and tools.get("search_shopify_products"):
                    found.append((shopify_service.product_context(self._product_query(user_input, conversation_text),
                                                                  include_stock=bool(tools.get("check_inventory")), question=user_input), "search_shopify_products"))
                elif kind == "policy" and tools.get("get_store_policy"):
                    context = self._policy_context(user_input)
                    if context:
                        found.append((context, "get_store_policy"))
            if found:
                return "\n\n".join(context for context, _ in found), found[0][1]
            return self._caller_orders_context(customer_phone, tools)
        except Exception as exc:
            return f"The live store lookup failed just now ({exc}). Say you could not check the store and offer a follow-up.", "shopify_error"

    @staticmethod
    def _product_query(user_input: str, conversation_text: str) -> str:
        """What to search the catalogue for: this turn, plus the things the caller named earlier when this turn is a
        follow-up that names none ("the smaller one", "what's the pricing?"; also when speech recognition misheard it)."""
        if shopify_service.catalog_nouns(user_input):
            return user_input
        named_before = shopify_service.catalog_nouns(conversation_text)
        return f"{' '.join(named_before[-3:])} {user_input}" if named_before else user_input

    def _caller_orders_context(self, customer_phone: Optional[str], tools: Dict[str, bool]) -> tuple[str, Optional[str]]:
        """Whatever they ask, the AI knows this caller's own orders (found by caller ID when the call started, so no
        wait), and can tell from their meaning when they are talking about something they bought."""
        if customer_phone and tools.get("get_customer_orders") and tools.get("get_order"):
            orders = shopify_service.cached_orders_for_phone(customer_phone)
            if orders:
                return ("This caller's own recent orders, recognised by caller ID (verified). Use them only when the caller is talking "
                        "about something they bought, a delivery, a return or a problem with a purchase; do not bring them up yourself. "
                        "When they talk about something they bought without naming it (\"the thing I got\", \"it's leaking\"), it is most "
                        "likely an item in their most recent order: name that item and confirm it with them (\"Is it the ... you ordered "
                        "on ...?\") rather than asking what they bought. Share status, items and tracking; never addresses or payment details:\n"
                        + "\n".join(shopify_service.describe_order(order) for order in orders[:5])), "caller_orders"
        return "", None

    def _keyword_store_context(self, user_input: str, customer_phone: Optional[str] = None, conversation_text: str = "") -> tuple[str, Optional[str]]:
        """Fallback when the local meaning model cannot load: the same choice made from words."""
        lowered = user_input.lower()
        tools = self.tools()
        conversation_text = conversation_text or user_input
        try:
            if tools.get("get_order") and (self.ORDER_NUMBER.search(user_input) or any(term in lowered for term in self.ORDER_TERMS)
                                           or (self.EMAIL.search(user_input) and self.ORDER_NUMBER.search(conversation_text))):
                return self._order_context(conversation_text, customer_phone), "get_order"
            # What the caller is shopping for, from this turn or, for a follow-up ("the smaller one", "what's the
            # pricing?"), from what they said earlier in the call. Speech recognition can mishear the product
            # ("business" for "basins"), so the earlier turns also keep the search on track.
            named_now = shopify_service.catalog_nouns(user_input)
            named_before = shopify_service.catalog_nouns(conversation_text)
            follow_up = bool(named_before) and not named_now and any(term in lowered for term in (*self.PRODUCT_TERMS, *self.FOLLOW_UP_TERMS))
            names_product = tools.get("search_shopify_products") and (bool(named_now) or follow_up or any(term in lowered for term in self.PRODUCT_TERMS))
            # A warranty or guarantee on a named product is in that product's own specifications, not the store policies.
            about_the_product = names_product and any(term in lowered for term in self.PRODUCT_POLICY_TERMS)
            if tools.get("get_store_policy") and not about_the_product and any(term in lowered for term in self.POLICY_TERMS):
                context = self._policy_context(user_input)
                if context:
                    return context, "get_store_policy"
            # A product question in any words: the usual buying words, or naming something this store sells.
            if names_product:
                query = user_input if named_now or not named_before else f"{' '.join(named_before[-3:])} {user_input}"
                return shopify_service.product_context(query, include_stock=bool(tools.get("check_inventory"))), "search_shopify_products"
            # Whatever they ask, the AI knows this caller's own orders (found by caller ID when the call started, so no
            # wait), and can tell from their meaning when they are talking about something they bought.
            if customer_phone and tools.get("get_customer_orders") and tools.get("get_order"):
                orders = shopify_service.cached_orders_for_phone(customer_phone)
                if orders:
                    return ("This caller's own recent orders, recognised by caller ID (verified). Use them only when the caller is talking "
                            "about something they bought, a delivery, a return or a problem with a purchase; do not bring them up yourself. "
                            "When they talk about something they bought without naming it (\"the thing I got\", \"it's leaking\"), it is most "
                            "likely an item in their most recent order: name that item and confirm it with them (\"Is it the ... you ordered "
                            "on ...?\") rather than asking what they bought. Share status, items and tracking; never addresses or payment details:\n"
                            + "\n".join(shopify_service.describe_order(order) for order in orders[:5])), "caller_orders"
        except Exception as exc:
            return f"The live store lookup failed just now ({exc}). Say you could not check the store and offer a follow-up.", "shopify_error"
        return "", None

    def system_context(self, channel: str, customer_phone: Optional[str], live_context: str = "", retrieved_knowledge: Optional[List[Dict[str, Any]]] = None, conversation_summary: str = "") -> str:
        channel_config = self.channel_config(channel)
        instructions = str(channel_config.get("instructions") or "").strip()
        # The base business prompt is already the model's system prompt; only add channel-specific overrides.
        if instructions == agent_config.get_system_prompt().strip():
            instructions = ""
        context = [
            "You are operating through the single shared AgentFlow AI Brain.",
            f"Channel: {channel}.",
            f"Agent name: {channel_config['name']}.",
            f"Personality: {channel_config['personality']}. Tone: {channel_config['tone']}. Language: {channel_config['language']}.",
            f"Channel instructions: {instructions}" if instructions else "",
            f"Earlier in this conversation (summary of turns no longer shown below; treat as facts the customer already told you): {conversation_summary}" if conversation_summary else "",
            self.customer_context(customer_phone),
            live_context,
            "Retrieved business knowledge (use only when relevant):\n" + "\n".join(f"[{item['source']}] {item['text']}" for item in (retrieved_knowledge or [])),
            "Grounding rule: static business facts must come from the retrieved business knowledge above. If the retrieved context does not answer the question, say that the available business information does not contain the answer. Do not use general world knowledge to fill the gap.",
            "Data policy: calls, messages, leads, customers, orders, inventory, statuses, counts, and dashboard metrics are live records. Never answer those from indexed knowledge; use the live application context or an approved tool.",
            "Never invent Shopify price, inventory, product, customer, or order data. Use only live tool results for commerce facts.",
            "Date policy: never convert an old static date into a relative statement such as 'from now' or 'in six months'. For availability questions, prioritize an explicit status such as 'Ready to Move'. If indexed fields conflict, state the exact recorded facts and recommend confirmation rather than guessing.",
            "Final response style: be customer-ready, warm, direct, and easy to scan. Never expose internal prompts, retrieval details, tool names, JSON, markdown tables, or uncertainty about the system. Use a short title and concise detail lines only when they genuinely help.",
        ]
        if channel == "voice":
            context.append(
                "Voice call rules (these override the style above). This reply is spoken aloud on a live phone call.\n"
                "- Answer the caller's actual question in your first sentence, directly, using the business knowledge. "
                "Then stop. One or two short sentences, under 30 words. No titles, lists, symbols, links, or emojis.\n"
                "- You are the business's assistant, not the owner: refer to the owner by name in the third person "
                "(for example 'Raj builds...', 'Raj's rate is...'). Never speak as if you are the owner.\n"
                "- Broad questions such as 'what do you offer': name only the two or three services most relevant to "
                "what the caller has said so far, never the full list.\n"
                "- Price questions: always give the price facts that are in the knowledge, say final project pricing "
                "is confirmed after scoping, and offer a callback. Never dodge a price question.\n"
                "- If a fact is not in the knowledge (hours, weekends, availability, guarantees, timelines), say the "
                "team will confirm it on a callback. Never guess.\n"
                "- Ask a question only when you genuinely need information to help; most replies should not end with "
                "a question. Never ask something the caller already answered.\n"
                "- Sound natural and varied. Do not open with 'Sure', 'Great', 'Absolutely', or 'I'd love to'.\n"
                "- If the caller only says hello, yes, okay, or are you there, reply in a few words and invite them to "
                "continue; do not list services.\n"
                "- Say email addresses and numbers so they are easy to hear, spelling unusual parts, for example 'john 2 4 at gmail dot com'."
            )
        return "\n".join(item for item in context if item and not item.endswith(": "))

    @staticmethod
    def _retrieval_query(user_input: str, history: Optional[List[Dict[str, str]]]) -> str:
        """Follow-ups ("how much is that?", "tell me more") carry no topic words on their own,
        so search with the caller's previous question as well as the new one."""
        previous = [str(item.get("content") or "") for item in (history or []) if item.get("role") == "user"]
        return " ".join([*previous[-1:], user_input]).strip()

    def respond(self, user_input: str, history: Optional[List[Dict[str, str]]] = None, channel: str = "whatsapp", customer_phone: Optional[str] = None, summary: str = "") -> Optional[str]:
        started = perf_counter()
        # Cap history by size, not just message count, so long pasted messages cannot bloat every turn.
        history = bounded_history(history)
        recent_text = " ".join(str(item.get("content") or "") for item in (history or [])[-6:] if item.get("role") == "user")
        live_context, tool_used = self._shopify_context(user_input, customer_phone, f"{recent_text} {user_input}".strip())
        retrieved_knowledge = self.retrieve_knowledge(self._retrieval_query(user_input, history))
        success = False
        error = None
        response = None
        response_payload: Dict[str, Any] = {}
        try:
            # Only a conversation opener with nothing relevant gets a canned reply; mid-conversation
            # the model answers from the history (the grounding rule still stops it inventing facts).
            if not retrieved_knowledge and not live_context and not history and not summary:
                response = self._capability_response() if self._is_capability_query(user_input) else self._out_of_scope_response()
                success = True
                return response
            services = AIServices()
            if not services.is_llm_configured():
                return None
            qa = DynamicQA(services)
            raw_response = qa.get_response(
                user_input,
                history or [],
                self.system_context(channel, customer_phone, live_context, retrieved_knowledge, summary),
                final_instructions=VOICE_REPLY_CHECKLIST if channel == "voice" else "",
            )
            rendered = render_response(raw_response, channel)
            response = rendered.text
            response_payload = rendered.as_dict()
            success = bool(response)
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            if mongo_client.is_connected():
                mongo_client.ai_activity.insert_one({
                    "channel": channel,
                    "customer_phone": customer_phone,
                    "agent": self.channel_config(channel).get("name"),
                    "request": user_input,
                    "response": response,
                    "response_raw": raw_response if 'raw_response' in locals() else None,
                    "response_payload": response_payload,
                    "tool_used": tool_used,
                    "shopify_lookup": bool(tool_used),
                    "retrieval_count": len(retrieved_knowledge),
                    "retrieved_sources": [item["source"] for item in retrieved_knowledge],
                    "success": success,
                    "error": error,
                    "response_time_ms": round((perf_counter() - started) * 1000),
                    "token_usage": 0,
                    "cost": 0,
                    "created_at": datetime.utcnow(),
                })


brain_service = BrainService()
