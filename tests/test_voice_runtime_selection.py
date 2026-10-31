from importlib import metadata

import voice_runtime


def test_default_selects_pipecat_for_the_standard_install(monkeypatch):
    monkeypatch.delenv("CUSTOM_VOICE_RUNTIME", raising=False)
    monkeypatch.setattr(metadata, "version", lambda name: voice_runtime.PIPECAT_VERSION)
    assert voice_runtime.runtime_status()["selected"] == "pipecat"


def test_explicit_legacy_keeps_the_rollback_path(monkeypatch):
    monkeypatch.setenv("CUSTOM_VOICE_RUNTIME", "legacy")
    monkeypatch.setattr(metadata, "version", lambda name: voice_runtime.PIPECAT_VERSION)
    assert voice_runtime.runtime_status()["selected"] == "legacy"
    assert voice_runtime.create_runtime() is None


def test_missing_default_runtime_keeps_answering_with_legacy(monkeypatch):
    monkeypatch.delenv("CUSTOM_VOICE_RUNTIME", raising=False)
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, "version", missing)
    status = voice_runtime.runtime_status()
    assert status["requested"] == "pipecat" and status["selected"] == "legacy"
    assert status["fallback_reason"]


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
