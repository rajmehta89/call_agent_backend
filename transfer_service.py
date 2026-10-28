from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from timeutil import utc_iso
from threading import RLock
from time import monotonic
from typing import Any, Dict, List, Optional


class TransferService:
    # A finished call's record is still read for a while (Retell's post-call webhooks, a handoff being completed),
    # then dropped: kept forever, one record per call (with its messages) grew for as long as the server ran.
    CLOSED_KEEP_SECONDS = 3600

    def __init__(self) -> None:
        self._lock = RLock()
        self._calls: Dict[str, Dict[str, Any]] = {}
        self._closed_at: Dict[str, float] = {}

    def _forget_finished_calls(self) -> None:
        now = monotonic()
        for session_id in [key for key, closed in self._closed_at.items() if now - closed > self.CLOSED_KEEP_SECONDS]:
            self._closed_at.pop(session_id, None)
            record = self._calls.get(session_id)
            if record and record.get("status") not in {"active", "awaiting_human", "dialing", "ringing", "accepted"}:
                self._calls.pop(session_id, None)

    def register_call(
        self,
        session_id: str,
        phone_number: str,
        lead_id: Optional[str] = None,
        direction: str = "inbound",
    ) -> Dict[str, Any]:
        with self._lock:
            self._forget_finished_calls()
            existing = self._calls.get(session_id, {})
            record = {
                "session_id": session_id,
                "phone_number": phone_number or "unknown",
                "lead_id": lead_id,
                "direction": direction,
                "status": existing.get("status", "active"),
                "transfer_requested": existing.get("transfer_requested", False),
                "transfer_reason": existing.get("transfer_reason"),
                "transfer_mode": existing.get("transfer_mode"),
                "transfer_confidence": existing.get("transfer_confidence"),
                "accepted_by": existing.get("accepted_by"),
                "handled_by": existing.get("handled_by"),
                "accepted_at": existing.get("accepted_at"),
                "resolved_at": existing.get("resolved_at"),
                "transfer_status": existing.get("transfer_status"),
                "transfer_destination": existing.get("transfer_destination"),
                "transfer_error": existing.get("transfer_error"),
                "notes": existing.get("notes"),
                "messages": existing.get("messages", []),
                "created_at": existing.get("created_at", utc_iso()),
                "updated_at": utc_iso(),
            }
            self._calls[session_id] = record
            return deepcopy(record)

    def append_message(self, session_id: str, speaker: str, content: str) -> None:
        if not session_id or not content:
            return

        with self._lock:
            if session_id not in self._calls:
                self._calls[session_id] = self.register_call(session_id, "unknown")
            record = self._calls[session_id]
            record["messages"].append(
                {
                    "speaker": speaker,
                    "content": content,
                    "timestamp": utc_iso(),
                }
            )
            record["messages"] = record["messages"][-30:]
            record["updated_at"] = utc_iso()

    def request_transfer(
        self,
        session_id: str,
        reason: str,
        mode: str,
        confidence: float,
        phone_number: Optional[str] = None,
        lead_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if session_id not in self._calls:
                self._calls[session_id] = self.register_call(session_id, phone_number or "unknown", lead_id)
            record = self._calls[session_id]
            record["phone_number"] = phone_number or record.get("phone_number") or "unknown"
            record["lead_id"] = lead_id or record.get("lead_id")
            record["transfer_requested"] = True
            record["transfer_reason"] = reason
            record["transfer_mode"] = mode
            record["transfer_confidence"] = round(confidence, 2)
            record["status"] = "awaiting_human"
            record["transfer_status"] = "requested"
            record["transfer_error"] = None
            record["updated_at"] = utc_iso()
            return deepcopy(record)

    def update_transfer(
        self,
        session_id: str,
        status: str,
        destination: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._calls.get(session_id)
            if not record:
                return None
            record["transfer_status"] = status
            record["status"] = status
            if destination:
                record["transfer_destination"] = destination
                record["handled_by"] = destination
            if error:
                record["transfer_error"] = error
            record["updated_at"] = utc_iso()
            return deepcopy(record)

    def accept_transfer(self, session_id: str, agent_name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._calls.get(session_id)
            if not record:
                return None
            record["status"] = "accepted"
            record["accepted_by"] = agent_name
            record["handled_by"] = agent_name
            record["transfer_status"] = "accepted"
            record["accepted_at"] = utc_iso()
            record["updated_at"] = utc_iso()
            return deepcopy(record)

    def resolve_transfer(self, session_id: str, notes: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._calls.get(session_id)
            if not record:
                return None
            record["status"] = "resolved"
            record["transfer_status"] = "completed"
            record["notes"] = notes or record.get("notes")
            record["resolved_at"] = utc_iso()
            record["updated_at"] = utc_iso()
            return deepcopy(record)

    def close_call(self, session_id: Optional[str]) -> None:
        if not session_id:
            return
        with self._lock:
            record = self._calls.get(session_id)
            if not record:
                return
            if record.get("status") == "active":
                record["status"] = "completed"
            record["updated_at"] = utc_iso()
            self._closed_at[session_id] = monotonic()

    def get_active_handoffs(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [
                deepcopy(record)
                for record in self._calls.values()
                if record.get("transfer_requested") and record.get("status") in {"awaiting_human", "dialing", "ringing", "accepted"}
            ]
        return sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)

    def get_call(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._calls.get(session_id)
            return deepcopy(record) if record else None


transfer_service = TransferService()
