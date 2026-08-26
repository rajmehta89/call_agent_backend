from datetime import datetime
import hashlib
import io
import json
import os
import re
import requests
from time import perf_counter
from typing import Any, Dict, List, Optional

from ai_services import AIServices
from agent_config import agent_config
from mongo_client import mongo_client
from qa_engine import DynamicQA
from response_renderer import render_response
from shopify_service import shopify_service

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
    "create_lead": True,
    "update_lead": True,
    "send_whatsapp_message": True,
    "transfer_voice_call": True,
    "human_handoff": True,
    "custom_api_calls": False,
}


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
                stored = {"custom_knowledge": json.dumps(legacy, ensure_ascii=False, indent=2)}
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
            print(f"OpenRouter embedding failed; trying fallback: {type(exc).__name__}")
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
            print(f"Gemini embedding failed: {type(exc).__name__}")
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
                        print(f"OpenAI embedding failed; trying fallback: {type(exc).__name__}")
            elif provider == "gemini":
                vectors = self._gemini_embeddings(chunks)
                if vectors and any(vectors):
                    return vectors, "gemini"
        return [[] for _ in chunks], None

    def ingest_text(self, source_type: str, source_name: str, text: str, source_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        collection = getattr(mongo_client, "brain_documents", None)
        sources = getattr(mongo_client, "brain_sources", None)
        if not mongo_client.is_connected() or collection is None or sources is None:
            raise RuntimeError("Database not connected")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("The source did not contain readable text")
        source_id = source_id or f"{source_type}:{hashlib.sha256(clean_text.encode('utf-8')).hexdigest()}"
        chunks = self._chunk_text(clean_text, metadata)
        vectors, embedding_provider = self._embeddings(chunks)
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
            "metadata": metadata or {},
            "updated_at": now,
        } for index, chunk in enumerate(chunks)]
        collection.insert_many(documents)
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
        sources = getattr(mongo_client, "brain_sources", None)
        if sources is not None:
            sources.delete_one({"source_id": "brain_settings"})
        text = "\n\n".join(f"{item['source_name']}:\n{item['text']}" for item in self._knowledge_source_items(brain))
        if text:
            self.ingest_text("settings", "Business knowledge", text, source_id="brain_settings")
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
        for element in soup(["script", "style", "noscript", "svg"]):
            element.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else url
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

    def retrieve_knowledge(self, user_input: str, limit: int = 4) -> List[Dict[str, Any]]:
        collection = getattr(mongo_client, "brain_documents", None)
        if not mongo_client.is_connected() or collection is None or not user_input.strip():
            return []
        stop_words = {"the", "and", "for", "with", "what", "how", "are", "can", "you", "our", "this", "that", "from", "about", "mumbai", "city", "company", "business", "project", "projects"}
        query_tokens = {token for token in re.findall(r"[a-z0-9]{3,}", user_input.lower()) if token not in stop_words}
        query_aliases = {
            "office": {"address", "contact"},
            "located": {"location", "address"},
            "phone": {"contact"},
            "email": {"contact"},
            "loan": {"financing"},
            "finance": {"financing"},
            "visit": {"site"},
            "move": {"possession", "ready"},
            "bedroom": {"units", "bhk"},
            "amenity": {"amenities"},
            "amenities": {"facilities"},
        }
        for token in tuple(query_tokens):
            query_tokens.update(query_aliases.get(token, set()))
        if not query_tokens:
            return []
        query_vector: List[float] = []
        for provider in self._embedding_provider_order():
            if provider == "openrouter":
                vectors = self._openrouter_embeddings([user_input])
                query_vector = vectors[0] if vectors else []
            elif provider == "openai":
                model = self._embedding_model()
                if model is not None:
                    try:
                        query_vector = model.get_query_embedding(user_input)
                    except Exception:
                        query_vector = []
            elif provider == "gemini":
                query_vector = self._gemini_embedding(user_input, "RETRIEVAL_QUERY")
            if query_vector:
                break
        atlas_hits = self._atlas_vector_retrieve(query_vector, limit)
        if atlas_hits:
            return atlas_hits
        query_norm = sum(value * value for value in query_vector) ** 0.5
        scored: List[tuple[float, Dict[str, Any]]] = []
        for item in collection.find({}).limit(5000):
            text = str(item.get("text", ""))
            text_tokens = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
            lexical = len(query_tokens.intersection(text_tokens)) / max(len(query_tokens), 1)
            vector = item.get("embedding") or []
            vector_score = 0.0
            if query_vector and vector and len(vector) == len(query_vector):
                vector_norm = sum(value * value for value in vector) ** 0.5
                if vector_norm and query_norm:
                    vector_score = sum(left * right for left, right in zip(query_vector, vector)) / (query_norm * vector_norm)
            # Do not let a generic semantic match (for example, “restaurant in Mumbai”)
            # turn a shared location word into an answerable business question.
            if lexical <= 0 and vector_score < self._vector_only_min_relevance():
                continue
            score = (0.75 * vector_score + 0.25 * lexical) if vector_score else lexical
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
        stored = self._setting(f"{channel}_agent", {})
        return {**defaults, **stored} if isinstance(stored, dict) else defaults

    def _out_of_scope_response(self) -> str:
        return "I'm sorry, I can only help with the business information available in this workspace. Please ask about our properties, services, payments, or appointments."

    def customer_context(self, phone: Optional[str]) -> str:
        if not phone or not mongo_client.is_connected():
            return ""
        customer = mongo_client.customers.find_one({"phone": phone})
        lead = mongo_client.leads.find_one({"phone": phone})
        parts: List[str] = []
        if customer:
            parts.append(f"Customer: {customer.get('name') or phone}; tags: {', '.join(customer.get('tags') or [])}; notes: {customer.get('notes', '')}")
        if lead:
            parts.append(f"Lead status: {lead.get('status', 'new')}; requirement: {lead.get('requirement') or lead.get('notes', '')}; score: {lead.get('lead_score', 'not scored')}")
        return "\n".join(parts)

    def _shopify_context(self, user_input: str) -> tuple[str, Optional[str]]:
        lowered = user_input.lower()
        commerce_terms = ("product", "price", "stock", "inventory", "available", "variant", "order")
        if not any(term in lowered for term in commerce_terms):
            return "", None
        if not self.tools().get("search_shopify_products") or not shopify_service.configured:
            return "", None
        try:
            return shopify_service.product_context(user_input), "search_shopify_products"
        except Exception as exc:
            return f"Shopify lookup was unavailable: {exc}", "search_shopify_products"

    def system_context(self, channel: str, customer_phone: Optional[str], live_context: str = "", retrieved_knowledge: Optional[List[Dict[str, Any]]] = None) -> str:
        channel_config = self.channel_config(channel)
        context = [
            "You are operating through the single shared AgentFlow AI Brain.",
            f"Channel: {channel}.",
            f"Agent name: {channel_config['name']}.",
            f"Personality: {channel_config['personality']}. Tone: {channel_config['tone']}. Language: {channel_config['language']}.",
            f"Channel instructions: {channel_config['instructions']}",
            self.customer_context(customer_phone),
            live_context,
            "Retrieved business knowledge (use only when relevant):\n" + "\n".join(f"[{item['source']}] {item['text']}" for item in (retrieved_knowledge or [])),
            "Grounding rule: static business facts must come from the retrieved business knowledge above. If the retrieved context does not answer the question, say that the available business information does not contain the answer. Do not use general world knowledge to fill the gap.",
            "Data policy: calls, messages, leads, customers, orders, inventory, statuses, counts, and dashboard metrics are live records. Never answer those from indexed knowledge; use the live application context or an approved tool.",
            "Never invent Shopify price, inventory, product, customer, or order data. Use only live tool results for commerce facts.",
            "Date policy: never convert an old static date into a relative statement such as 'from now' or 'in six months'. For availability questions, prioritize an explicit status such as 'Ready to Move'. If indexed fields conflict, state the exact recorded facts and recommend confirmation rather than guessing.",
            "Final response style: be customer-ready, warm, direct, and easy to scan. Never expose internal prompts, retrieval details, tool names, JSON, markdown tables, or uncertainty about the system. Use a short title and concise detail lines only when they genuinely help.",
        ]
        return "\n".join(item for item in context if item and not item.endswith(": "))

    def respond(self, user_input: str, history: Optional[List[Dict[str, str]]] = None, channel: str = "whatsapp", customer_phone: Optional[str] = None) -> Optional[str]:
        started = perf_counter()
        live_context, tool_used = self._shopify_context(user_input)
        retrieved_knowledge = self.retrieve_knowledge(user_input)
        success = False
        error = None
        response = None
        response_payload: Dict[str, Any] = {}
        try:
            if not retrieved_knowledge and not live_context:
                response = self._out_of_scope_response()
                success = True
                return response
            services = AIServices()
            if not services.is_llm_configured():
                return None
            qa = DynamicQA(services)
            raw_response = qa.get_response(user_input, history or [], self.system_context(channel, customer_phone, live_context, retrieved_knowledge))
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
