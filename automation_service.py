"""Small event-driven automation runner for the supported workspace actions."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from bson import ObjectId

from mongo_client import mongo_client


class AutomationService:
    SUPPORTED_EVENTS = {"whatsapp_message", "customer_reply", "voice_call", "missed_call", "new_lead", "lead_qualified"}

    def run(self, event: str, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if event not in self.SUPPORTED_EVENTS or not mongo_client.is_connected():
            return []
        context = context or {}
        results: List[Dict[str, Any]] = []
        for automation in mongo_client.automations.find({"enabled": True}):
            if not self._matches(str(automation.get("trigger", "")), event, context) or not self._conditions_match(automation.get("conditions") or [], context):
                continue
            results.append(self._run_one(automation, event, context))
        return results

    def _matches(self, trigger: str, event: str, context: Dict[str, Any]) -> bool:
        normalized = trigger.strip().lower()
        if normalized == "whatsapp message":
            return event == "whatsapp_message"
        if normalized == "customer reply":
            return event == "customer_reply"
        if normalized == "voice call":
            return event == "voice_call"
        if normalized == "missed call":
            return event == "missed_call"
        if normalized == "new lead":
            return event == "new_lead"
        if normalized == "lead qualified":
            return event == "lead_qualified" or context.get("status") in {"qualified", "hot", "converted"}
        return False

    def _conditions_match(self, conditions: List[Dict[str, Any]], context: Dict[str, Any]) -> bool:
        """Apply simple all-of conditions without allowing arbitrary code execution."""
        for condition in conditions:
            field = str(condition.get("field", "")).strip()
            operator = str(condition.get("operator", "equals")).strip().lower()
            expected = str(condition.get("value", "")).strip().lower()
            if not field:
                continue
            actual_value = context.get(field)
            actual = "" if actual_value is None else str(actual_value).strip().lower()
            if operator in {"equals", "is"} and actual != expected:
                return False
            if operator in {"not equals", "is not"} and actual == expected:
                return False
            if operator == "contains" and expected not in actual:
                return False
            if operator == "exists" and (actual_value is None or actual == ""):
                return False
        return True

    def _run_one(self, automation: Dict[str, Any], event: str, context: Dict[str, Any]) -> Dict[str, Any]:
        automation_id = automation.get("_id")
        started = datetime.utcnow()
        context = {**context, "event": event}
        action_results: List[Dict[str, Any]] = []
        try:
            for step in automation.get("steps") or []:
                action_results.append(self._execute_action(str(step.get("type", "")), step, context, event))
            now = datetime.utcnow()
            mongo_client.automations.update_one(
                {"_id": automation_id},
                {"$inc": {"runs": 1}, "$set": {"last_run_at": now, "last_run_status": "success", "last_run_error": None, "updated_at": now}},
            )
            self._log_run(automation_id, automation.get("name", "Automation"), event, "success", action_results, started, now)
            return {"automation_id": str(automation_id), "status": "success", "actions": action_results}
        except Exception as exc:
            now = datetime.utcnow()
            mongo_client.automations.update_one(
                {"_id": automation_id},
                {"$inc": {"errors": 1}, "$set": {"last_run_at": now, "last_run_status": "error", "last_run_error": str(exc), "updated_at": now}},
            )
            self._log_run(automation_id, automation.get("name", "Automation"), event, "error", action_results, started, now, str(exc))
            return {"automation_id": str(automation_id), "status": "error", "error": str(exc)}

    def _execute_action(self, action: str, step: Dict[str, Any], context: Dict[str, Any], event: str) -> Dict[str, Any]:
        normalized = action.strip().lower()
        if normalized in {"create lead", "create_lead"}:
            return self._create_lead(context)
        if normalized in {"update lead", "update_lead"}:
            return self._update_lead(context, step)
        if normalized in {"send whatsapp", "send whatsapp message", "send_whatsapp_message"}:
            return self._send_whatsapp(context, step, event)
        if normalized in {"human handoff", "human_handoff"}:
            return self._handoff(context)
        raise ValueError(f"Unsupported automation action: {action}")

    def _find_lead(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        phone = str(context.get("customer_phone") or context.get("phone") or "").strip()
        if phone:
            lead = mongo_client.leads.find_one({"phone": phone})
            if lead:
                return lead
        lead_id = context.get("lead_id")
        if lead_id and ObjectId.is_valid(str(lead_id)):
            return mongo_client.leads.find_one({"_id": ObjectId(str(lead_id))})
        return None

    def _create_lead(self, context: Dict[str, Any]) -> Dict[str, Any]:
        phone = str(context.get("customer_phone") or context.get("phone") or "").strip()
        if not phone:
            return {"action": "create_lead", "status": "skipped", "reason": "No customer phone"}
        existing = mongo_client.leads.find_one({"phone": phone})
        if existing:
            return {"action": "create_lead", "status": "existing", "lead_id": str(existing["_id"])}
        now = datetime.utcnow()
        row = {"name": context.get("customer_name") or context.get("name") or phone, "phone": phone, "email": context.get("email", ""), "company": context.get("company", ""), "notes": context.get("message", ""), "status": "new", "source": context.get("source", "automation"), "call_attempts": 0, "last_call": None, "created_at": now, "updated_at": now}
        result = mongo_client.leads.insert_one(row)
        return {"action": "create_lead", "status": "created", "lead_id": str(result.inserted_id)}

    def _update_lead(self, context: Dict[str, Any], step: Dict[str, Any]) -> Dict[str, Any]:
        lead = self._find_lead(context)
        if not lead:
            return {"action": "update_lead", "status": "skipped", "reason": "Lead not found"}
        updates = {"updated_at": datetime.utcnow(), "last_automation_event": context.get("event", "automation")}
        if step.get("status") or context.get("status"):
            updates["status"] = step.get("status") or context["status"]
        if step.get("note") or context.get("message"):
            updates["last_automation_note"] = step.get("note") or context["message"]
        mongo_client.leads.update_one({"_id": lead["_id"]}, {"$set": updates})
        return {"action": "update_lead", "status": "updated", "lead_id": str(lead["_id"])}

    def _send_whatsapp(self, context: Dict[str, Any], step: Dict[str, Any], event: str) -> Dict[str, Any]:
        phone = str(context.get("customer_phone") or context.get("phone") or "").strip()
        if not phone:
            return {"action": "send_whatsapp", "status": "skipped", "reason": "No customer phone"}
        message = str(step.get("message") or context.get("automation_message") or self._default_message(event))
        from routers.whatsapp_api import _send_message, _store_message
        provider_result = _send_message(phone, message)
        stored = _store_message(phone, message, "outbound", provider_result["provider"], provider_result.get("provider_message_id"), context.get("conversation_id"), context.get("customer_name"), provider_result)
        return {"action": "send_whatsapp", "status": "sent", "message_id": str(stored.get("_id"))}

    def _default_message(self, event: str) -> str:
        if event == "missed_call":
            return "We missed your call. Reply here and our team will help you shortly."
        if event == "new_lead":
            return "Thanks for reaching out. Our team will follow up shortly."
        return "Thanks for your message. Our team will get back to you shortly."

    def _handoff(self, context: Dict[str, Any]) -> Dict[str, Any]:
        phone = str(context.get("customer_phone") or context.get("phone") or "").strip()
        assigned_to = context.get("assigned_to") or "Human team"
        if context.get("conversation_id"):
            try:
                mongo_client.whatsapp_conversations.update_one({"_id": ObjectId(str(context["conversation_id"]))}, {"$set": {"ai_enabled": False, "assigned_to": assigned_to, "updated_at": datetime.utcnow()}})
            except Exception:
                pass
        if context.get("call_id") and ObjectId.is_valid(str(context["call_id"])):
            mongo_client.calls.update_one({"_id": ObjectId(str(context["call_id"]))}, {"$set": {"transfer_requested": True, "transfer_status": "requested", "handled_by": assigned_to, "updated_at": datetime.utcnow()}})
        from notification_service import notify_human_assignment
        notify_human_assignment(
            channel=str(context.get("channel") or "voice"),
            customer=phone,
            recipient=assigned_to,
            conversation_id=str(context.get("conversation_id")) if context.get("conversation_id") else None,
            call_session_id=str(context.get("call_session_id")) if context.get("call_session_id") else None,
            message=f"Human assistance requested for {phone or 'a customer'}.",
        )
        return {"action": "human_handoff", "status": "requested", "assigned_to": assigned_to}

    def _log_run(self, automation_id: Any, name: str, event: str, status: str, actions: List[Dict[str, Any]], started: datetime, finished: datetime, error: Optional[str] = None) -> None:
        if not hasattr(mongo_client, "automation_runs"):
            return
        mongo_client.automation_runs.insert_one({"automation_id": str(automation_id), "automation_name": name, "event": event, "status": status, "actions": actions, "error": error, "started_at": started, "finished_at": finished, "created_at": finished})


automation_service = AutomationService()
