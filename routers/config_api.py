"""
Configuration API Router - Agent Configuration Management
Converted from Flask to FastAPI for Render deployment
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any
import json
import os

router = APIRouter(
    prefix="/api/config",
    tags=["Configuration"]
)

# Pydantic models
class ConfigUpdate(BaseModel):
    greeting_message: Optional[str] = None
    exit_message: Optional[str] = None
    system_prompt: Optional[str] = None
    knowledge_base_enabled: Optional[bool] = None
    knowledge_base: Optional[str] = None

class MessageUpdate(BaseModel):
    message: str

class KnowledgeBaseUpdate(BaseModel):
    enabled: Optional[bool] = None
    knowledge_base: Optional[Dict[str, Any]] = None

# Simple agent config class (replace agent_config import)
class AgentConfig:
    def __init__(self):
        self.config_file = "agent_config.json"
        self.config = self.load_config()

    def load_config(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    return json.load(f)
        except:
            pass
        return self.get_defaults()

    def save_config(self):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
            return True
        except:
            return False

    def get_defaults(self):
        return {
            "greeting_message": "Hello! How can I help you today?",
            "exit_message": "Thank you for your time. Have a great day!",
            "system_prompt": "You are a helpful AI assistant.",
            "knowledge_base_enabled": False,
            "knowledge_base": "{}"
        }

    def get_all_config(self):
        return self.config

    def update_config(self, updates):
        self.config.update(updates)
        return self.save_config()

    def set_greeting_message(self, message):
        self.config["greeting_message"] = message
        return self.save_config()

    def set_exit_message(self, message):
        self.config["exit_message"] = message
        return self.save_config()

    def set_system_prompt(self, prompt):
        self.config["system_prompt"] = prompt
        return self.save_config()

    def set_knowledge_base_enabled(self, enabled):
        self.config["knowledge_base_enabled"] = enabled
        return self.save_config()

    def set_knowledge_base(self, knowledge_base):
        self.config["knowledge_base"] = json.dumps(knowledge_base) if isinstance(knowledge_base, dict) else knowledge_base
        return self.save_config()

    def get_greeting_message(self):
        return self.config.get("greeting_message", "")

    def get_exit_message(self):
        return self.config.get("exit_message", "")

    def get_system_prompt(self):
        return self.config.get("system_prompt", "")

    def get_knowledge_base_enabled(self):
        return self.config.get("knowledge_base_enabled", False)

    def get_knowledge_base(self):
        return self.config.get("knowledge_base", "{}")

    def reset_to_defaults(self):
        self.config = self.get_defaults()
        return self.save_config()

# Initialize agent config
agent_config = AgentConfig()

@router.get("/")
async def get_config():
    """Get current agent configuration"""
    try:
        config = agent_config.get_all_config()
        return config
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/")
@router.post("/")
async def update_config(data: ConfigUpdate):
    """Update agent configuration"""
    try:
        updates = {}

        if data.greeting_message is not None:
            updates["greeting_message"] = data.greeting_message.strip()
        if data.exit_message is not None:
            updates["exit_message"] = data.exit_message.strip()
        if data.system_prompt is not None:
            updates["system_prompt"] = data.system_prompt.strip()
        if data.knowledge_base_enabled is not None:
            updates["knowledge_base_enabled"] = data.knowledge_base_enabled
        if data.knowledge_base is not None:
            updates["knowledge_base"] = data.knowledge_base

        if not updates:
            raise HTTPException(status_code=400, detail="No valid fields to update")

        success = agent_config.update_config(updates)

        if success:
            return {
                "success": True,
                "message": "Configuration updated successfully",
                "data": agent_config.get_all_config()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to save configuration")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/greeting")
async def update_greeting(data: MessageUpdate):
    """Update greeting message only"""
    try:
        if not data.message.strip():
            raise HTTPException(status_code=400, detail="Message cannot be empty")

        success = agent_config.set_greeting_message(data.message.strip())

        if success:
            return {
                "success": True,
                "message": "Greeting updated successfully",
                "greeting_message": agent_config.get_greeting_message()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to update greeting")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/exit")
async def update_exit(data: MessageUpdate):
    """Update exit message only"""
    try:
        if not data.message.strip():
            raise HTTPException(status_code=400, detail="Message cannot be empty")

        success = agent_config.set_exit_message(data.message.strip())

        if success:
            return {
                "success": True,
                "message": "Exit message updated successfully",
                "exit_message": agent_config.get_exit_message()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to update exit message")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/prompt")
async def update_prompt(data: MessageUpdate):
    """Update system prompt only"""
    try:
        if not data.message.strip():
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")

        success = agent_config.set_system_prompt(data.message.strip())

        if success:
            return {
                "success": True,
                "message": "System prompt updated successfully",
                "system_prompt": agent_config.get_system_prompt()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to update system prompt")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/knowledge-base")
async def update_knowledge_base(data: KnowledgeBaseUpdate):
    """Update knowledge base settings"""
    try:
        if data.enabled is not None:
            success = agent_config.set_knowledge_base_enabled(data.enabled)
            if not success:
                raise HTTPException(status_code=500, detail="Failed to update knowledge base enabled status")

        if data.knowledge_base:
            success = agent_config.set_knowledge_base(data.knowledge_base)
            if not success:
                raise HTTPException(status_code=500, detail="Failed to update knowledge base")

        return {
            "success": True,
            "message": "Knowledge base updated successfully",
            "data": {
                "knowledge_base_enabled": agent_config.get_knowledge_base_enabled(),
                "knowledge_base": agent_config.get_knowledge_base()
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/knowledge-base")
async def get_knowledge_base():
    """Get current knowledge base settings"""
    try:
        return {
            "success": True,
            "data": {
                "knowledge_base_enabled": agent_config.get_knowledge_base_enabled(),
                "knowledge_base": agent_config.get_knowledge_base()
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reset")
async def reset_config():
    """Reset configuration to defaults"""
    try:
        success = agent_config.reset_to_defaults()

        if success:
            return {
                "success": True,
                "message": "Configuration reset to defaults",
                "data": agent_config.get_all_config()
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to reset configuration")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
