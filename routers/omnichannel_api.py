import os
from datetime import datetime

from fastapi import APIRouter

from agent_config import agent_config
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
    whatsapp_ready = bool(
        os.getenv("WHATSAPP_ACCESS_TOKEN")
        or os.getenv("WHATSAPP_TOKEN")
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        or os.getenv("TWILIO_WHATSAPP_NUMBER")
    )
    openai_ready = bool(os.getenv("OPENAI_API_KEY"))

    return {
        "success": True,
        "data": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "channels": {
                "voice": {
                    "enabled": twilio_ready,
                    "provider": "twilio" if twilio_ready else "not_configured",
                },
                "whatsapp": {
                    "enabled": whatsapp_ready,
                    "provider": "configured" if whatsapp_ready else "not_configured",
                },
            },
            "ai": {
                "llm_provider": (os.getenv("LLM_PROVIDER") or "openai").lower(),
                "openai_ready": openai_ready,
            },
            "operations": {
                "human_handoff_enabled": bool(human_transfer.get("enabled", True)),
                "human_agents_ready": len(human_agents),
                "knowledge_base_enabled": bool(agent_config.get_knowledge_base_enabled()),
            },
            "totals": {
                "leads": _count_documents("leads"),
                "calls": _count_documents("calls"),
            },
        },
    }
