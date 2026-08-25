from datetime import datetime
from time import perf_counter
from typing import Any, Dict, List, Optional

from ai_services import AIServices
from agent_config import agent_config
from mongo_client import mongo_client
from qa_engine import DynamicQA
from shopify_service import shopify_service


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
        }

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
            "knowledge_enabled": True,
            "shopify_access": True,
        }
        stored = self._setting(f"{channel}_agent", {})
        return {**defaults, **stored} if isinstance(stored, dict) else defaults

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

    def system_context(self, channel: str, customer_phone: Optional[str], live_context: str = "") -> str:
        brain = self.brain_config()
        channel_config = self.channel_config(channel)
        context = [
            "You are operating through the single shared AgentFlow AI Brain.",
            f"Channel: {channel}.",
            f"Agent name: {channel_config['name']}.",
            f"Personality: {channel_config['personality']}. Tone: {channel_config['tone']}. Language: {channel_config['language']}.",
            f"Channel instructions: {channel_config['instructions']}",
            f"Business: {brain['business_description'] or brain['company_information']}",
            f"Working hours: {brain['working_hours']}",
            f"Services: {brain['services']}",
            f"FAQs: {brain['faqs']}",
            f"Policies: {brain['policies']}",
            f"Custom knowledge: {brain['custom_knowledge']}",
            self.customer_context(customer_phone),
            live_context,
            "Never invent Shopify price, inventory, product, customer, or order data. Use only live tool results for commerce facts.",
        ]
        return "\n".join(item for item in context if item and not item.endswith(": "))

    def respond(self, user_input: str, history: Optional[List[Dict[str, str]]] = None, channel: str = "whatsapp", customer_phone: Optional[str] = None) -> Optional[str]:
        started = perf_counter()
        live_context, tool_used = self._shopify_context(user_input)
        success = False
        error = None
        response = None
        try:
            services = AIServices()
            if not services.is_llm_configured():
                return None
            qa = DynamicQA(services)
            response = qa.get_response(user_input, history or [], self.system_context(channel, customer_phone, live_context))
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
                    "tool_used": tool_used,
                    "shopify_lookup": bool(tool_used),
                    "success": success,
                    "error": error,
                    "response_time_ms": round((perf_counter() - started) * 1000),
                    "token_usage": 0,
                    "cost": 0,
                    "created_at": datetime.utcnow(),
                })


brain_service = BrainService()
