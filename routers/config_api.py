"""
Configuration API Router - Agent Configuration Management
Converted from Flask to FastAPI for Render deployment
"""

from fastapi import APIRouter, HTTPException
from fastapi.middleware.cors import CORS
from pydantic import BaseModel
from typing import Optional, Dict, Any
import json
import os
from datetime import datetime

# Initialize FastAPI router
router = APIRouter(
    prefix="/api/config",
    tags=["Configuration"]
)

# Add CORS middleware (equivalent to Flask's CORS configuration)
from fastapi import FastAPI
app = FastAPI()
app.add_middleware(
    CORS,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
            self.config["last_updated"] = datetime.utcnow().isoformat()
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
            "knowledge_base": "{}",
            "last_updated": datetime.utcnow().isoformat()
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
        return jsonify(config)  # Mimic Flask's jsonify behavior
    except Exception as e:
        return {"success": False, "error": str(e)}, 500

@router.put("/")
@router.post("/")
async def update_config(data: ConfigUpdate):
    """Update agent configuration"""
    try:
        if not data.dict(exclude_unset=True):
            return {"success": False, "error": "No data provided"}, 400

        valid_fields = ["greeting_message", "exit_message", "system_prompt", "knowledge_base_enabled", "knowledge_base"]
        updates = {}

        for field in valid_fields:
            if getattr(data, field) is not None:
                if field == "knowledge_base_enabled":
                    if not isinstance(data.knowledge_base_enabled, bool):
                        return {"success": False, "error": f"Invalid value for {field}. Must be a boolean."}, 400
                    updates[field] = data.knowledge_base_enabled
                elif field == "knowledge_base":
                    if not isinstance(data.knowledge_base, str):
                        return {"success": False, "error": f"Invalid value for {field}. Must be a string."}, 400
                    updates[field] = data.knowledge_base
                else:
                    value = getattr(data, field)
                    if not isinstance(value, str):
                        return {"success": False, "error": f"Invalid value for {field}. Must be a string."}, 400
                    updates[field] = value.strip()

        if not updates:
            return {"success": False, "error": "No valid fields to update"}, 400

        success = agent_config.update_config(updates)

        if success:
            return {
                "success": True,
                "message": "Configuration updated successfully",
                "data": agent_config.get_all_config()
            }, 200
        else:
            return {"success": False, "error": "Failed to save configuration"}, 500

    except Exception as e:
        return {"success": False, "error": str(e)}, 500

@router.post("/greeting")
async def update_greeting(data: MessageUpdate):
    """Update greeting message only"""
    try:
        message = data.message.strip()
        if not message:
            return {"success": False, "error": "Message cannot be empty"}, 400

        success = agent_config.set_greeting_message(message)

        if success:
            return {
                "success": True,
                "message": "Greeting updated successfully",
                "greeting_message": agent_config.get_greeting_message()
            }, 200
        else:
            return {"success": False, "error": "Failed to update greeting"}, 500

    except Exception as e:
        return {"success": False, "error": str(e)}, 500

@router.post("/exit")
async def update_exit(data: MessageUpdate):
    """Update exit message only"""
    try:
        message = data.message.strip()
        if not message:
            return {"success": False, "error": "Message cannot be empty"}, 400

        success = agent_config.set_exit_message(message)

        if success:
            return {
                "success": True,
                "message": "Exit message updated successfully",
                "exit_message": agent_config.get_exit_message()
            }, 200
        else:
            return {"success": False, "error": "Failed to update exit message"}, 500

    except Exception as e:
        return {"success": False, "error": str(e)}, 500

@router.post("/prompt")
async def update_prompt(data: MessageUpdate):
    """Update system prompt only"""
    try:
        prompt = data.message.strip()
        if not prompt:
            return {"success": False, "error": "Prompt cannot be empty"}, 400

        success = agent_config.set_system_prompt(prompt)

        if success:
            return {
                "success": True,
                "message": "System prompt updated successfully",
                "system_prompt": agent_config.get_system_prompt()
            }, 200
        else:
            return {"success": False, "error": "Failed to update system prompt"}, 500

    except Exception as e:
        return {"success": False, "error": str(e)}, 500

@router.post("/knowledge-base")
async def update_knowledge_base(data: KnowledgeBaseUpdate):
    """Update knowledge base settings"""
    try:
        if not data.dict(exclude_unset=True):
            return {"success": False, "error": "No data provided"}, 400

        if data.enabled is not None:
            success = agent_config.set_knowledge_base_enabled(data.enabled)
            if not success:
                return {"success": False, "error": "Failed to update knowledge base enabled status"}, 500

        if data.knowledge_base is not None:
            success = agent_config.set_knowledge_base(data.knowledge_base)
            if not success:
                return {"success": False, "error": "Failed to update knowledge base"}, 500

        return {
            "success": True,
            "message": "Knowledge base updated successfully",
            "data": {
                "knowledge_base_enabled": agent_config.get_knowledge_base_enabled(),
                "knowledge_base": agent_config.get_knowledge_base()
            }
        }, 200

    except Exception as e:
        return {"success": False, "error": str(e)}, 500

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
        }, 200
    except Exception as e:
        return {"success": False, "error": str(e)}, 500

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
            }, 200
        else:
            return {"success": False, "error": "Failed to reset configuration"}, 500

    except Exception as e:
        return {"success": False, "error": str(e)}, 500

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        return {
            "success": True,
            "message": "Config API is running",
            "timestamp": agent_config.get_all_config().get("last_updated")
        }, 200
    except Exception as e:
        return {"success": False, "error": str(e)}, 500

# Note: The FastAPI app should be run separately, typically in a main.py file
# Example:
# if __name__ == "__main__":
#     import uvicorn
#     print("🚀 Agent Configuration API running on port 8000")
#     uvicorn.run(app, host="0.0.0.0", port=8000)