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
