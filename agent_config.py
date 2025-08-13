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
            "greeting_message": "Hello! I'm here to help you with information about Vansh Real Estate Developers projects. How can I assist you?",
            "exit_message": "Thank you for your interest in Vansh Real Estate Developers. Have a great day!",
            "system_prompt": (
                "You are a professional, friendly real estate assistant representing Vansh Real Estate Developers. ONLY answer questions using the provided real estate information. "
                "When a user asks a question, respond ONLY with the most relevant, specific answer. Do NOT include extra details unless the user explicitly asks for more. "
                "Keep responses SHORT and CONCISE - aim for 1-2 sentences maximum."
            ),
            "knowledge_base_enabled": False,
            "knowledge_base": {},
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
        return self.config.copy()

    def reset_to_defaults(self) -> bool:
        return self.save_config(self.default_config.copy())

# Global instance
agent_config = AgentConfig()
