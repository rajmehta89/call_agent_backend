from importlib import metadata

import voice_runtime


def test_default_keeps_existing_call_path(monkeypatch):
    monkeypatch.delenv("CUSTOM_VOICE_RUNTIME", raising=False)
    assert voice_runtime.runtime_status()["selected"] == "legacy"
    assert voice_runtime.create_runtime() is None


def test_missing_or_unsupported_optional_version_falls_back(monkeypatch):
    monkeypatch.setenv("CUSTOM_VOICE_RUNTIME", "pipecat")
    monkeypatch.setattr(metadata, "version", lambda name: "0.0.100")
    assert voice_runtime.runtime_status()["selected"] == "legacy"
    assert "requirements-pipecat" in voice_runtime.runtime_status()["fallback_reason"]


def test_installed_supported_runtime_can_be_selected(monkeypatch):
    monkeypatch.setenv("CUSTOM_VOICE_RUNTIME", "pipecat")
    monkeypatch.setattr(metadata, "version", lambda name: voice_runtime.PIPECAT_VERSION)
    assert voice_runtime.runtime_status()["selected"] == "pipecat"


def test_unknown_runtime_has_an_explicit_fallback_reason(monkeypatch):
    monkeypatch.setenv("CUSTOM_VOICE_RUNTIME", "invalid")
    assert voice_runtime.runtime_status()["selected"] == "legacy"
    assert "Unknown" in voice_runtime.runtime_status()["fallback_reason"]
