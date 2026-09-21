"""Approval and sending windows for outbound Gmail automations."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from zoneinfo import ZoneInfo

from mongo_client import mongo_client


DEFAULT_EMAIL_POLICY: Dict[str, Any] = {
    "approval_required": True,
    "timezone": "Asia/Kolkata",
    "approval_start_hour": 9,
    "approval_end_hour": 18,
    "send_start_hour": 9,
    "send_end_hour": 18,
    "approval_start_time": "09:00",
    "approval_end_time": "18:00",
    "send_start_time": "09:00",
    "send_end_time": "18:00",
    "send_days": [0, 1, 2, 3, 4],
}


def get_email_policy() -> Dict[str, Any]:
    if mongo_client.is_connected():
        stored = mongo_client.platform_settings.find_one({"key": "email_policy"})
        if stored and isinstance(stored.get("value"), dict):
            return {**DEFAULT_EMAIL_POLICY, **stored["value"]}
    return DEFAULT_EMAIL_POLICY.copy()


def save_email_policy(value: Dict[str, Any]) -> Dict[str, Any]:
    policy = {**DEFAULT_EMAIL_POLICY, **value}
    policy["approval_required"] = bool(policy["approval_required"])
    policy["send_days"] = [int(day) for day in policy.get("send_days", DEFAULT_EMAIL_POLICY["send_days"]) if int(day) in range(7)]
    for prefix in ("approval_start", "approval_end", "send_start", "send_end"):
        raw_time = str(policy.get(f"{prefix}_time") or "").strip()
        if len(raw_time) != 5 or raw_time[2] != ":":
            raw_time = f"{int(policy.get(f'{prefix}_hour', 9)):02d}:00"
        try:
            hour, minute = [int(part) for part in raw_time.split(":")]
            # 24:00 is a valid end-of-day sentinel for an always-open window.
            # Start times remain limited to 00:00-23:59.
            end_of_day = prefix.endswith("_end") and hour == 24 and minute == 0
            if (hour not in range(24) and not end_of_day) or minute not in range(60):
                raise ValueError
            policy[f"{prefix}_time"] = f"{hour:02d}:{minute:02d}"
            policy[f"{prefix}_hour"] = hour
        except (TypeError, ValueError):
            policy[f"{prefix}_time"] = DEFAULT_EMAIL_POLICY[f"{prefix}_time"]
            policy[f"{prefix}_hour"] = int(policy[f"{prefix}_time"].split(":")[0])
    if mongo_client.is_connected():
        mongo_client.platform_settings.update_one(
            {"key": "email_policy"},
            {"$set": {"value": policy, "updated_at": datetime.utcnow()}, "$setOnInsert": {"created_at": datetime.utcnow()}},
            upsert=True,
        )
    return policy


def _within_hours(policy: Dict[str, Any], start_key: str, end_key: str) -> bool:
    timezone_name = str(policy.get("timezone") or "Asia/Kolkata")
    try:
        current = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        # Windows Python installations may not ship the IANA tzdata package.
        # IST is fixed at UTC+05:30, so retain correct approval behavior without
        # requiring a dependency or silently falling back to the wrong timezone.
        if timezone_name in {"Asia/Kolkata", "Asia/Calcutta", "IST"}:
            current = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        else:
            current = datetime.now().astimezone()
    if current.weekday() not in policy.get("send_days", DEFAULT_EMAIL_POLICY["send_days"]):
        return False
    start_time = str(policy.get(f"{start_key.replace('_hour', '_time')}") or f"{int(policy.get(start_key, 9)):02d}:00")
    end_time = str(policy.get(f"{end_key.replace('_hour', '_time')}") or f"{int(policy.get(end_key, 18)):02d}:00")
    start_hour, start_minute = [int(part) for part in start_time.split(":")]
    end_hour, end_minute = [int(part) for part in end_time.split(":")]
    current_minutes = current.hour * 60 + current.minute
    return start_hour * 60 + start_minute <= current_minutes < end_hour * 60 + end_minute


def approval_window_open(policy: Dict[str, Any] | None = None) -> bool:
    policy = policy or get_email_policy()
    return _within_hours(policy, "approval_start_hour", "approval_end_hour")


def send_window_open(policy: Dict[str, Any] | None = None) -> bool:
    policy = policy or get_email_policy()
    return _within_hours(policy, "send_start_hour", "send_end_hour")
