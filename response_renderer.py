"""Turn model output into clean, channel-appropriate customer responses."""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass
class RenderedResponse:
    message: str
    title: str = ""
    details: List[str] = field(default_factory=list)
    actions: List[Dict[str, str]] = field(default_factory=list)
    text: str = ""
    channel: str = "whatsapp"
    style: str = "plain"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"```(?:json|text)?", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")
    text = re.sub(r"\[\s*source[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*[-*•]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^[\s]*\u2022\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def _parse_payload(raw: str) -> tuple[Dict[str, Any], bool]:
    cleaned = raw.strip()
    candidates = [cleaned]
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, dict) and value.get("message"):
                return value, True
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return {"message": raw}, False


def _details(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]


def _actions(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict) and item.get("label"):
            result.append({"label": _clean_text(item["label"]), "action": str(item.get("action") or "")})
        elif isinstance(item, str) and item.strip():
            result.append({"label": _clean_text(item), "action": ""})
    return result[:4]


def render_response(raw: Any, channel: str = "whatsapp") -> RenderedResponse:
    """Normalize optional structured model output and render it for a channel."""
    raw_text = str(raw or "").strip()
    payload, structured = _parse_payload(raw_text)
    message = _clean_text(payload.get("message")) or "I'm here to help."
    title = _clean_text(payload.get("title"))
    details = _details(payload.get("details"))
    actions = _actions(payload.get("actions"))
    channel_name = "voice" if channel == "voice" else "whatsapp"

    if channel_name == "voice":
        parts = [title, message, ". ".join(details)]
        if actions:
            labels = ", ".join(item["label"] for item in actions)
            parts.append(f"You can say {labels} if you would like to continue")
        text = ". ".join(part.strip(" .") for part in parts if part.strip()).strip() + "."
        text = re.sub(r"\s+", " ", text)
    else:
        blocks = [part for part in (title, message) if part]
        if details:
            blocks.append("\n".join(f"• {item}" for item in details))
        if actions:
            blocks.append("Reply with: " + " · ".join(item["label"] for item in actions))
        text = "\n\n".join(blocks).strip()

    return RenderedResponse(
        message=message,
        title=title,
        details=details,
        actions=actions,
        text=text,
        channel=channel_name,
        style="structured" if structured else "clean_text",
    )
