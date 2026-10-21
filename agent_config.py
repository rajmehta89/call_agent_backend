"""
Dynamic Agent Configuration Management
Handles greeting messages, exit messages, and prompt configurations

Stored in MongoDB (platform_settings, key "agent_config") so every server and every redeploy share it.
agent_config.json is kept as the first-run default and as a fallback while the database is unreachable.
Reads are cached for DB_REFRESH_SECONDS so a voice turn never waits on the database.
"""

import json
import os
import threading
import time
from typing import Dict, Any, Optional
from datetime import datetime
from timeutil import utc_iso
import logging

logger = logging.getLogger(__name__)

DB_KEY = "agent_config"
DB_REFRESH_SECONDS = 30


def _settings_collection():
    """platform_settings when the database is reachable, else None (imported lazily: mongo_client loads after this module)."""
    try:
        from mongo_client import mongo_client
        return mongo_client.platform_settings if mongo_client and mongo_client.is_connected() else None
    except Exception as error:  # never let config reads break a call
        logger.warning("agent config: database unavailable (%s); using the local file", error)
        return None

class AgentConfig:
    def __init__(self, config_file_path: str = "agent_config.json"):
        self.config_file_path = config_file_path
        self.default_config = {
            "greeting_message": "Hello, this is Raj's AI assistant. I can help with AI agents, voice AI, WhatsApp automation, CRM integrations, and custom software projects. How can I help you today?",
            "exit_message": "Thanks for reaching out. Raj's team will be happy to help with your AI or software project. Have a great day!",
            "system_prompt": (
                "You are Raj Mehta's professional AI business concierge and solution consultant. "
                "Raj is an AI Automation Developer and freelance software developer based in Surat, India. "
                "Help founders and businesses understand how AI agents, voice AI, WhatsApp assistants, chatbots, n8n workflows, CRM/calendar/email/API integrations, and custom web or mobile applications can improve their operations. "
                "Use the approved business knowledge and live application data as the source of truth. Never invent pricing, availability, guarantees, credentials, client results, or technical commitments. "
                "Ask a short clarifying question when the business goal or current process is unclear, then suggest a practical automation flow and next step. "
                "Be warm, confident, consultative, and concise; on voice calls use natural spoken language and normally answer in 1-3 short sentences. "
                "When a request needs a human decision, implementation estimate, account access, or project approval, offer a callback or human handoff."
            ),
            "knowledge_base_enabled": False,
            "knowledge_base": {},
            "human_transfer": {
                "enabled": True,
                "team_mode": "ring_all",
                "fallback_message": "I am transferring you to a human agent now. They will have the context from this call.",
                "agents": []
            },
            "last_updated": utc_iso(),
            "version": "1.0"
        }
        self._db_checked_at = 0.0
        self._refreshing = False
        self.config = self.load_config()

        # Initialize last modified timestamp for config reload tracking
        if os.path.exists(self.config_file_path):
            self._last_modified = os.path.getmtime(self.config_file_path)
        else:
            self._last_modified = 0

    def _load_from_db(self) -> Optional[Dict[str, Any]]:
        collection = _settings_collection()
        if collection is None:
            return None
        document = collection.find_one({"key": DB_KEY})
        self._db_checked_at = time.monotonic()
        return {**self.default_config, **document["value"]} if document and isinstance(document.get("value"), dict) else None

    def _save_to_db(self, config: Dict[str, Any]) -> bool:
        collection = _settings_collection()
        if collection is None:
            return False
        collection.update_one({"key": DB_KEY}, {"$set": {"value": config, "updated_at": datetime.utcnow()}}, upsert=True)
        self._db_checked_at = time.monotonic()
        return True

    def load_config(self) -> Dict[str, Any]:
        """Load configuration from the database, else the file (seeding the database from it), else defaults"""
        try:
            stored = self._load_from_db()
            if stored is not None:
                return stored
        except Exception as e:
            logger.error(f"Error loading config from the database: {e}")
        try:
            if os.path.exists(self.config_file_path):
                with open(self.config_file_path, 'r', encoding='utf-8') as f:
                    config = {**self.default_config, **json.load(f)}  # merge with defaults so all keys exist
                if self._save_to_db(config):
                    logger.info("agent config: copied %s into the database", self.config_file_path)
                return config
            else:
                # Create default config file
                self.save_config(self.default_config)
                return self.default_config.copy()
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return self.default_config.copy()

    def save_config(self, config: Dict[str, Any] = None) -> bool:
        """Save configuration to file"""
        try:
            config_to_save = config or self.config
            config_to_save["last_updated"] = utc_iso()

            saved_to_db = False
            try:
                saved_to_db = self._save_to_db(config_to_save)
            except Exception as e:
                logger.error(f"Error saving config to the database: {e}")
            with open(self.config_file_path, 'w', encoding='utf-8') as f:  # local copy: offline fallback
                json.dump(config_to_save, f, indent=2, ensure_ascii=False)
            self._last_modified = os.path.getmtime(self.config_file_path)

            if config:
                self.config = config_to_save

            logger.info("Configuration saved (%s)", "database + local file" if saved_to_db else f"local file only: {self.config_file_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False

    def update_config(self, updates: Dict[str, Any]) -> bool:
        """Update specific configuration values.
        Writes only the changed fields, then reloads the whole record: the in-memory copy can be up to
        DB_REFRESH_SECONDS old (or come from another process), and saving it whole would silently undo settings
        saved elsewhere in the meantime, e.g. the knowledge copy written by the Knowledge page."""
        try:
            collection = _settings_collection()
            if collection is not None:
                fields = {f"value.{key}": value for key, value in updates.items()}
                collection.update_one({"key": DB_KEY}, {"$set": {**fields, "value.last_updated": utc_iso(), "updated_at": datetime.utcnow()}}, upsert=True)
                stored = self._load_from_db()
                if stored is not None:
                    self.config = stored
                    with open(self.config_file_path, 'w', encoding='utf-8') as f:  # local copy: offline fallback
                        json.dump(self.config, f, indent=2, ensure_ascii=False)
                    self._last_modified = os.path.getmtime(self.config_file_path)
                    return True
            self.config.update(updates)
            return self.save_config()
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return False

    def _needs_reload(self) -> bool:
        """Check if config file has been modified since last load"""
        try:
            if os.path.exists(self.config_file_path):
                current_modified = os.path.getmtime(self.config_file_path)
                return current_modified > self._last_modified
            return False
        except Exception:
            return True  # If we can't check, assume we need to reload

    def _refresh_from_db(self) -> None:
        try:
            stored = self._load_from_db()
            if stored is not None:
                self.config = stored
        except Exception as e:
            logger.warning(f"background config refresh failed: {e}")
        finally:
            self._refreshing = False

    def reload_config(self) -> bool:
        """Pick up changes: from the database every DB_REFRESH_SECONDS, else from the file if it was modified"""
        try:
            if self._db_checked_at and time.monotonic() - self._db_checked_at >= DB_REFRESH_SECONDS:
                # Serve the copy in memory and refresh it in the background: a database round trip (0.1-0.3 s to
                # the cloud) must not sit inside a live call turn. Changes still show within one refresh period.
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh_from_db, name="agent-config-refresh", daemon=True).start()
                return True
            if self._db_checked_at == 0.0:
                stored = self._load_from_db()
                if stored is not None:
                    self.config = stored
                    return True
            if self._db_checked_at == 0.0 and self._needs_reload():
                self.config = self.load_config()
                if os.path.exists(self.config_file_path):
                    self._last_modified = os.path.getmtime(self.config_file_path)
                return True
            return True  # No reload needed
        except Exception as e:
            logger.error(f"Error reloading config: {e}")
            return False

    def get_greeting_message(self) -> str:
        """Get current greeting message (reloads config to get latest)"""
        self.reload_config()
        return self.config.get("greeting_message", self.default_config["greeting_message"])

    def get_exit_message(self) -> str:
        """Get current exit message (reloads config to get latest)"""
        self.reload_config()
        return self.config.get("exit_message", self.default_config["exit_message"])

    def get_system_prompt(self) -> str:
        """Get current system prompt (reloads config to get latest)"""
        self.reload_config()
        return self.config.get("system_prompt", self.default_config["system_prompt"])

    def get_voice_prompt(self) -> str:
        """Full voice-call prompt written by the business ({{business_knowledge}} marks where knowledge goes).
        Empty means the built-in voice rules are used (routers/retell_api.py)."""
        self.reload_config()
        return str(self.config.get("voice_prompt") or "").strip()

    def get_knowledge_base_enabled(self) -> bool:
        """Get knowledge base enabled status"""
        self.reload_config()
        return self.config.get("knowledge_base_enabled", False)

    def get_knowledge_base(self) -> Dict[str, Any]:
        """Get current knowledge base"""
        self.reload_config()
        kb = self.config.get("knowledge_base", {})

        if isinstance(kb, str):
            try:
                import json
                import re
                fixed_kb = re.sub(r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', r'"\1\2"', kb)
                fixed_kb = fixed_kb.replace("'", '"')
                fixed_kb = re.sub(r'\(\s*([^)]+)\s*\)', r'\1', fixed_kb)
                kb = json.loads(fixed_kb)
            except (json.JSONDecodeError, TypeError):
                try:
                    import ast
                    kb = ast.literal_eval(kb)
                except Exception:
                    kb = {}

        if not isinstance(kb, dict):
            kb = {}

        return kb

    def set_greeting_message(self, message: str) -> bool:
        return self.update_config({"greeting_message": message})

    def set_exit_message(self, message: str) -> bool:
        return self.update_config({"exit_message": message})

    def set_system_prompt(self, prompt: str) -> bool:
        return self.update_config({"system_prompt": prompt})

    def set_knowledge_base_enabled(self, enabled: bool) -> bool:
        return self.update_config({"knowledge_base_enabled": enabled})

    def set_knowledge_base(self, knowledge_base: Dict[str, Any]) -> bool:
        return self.update_config({"knowledge_base": knowledge_base})

    def get_all_config(self) -> Dict[str, Any]:
        self.reload_config()
        return self.config.copy()

    def get_human_transfer(self) -> Dict[str, Any]:
        self.reload_config()
        human_transfer = self.config.get("human_transfer", self.default_config["human_transfer"])
        if not isinstance(human_transfer, dict):
            human_transfer = self.default_config["human_transfer"].copy()
        human_transfer.setdefault("enabled", True)
        human_transfer.setdefault("team_mode", "ring_all")
        human_transfer.setdefault("fallback_message", self.default_config["human_transfer"]["fallback_message"])
        human_transfer.setdefault("agents", [])
        return human_transfer

    def set_human_transfer(self, human_transfer: Dict[str, Any]) -> bool:
        return self.update_config({"human_transfer": human_transfer})

    def reset_to_defaults(self) -> bool:
        return self.save_config(self.default_config.copy())

# Global instance
agent_config = AgentConfig()
