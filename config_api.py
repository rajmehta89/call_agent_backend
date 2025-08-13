"""
Flask API for Agent Configuration Management
Provides endpoints for frontend to manage agent settings
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from agent_config import agent_config
import os
from dotenv import load_dotenv
from config import ALLOWED_ORIGINS

load_dotenv()

app = Flask(__name__)
CORS(app, origins=ALLOWED_ORIGINS)

@app.route("/api/config", methods=["GET"])
def get_config():
    """Get current agent configuration"""
    try:
        config = agent_config.get_all_config()
        return jsonify(config), 200
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config", methods=["PUT", "POST"])
def update_config():
    """Update agent configuration"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                "success": False,
                "error": "No data provided"
            }), 400
        
        # Validate required fields
        valid_fields = ["greeting_message", "exit_message", "system_prompt", "knowledge_base_enabled", "knowledge_base"]
        updates = {}
        
        for field in valid_fields:
            if field in data:
                if field == "knowledge_base_enabled":
                    # Boolean field
                    if not isinstance(data[field], bool):
                        return jsonify({
                            "success": False,
                            "error": f"Invalid value for {field}. Must be a boolean."
                        }), 400
                    updates[field] = data[field]
                elif field == "knowledge_base":
                    # String field (JSON string)
                    if not isinstance(data[field], str):
                        return jsonify({
                            "success": False,
                            "error": f"Invalid value for {field}. Must be a string."
                        }), 400
                    updates[field] = data[field]
                else:
                    # String fields
                    value = data[field]
                    if not isinstance(value, str):
                        return jsonify({
                            "success": False,
                            "error": f"Invalid value for {field}. Must be a string."
                        }), 400
                    updates[field] = value.strip()
        
        if not updates:
            return jsonify({
                "success": False,
                "error": "No valid fields to update"
            }), 400
        
        success = agent_config.update_config(updates)
        
        if success:
            return jsonify({
                "success": True,
                "message": "Configuration updated successfully",
                "data": agent_config.get_all_config()
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "Failed to save configuration"
            }), 500
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config/greeting", methods=["POST"])
def update_greeting():
    """Update greeting message only"""
    try:
        data = request.get_json()
        message = data.get("message", "").strip()
        
        if not message:
            return jsonify({
                "success": False,
                "error": "Message cannot be empty"
            }), 400
        
        success = agent_config.set_greeting_message(message)
        
        if success:
            return jsonify({
                "success": True,
                "message": "Greeting updated successfully",
                "greeting_message": agent_config.get_greeting_message()
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "Failed to update greeting"
            }), 500
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config/exit", methods=["POST"])
def update_exit():
    """Update exit message only"""
    try:
        data = request.get_json()
        message = data.get("message", "").strip()
        
        if not message:
            return jsonify({
                "success": False,
                "error": "Message cannot be empty"
            }), 400
        
        success = agent_config.set_exit_message(message)
        
        if success:
            return jsonify({
                "success": True,
                "message": "Exit message updated successfully",
                "exit_message": agent_config.get_exit_message()
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "Failed to update exit message"
            }), 500
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config/prompt", methods=["POST"])
def update_prompt():
    """Update system prompt only"""
    try:
        data = request.get_json()
        prompt = data.get("prompt", "").strip()
        
        if not prompt:
            return jsonify({
                "success": False,
                "error": "Prompt cannot be empty"
            }), 400
        
        success = agent_config.set_system_prompt(prompt)
        
        if success:
            return jsonify({
                "success": True,
                "message": "System prompt updated successfully",
                "system_prompt": agent_config.get_system_prompt()
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "Failed to update system prompt"
            }), 500
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config/knowledge-base", methods=["POST"])
def update_knowledge_base():
    """Update knowledge base settings"""
    try:
        data = request.get_json()
        enabled = data.get("enabled")
        knowledge_base = data.get("knowledge_base", {})
        
        if enabled is not None:
            success = agent_config.set_knowledge_base_enabled(enabled)
            if not success:
                return jsonify({
                    "success": False,
                    "error": "Failed to update knowledge base enabled status"
                }), 500
        
        if knowledge_base:
            success = agent_config.set_knowledge_base(knowledge_base)
            if not success:
                return jsonify({
                    "success": False,
                    "error": "Failed to update knowledge base"
                }), 500
        
        return jsonify({
            "success": True,
            "message": "Knowledge base updated successfully",
            "data": {
                "knowledge_base_enabled": agent_config.get_knowledge_base_enabled(),
                "knowledge_base": agent_config.get_knowledge_base()
            }
        }), 200
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config/knowledge-base", methods=["GET"])
def get_knowledge_base():
    """Get current knowledge base settings"""
    try:
        return jsonify({
            "success": True,
            "data": {
                "knowledge_base_enabled": agent_config.get_knowledge_base_enabled(),
                "knowledge_base": agent_config.get_knowledge_base()
            }
        }), 200
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/config/reset", methods=["POST"])
def reset_config():
    """Reset configuration to defaults"""
    try:
        success = agent_config.reset_to_defaults()
        
        if success:
            return jsonify({
                "success": True,
                "message": "Configuration reset to defaults",
                "data": agent_config.get_all_config()
            }), 200
        else:
            return jsonify({
                "success": False,
                "error": "Failed to reset configuration"
            }), 500
            
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "success": True,
        "message": "Config API is running",
        "timestamp": agent_config.config.get("last_updated")
    }), 200

if __name__ == "__main__":
    print("Agent Configuration API running on port 5001")
    app.run(host="0.0.0.0", port=5001, debug=False) 