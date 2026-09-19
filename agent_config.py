"""
Dynamic Agent Configuration Management
Handles greeting messages, exit messages, and prompt configurations
"""

import json
import os
from typing import Dict, Any
from datetime import datetime

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
            "last_updated": datetime.now().isoformat(),
            "version": "1.0"
        }
        self.config = self.load_config()

        # Initialize last modified timestamp for config reload tracking
        if os.path.exists(self.config_file_path):
            self._last_modified = os.path.getmtime(self.config_file_path)
        else:
            self._last_modified = 0

    def load_config(self) -> Dict[str, Any]:
        """Load configuration from file or create with defaults"""
        try:
            if os.path.exists(self.config_file_path):
                with open(self.config_file_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # Merge with defaults to ensure all keys exist
                    return {**self.default_config, **config}
            else:
                # Create default config file
                self.save_config(self.default_config)
                return self.default_config.copy()
        except Exception as e:
            print(f"Error loading config: {e}")
            return self.default_config.copy()

    def save_config(self, config: Dict[str, Any] = None) -> bool:
        """Save configuration to file"""
        try:
            config_to_save = config or self.config
            config_to_save["last_updated"] = datetime.now().isoformat()

            with open(self.config_file_path, 'w', encoding='utf-8') as f:
                json.dump(config_to_save, f, indent=2, ensure_ascii=False)

            if config:
                self.config = config_to_save

            print(f"Configuration saved to {self.config_file_path}")
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False

    def update_config(self, updates: Dict[str, Any]) -> bool:
        """Update specific configuration values"""
        try:
            self.config.update(updates)
            return self.save_config()
        except Exception as e:
            print(f"Error updating config: {e}")
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

    def reload_config(self) -> bool:
        """Reload configuration from file if it has been modified"""
        try:
            if self._needs_reload():
                self.config = self.load_config()
                if os.path.exists(self.config_file_path):
                    self._last_modified = os.path.getmtime(self.config_file_path)
                return True
            return True  # No reload needed
        except Exception as e:
            print(f"Error reloading config: {e}")
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
