"""Select the speech runtime without importing its audio stack before call authentication."""

from importlib import metadata
import os

PIPECAT_VERSION = "1.15.0"


def runtime_status():
    requested = os.getenv("CUSTOM_VOICE_RUNTIME", "pipecat").strip().lower() or "pipecat"
    try:
        installed = metadata.version("pipecat-ai")
    except metadata.PackageNotFoundError:
        installed = None
    available = installed == PIPECAT_VERSION
    reason = ""
    if requested not in {"legacy", "pipecat"}:
        reason = "Unknown CUSTOM_VOICE_RUNTIME; using legacy"
    elif requested == "pipecat" and not available:
        reason = f"Install requirements-pipecat.txt (requires pipecat-ai {PIPECAT_VERSION})"
    return {"requested": requested, "selected": "pipecat" if requested == "pipecat" and available else "legacy",
            "pipecat_version": installed, "fallback_reason": reason}


def create_runtime():
    """Called off the event loop, only after the Twilio stream token is verified."""
    if runtime_status()["selected"] != "pipecat":
        return None
    from pipecat_voice import PipecatVoiceRuntime
    return PipecatVoiceRuntime()


def warm_runtime():
    if runtime_status()["selected"] == "pipecat":
        # Import audio/service classes before readiness; no account data or provider requests.
        import pipecat_voice  # noqa: F401
