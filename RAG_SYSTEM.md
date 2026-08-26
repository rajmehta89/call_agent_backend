# AgentFlow shared brain

The shared brain uses two data lanes:

- **Indexed knowledge:** business settings, PDFs, scraped webpages, and a Shopify product-catalog snapshot.
- **Live context/tools:** calls, messages, leads, customers, orders, inventory, statuses, counts, and dashboard metrics.

Live records are never copied into static knowledge chunks. Shopify prices, inventory, orders, and customer data are resolved through live tools at answer time. The catalog snapshot is only for broad product discovery and is refreshed explicitly.

## Ingestion

- `POST /api/platform/brain/upload` accepts PDF files up to 25 MB.
- `POST /api/platform/brain/scrape` accepts an `http` or `https` webpage URL.
- `POST /api/platform/brain/shopify` indexes the current Shopify product catalog.
- `PUT /api/platform/brain` updates the governed business-settings source.

Each source is tracked in `brain_sources`; chunks and metadata are stored in `brain_documents`. LlamaIndex `Document`, `IngestionPipeline`, and `SentenceSplitter` create sentence-aware chunks. It is an ingestion utility here, not the database or the answer model. Embeddings are provider-configurable: the production primary is OpenRouter's `openai/text-embedding-3-small` at 1536 dimensions; the fallback is Gemini Embedding 2 at 1536 dimensions.

## Retrieval

The query path is:

1. Attempt MongoDB Atlas `$vectorSearch` using `RAG_ATLAS_VECTOR_INDEX_NAME`.
2. Fall back to local hybrid vector/lexical scoring when Atlas Search is not provisioned.
3. Fall back to lexical scoring when embeddings are unavailable.
4. Add customer/lead context and live Shopify results only for the current request.

The final model answer passes through `response_renderer.py`. It accepts normal text or an optional structured payload with `title`, `details`, and `actions`, then renders readable WhatsApp text or natural voice speech. Internal prompts, tool names, JSON, retrieval scores, and markdown tables are never sent to the customer. Generation is policy-based: factual RAG answers use temperature 0.2, tool/classification decisions use 0.0, and ordinary conversation uses 0.35. `top_p` remains 1.0 so temperature is the only sampling control being tuned.

For Atlas Vector Search, create a `vector_index` on `brain_documents.embedding` with 1536 dimensions and cosine similarity. The application remains usable while that index or an embedding credential is unavailable.

## Environment

```text
RAG_EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_PROVIDER=openrouter
OPENROUTER_EMBEDDING_MODEL=openai/text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
RAG_ATLAS_VECTOR_SEARCH=true
RAG_ATLAS_VECTOR_INDEX_NAME=vector_index
RAG_MIN_RELEVANCE=0.2
RAG_VECTOR_ONLY_MIN=0.4
```
