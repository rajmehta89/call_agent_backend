import os
from datetime import datetime

from fastapi import APIRouter

from agent_config import agent_config
from brain_service import brain_service
from mongo_client import mongo_client


router = APIRouter(prefix="/api/omnichannel", tags=["omnichannel"])


def _count_documents(collection_name: str) -> int:
    if not mongo_client.is_connected():
        return 0
    collection = getattr(mongo_client, collection_name, None)
    if collection is None:
        return 0
    try:
        return collection.count_documents({})
    except Exception:
        return 0


@router.get("/summary")
async def omnichannel_summary():
    human_transfer = agent_config.get_human_transfer()
    human_agents = [agent for agent in human_transfer.get("agents", []) if agent.get("enabled")]

    twilio_ready = bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and (os.getenv("TWILIO_CALLER_ID") or os.getenv("CALLER_ID"))
    )
    meta_access_token = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN")
    meta_phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    meta_verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN")
    meta_ready = bool(meta_access_token and meta_phone_number_id)
    twilio_whatsapp_ready = bool(
        os.getenv("TWILIO_ACCOUNT_SID")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and os.getenv("TWILIO_WHATSAPP_NUMBER")
    )
    whatsapp_ready = meta_ready or twilio_whatsapp_ready
    openai_ready = bool(os.getenv("OPENAI_API_KEY"))
    brain_index = brain_service.index_status()

    return {
        "success": True,
        "data": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "channels": {
                "voice": {
                    "enabled": twilio_ready,
                    "provider": "twilio" if twilio_ready else "not_configured",
                    "number": os.getenv("TWILIO_CALLER_ID") or os.getenv("CALLER_ID") or "",
                },
                "whatsapp": {
                    "enabled": whatsapp_ready,
                    "provider": "meta" if meta_ready else "twilio" if twilio_whatsapp_ready else "not_configured",
                    "meta": {
                        "ready": meta_ready,
                        "access_token_configured": bool(meta_access_token),
                        "phone_number_id_configured": bool(meta_phone_number_id),
                        "verify_token_configured": bool(meta_verify_token),
                        "webhook_ready": bool(meta_phone_number_id and meta_verify_token),
                        "webhook_path": "/api/whatsapp/webhook",
                    },
                    "twilio": {"ready": twilio_whatsapp_ready},
                },
            },
            "ai": {
                "llm_provider": (os.getenv("LLM_PROVIDER") or "openai").lower(),
                "openai_ready": openai_ready,
                "enabled": openai_ready,
                "brain_index": brain_index,
            },
            "operations": {
                "human_handoff_enabled": bool(human_transfer.get("enabled", True)),
                "human_agents_ready": len(human_agents),
                "knowledge_base_enabled": bool(agent_config.get_knowledge_base_enabled()),
                "knowledge_index_ready": bool(brain_index.get("ready")),
                "knowledge_chunks": int(brain_index.get("documents_count", 0) or 0),
            },
            "totals": {
                "leads": _count_documents("leads"),
                "calls": _count_documents("calls"),
            },
        },
    }
