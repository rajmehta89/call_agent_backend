import requests
import base64
import numpy as np
import time
import os
from typing import Optional
from groq import Groq
from openai import OpenAI
from config import Config
import logging

from logging_setup import timed

logger = logging.getLogger(__name__)


class _CompletionAdapter:
    def __init__(self, parent):
        self.parent = parent

    def create(self, messages, model=None, max_tokens=80, temperature=None, top_p=None, stream=False):
        text = self.parent.chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
        )

        class _Message:
            def __init__(self, content):
                self.content = content

        class _Choice:
            def __init__(self, content):
                self.message = _Message(content)

        class _Response:
            def __init__(self, content):
                self.choices = [_Choice(content)]

        return _Response(text)


class _ChatAdapter:
    def __init__(self, parent):
        self.completions = _CompletionAdapter(parent)


class _ClientAdapter:
    def __init__(self, parent):
        self.chat = _ChatAdapter(parent)

class AIServices:
    def __init__(self):
        self.config = Config()
        self.provider = self._resolve_provider()
        self.groq_client = None
        self.openai_client = None

        logger.debug(f"Initializing AIServices...")
        logger.debug(f"LLM provider selected: {self.provider}")
        logger.debug(f"GROQ_API_KEY present: {'Yes' if self.config.GROQ_API_KEY else 'No'}")
        logger.debug(f"OPENAI_API_KEY present: {'Yes' if self.config.OPENAI_API_KEY else 'No'}")
        logger.debug(f"OPENROUTER_API_KEY present: {'Yes' if self.config.OPENROUTER_API_KEY else 'No'}")

        if self.provider == "groq" and self.config.GROQ_API_KEY:
            try:
                self.groq_client = Groq(api_key=self.config.GROQ_API_KEY)
                logger.debug("Groq client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Groq client: {e}")

        if self.provider in {"openai", "openrouter"} and ((self.provider == "openai" and self.config.OPENAI_API_KEY) or (self.provider == "openrouter" and self.config.OPENROUTER_API_KEY)):
            try:
                self.openai_client = OpenAI(
                    api_key=self.config.OPENAI_API_KEY if self.provider == "openai" else self.config.OPENROUTER_API_KEY,
                    base_url="https://openrouter.ai/api/v1" if self.provider == "openrouter" else None,
                )
                self.groq_client = _ClientAdapter(self)
                if not self.config.GROQ_API_KEY:
                    self.config.GROQ_API_KEY = "openrouter-proxy" if self.provider == "openrouter" else "openai-proxy"
                logger.debug(f"{self.provider.title()} client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize OpenAI client: {e}")

    def _resolve_provider(self) -> str:
        if self.config.LLM_PROVIDER in {"groq", "openai", "openrouter"}:
            return self.config.LLM_PROVIDER
        if self.config.OPENROUTER_API_KEY:
            return "openrouter"
        if self.config.GROQ_API_KEY and self.config.GROQ_API_KEY != "your_groq_api_key_here":
            return "groq"
        if self.config.OPENAI_API_KEY:
            return "openai"
        return "groq"

    def is_llm_configured(self) -> bool:
        if self.provider in {"openai", "openrouter"}:
            key = self.config.OPENAI_API_KEY if self.provider == "openai" else self.config.OPENROUTER_API_KEY
            return bool(self.openai_client and key)
        return bool(self.groq_client and self.config.GROQ_API_KEY and self.config.GROQ_API_KEY != "your_groq_api_key_here")

    def _openrouter_extra_body(self):
        """Return low-latency routing preferences for OpenRouter requests only."""
        if self.provider != "openrouter":
            return None

        provider_order = [
            provider.strip().lower()
            for provider in os.getenv(
                "RETELL_OPENROUTER_PROVIDER_ORDER",
                "coreweave,deepinfra,groq",
            ).split(",")
            if provider.strip()
        ]
        preferences = {"allow_fallbacks": True}
        if provider_order:
            preferences["order"] = provider_order

        # GPT-OSS is a reasoning model. Low effort is appropriate for a
        # real-time voice turn, where first spoken token matters most.
        reasoning_effort = os.getenv("RETELL_OPENROUTER_REASONING_EFFORT", "low").strip().lower()
        extra_body = {"provider": preferences}
        if reasoning_effort in {"low", "medium", "high"}:
            extra_body["reasoning"] = {"effort": reasoning_effort}
        return extra_body

    def chat_completion(self, messages, max_tokens=80, temperature: Optional[float] = None, top_p: Optional[float] = None):
        temperature = self.config.LLM_TEMPERATURE if temperature is None else temperature
        top_p = self.config.LLM_TOP_P if top_p is None else top_p
        if self.provider in {"openai", "openrouter"}:
            if not self.openai_client:
                raise RuntimeError(f"{self.provider.title()} client is not initialized")
            model = self.config.OPENAI_MODEL if self.provider == "openai" else self.config.OPENROUTER_MODEL
            with timed(logger, f"LLM {model}", warn_after_ms=4000):
                response = self.openai_client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    extra_body=self._openrouter_extra_body(),
                )
            return (response.choices[0].message.content or "").strip()

        if not self.groq_client:
            raise RuntimeError("Groq client is not initialized")
        response = self.groq_client.chat.completions.create(
            messages=messages,
            model=self.config.GROQ_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=False
        )
        return (response.choices[0].message.content or "").strip()

    def stream_chat_completion(self, messages, max_tokens=80, temperature: Optional[float] = None, top_p: Optional[float] = None):
        """Yield text deltas for low-latency voice responses."""
        temperature = self.config.LLM_TEMPERATURE if temperature is None else temperature
        top_p = self.config.LLM_TOP_P if top_p is None else top_p
        if self.provider not in {"openai", "openrouter"} or not self.openai_client:
            raise RuntimeError("Streaming is currently available for OpenAI-compatible providers only")

        stream = self.openai_client.chat.completions.create(
            model=self.config.OPENAI_MODEL if self.provider == "openai" else self.config.OPENROUTER_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=True,
            extra_body=self._openrouter_extra_body(),
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield content
    
