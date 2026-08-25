#!/usr/bin/env python3
"""
MongoDB Client Configuration
Handles database connections and collections
"""

import os
from pymongo import MongoClient
from datetime import datetime
from typing import Dict, Any, List, Optional
from env_loader import load_project_env

load_project_env()

class MongoDBClient:
    def __init__(self):
        # Get MongoDB Atlas connection string from environment
        self.mongo_uri = os.getenv("MONGO_URI")
        self.database_name = os.getenv("MONGO_DB", "ai_agent_assist")
        self.last_error: Optional[str] = None
        
        if not self.mongo_uri:
            print("MONGO_URI not found in environment variables")
            print("Please add your MongoDB Atlas connection string to .env file")
            self.client = None
            self.db = None
            self.last_error = "MONGO_URI not configured"
            return
        
        try:
            print("Connecting to MongoDB Atlas...")
            self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000)
            # Force a real connection during startup so bad URIs or Atlas access issues fail early.
            self.client.admin.command("ping")
            self.db = self.client[self.database_name]
            
            # Initialize collections
            self.leads = self.db.leads
            self.calls = self.db.calls
            self.whatsapp_conversations = self.db.whatsapp_conversations
            self.whatsapp_messages = self.db.whatsapp_messages
            self.customers = self.db.customers
            self.platform_settings = self.db.platform_settings
            self.automations = self.db.automations
            self.whatsapp_templates = self.db.whatsapp_templates
            self.ai_activity = self.db.ai_activity
            self.team_members = self.db.team_members
            self.shopify_sync_history = self.db.shopify_sync_history
            self.audit_logs = self.db.audit_logs
            self.notifications = self.db.notifications
            
            # Create indexes for better performance
            self.leads.create_index("phone", unique=True)
            self.leads.create_index("email")
            self.leads.create_index("status")
            self.leads.create_index("created_at")
            
            self.calls.create_index("lead_id")
            self.calls.create_index("phone_number")
            self.calls.create_index("call_date")
            self.calls.create_index("status")
            # Unique session id to deduplicate multiple WS reconnects for the same call
            try:
                self.calls.create_index("call_session_id", unique=True, sparse=True)
            except Exception:
                # Index may already exist
                pass

            self.whatsapp_conversations.create_index("customer_phone", unique=True)
            self.whatsapp_conversations.create_index("updated_at")
            self.whatsapp_conversations.create_index("status")

            self.whatsapp_messages.create_index("conversation_id")
            self.whatsapp_messages.create_index("customer_phone")
            self.whatsapp_messages.create_index("created_at")
            try:
                self.whatsapp_messages.create_index("provider_message_id", unique=True, sparse=True)
            except Exception:
                pass

            self.customers.create_index("phone", unique=True, sparse=True)
            self.customers.create_index("email", sparse=True)
            self.customers.create_index("updated_at")
            self.platform_settings.create_index("key", unique=True)
            self.automations.create_index("enabled")
            self.automations.create_index("updated_at")
            self.whatsapp_templates.create_index("name", unique=True)
            self.ai_activity.create_index("created_at")
            self.ai_activity.create_index("channel")
            self.ai_activity.create_index("customer_phone")
            self.team_members.create_index("email", unique=True, sparse=True)
            self.shopify_sync_history.create_index("created_at")
            self.audit_logs.create_index("created_at")
            self.audit_logs.create_index("resource")
            self.notifications.create_index("created_at")
            self.notifications.create_index("read")
            
            print("MongoDB connected successfully")
            
        except Exception as e:
            print(f"MongoDB connection failed: {e}")
            self.client = None
            self.db = None
            self.last_error = str(e)
    
    def is_connected(self) -> bool:
        """Check if MongoDB is connected"""
        if self.client is None:
            return False

        try:
            self.client.admin.command("ping")
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False
    
    def get_database_stats(self) -> Dict[str, Any]:
        """Get database statistics"""
        if not self.is_connected():
            return {"error": "Database not connected"}
        
        try:
            leads_count = self.leads.count_documents({})
            calls_count = self.calls.count_documents({})
            
            # Get leads by status
            status_counts = {}
            for status in ["new", "called", "contacted", "converted"]:
                status_counts[status] = self.leads.count_documents({"status": status})
            
            return {
                "leads_count": leads_count,
                "calls_count": calls_count,
                "status_counts": status_counts,
                "connected": True
            }
        except Exception as e:
            return {"error": str(e), "connected": False}

    def get_connection_status(self) -> Dict[str, Any]:
        return {
            "connected": self.is_connected(),
            "database": self.database_name,
            "last_error": self.last_error,
        }

# Global MongoDB client instance
mongo_client = MongoDBClient() 
