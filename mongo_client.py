#!/usr/bin/env python3
"""
MongoDB Client Configuration
Handles database connections and collections
"""

import os
import time
from pymongo import MongoClient
from datetime import datetime
from typing import Dict, Any, List, Optional
from env_loader import load_project_env
import logging

logger = logging.getLogger(__name__)

load_project_env()

class MongoDBClient:
    def __init__(self):
        # Get MongoDB Atlas connection string from environment
        self.mongo_uri = os.getenv("MONGO_URI")
        self.database_name = os.getenv("MONGO_DB", "ai_agent_assist")
        self.last_error: Optional[str] = None
        
        if not self.mongo_uri:
            logger.warning("MONGO_URI not found in environment variables")
            logger.info("Please add your MongoDB Atlas connection string to .env file")
            self.client = None
            self.db = None
            self.last_error = "MONGO_URI not configured"
            return
        
        self._last_attempt = 0.0
        self._connect()

    # A failed connection is retried at most this often, from is_connected(), so a network drop while the
    # server starts (e.g. a phone hotspot) heals by itself instead of leaving every request "not connected".
    RECONNECT_EVERY_SECONDS = 10
    # How long a successful ping is trusted before is_connected() pings again.
    PING_TRUST_SECONDS = 15
    _last_ok = 0.0

    def _connect(self) -> None:
        self._last_attempt = time.monotonic()
        try:
            logger.info("Connecting to MongoDB Atlas...")
            # Networks like phone hotspots silently drop idle connections; recycling them after 60s
            # means a request never waits on a dead socket.
            self.client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=5000, maxIdleTimeMS=60000, connectTimeoutMS=10000, socketTimeoutMS=20000)
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
            self.automation_runs = self.db.automation_runs
            self.whatsapp_templates = self.db.whatsapp_templates
            self.ai_activity = self.db.ai_activity
            self.brain_documents = self.db.brain_documents
            self.brain_sources = self.db.brain_sources
            self.team_members = self.db.team_members
            self.users = self.db.users
            self.roles = self.db.roles
            self.invitations = self.db.invitations
            self.shopify_sync_history = self.db.shopify_sync_history
            self.shopify_oauth_states = self.db.shopify_oauth_states  # one-time "Connect with Shopify" requests
            self.audit_logs = self.db.audit_logs
            self.notifications = self.db.notifications
            self.sms_messages = self.db.sms_messages
            self.email_outbox = self.db.email_outbox
            self.campaign_prospects = self.db.campaign_prospects
            self.discovery_runs = self.db.discovery_runs
            self.team_messages = self.db.team_messages
            self.inbox_threads = self.db.inbox_threads  # per-caller inbox state (assignment), keyed by caller identity
            self.login_failures = self.db.login_failures  # wrong passwords, expire after 15 min (sign-in lockout)
            
            # Create indexes for better performance
            self.leads.create_index("phone", unique=True)
            self.leads.create_index("email")
            self.leads.create_index("status")
            self.leads.create_index("created_at")
            self.leads.create_index("assigned_to", sparse=True)  # team members see the leads assigned to them
            
            self.calls.create_index("lead_id")
            self.calls.create_index("phone_number")
            self.calls.create_index("call_date")
            self.calls.create_index("status")
            self.inbox_threads.create_index("assigned_to", sparse=True)
            self.inbox_threads.create_index("call_summary_cache.latest.call_date")
            self.calls.create_index([("phone_number", 1), ("call_date", -1)])
            self.sms_messages.create_index([("workspace_id", 1), ("lead_id", 1), ("created_at", -1)])
            self.team_messages.create_index([("workspace_id", 1), ("created_at", -1)])
            self.team_messages.create_index("mentions")
            self.team_messages.create_index("conversation_id", sparse=True)
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
            self.automation_runs.create_index("automation_id")
            self.automation_runs.create_index("created_at")
            self.whatsapp_templates.create_index("name", unique=True)
            self.ai_activity.create_index("created_at")
            self.ai_activity.create_index("channel")
            self.ai_activity.create_index("customer_phone")
            self.brain_documents.create_index("source_key")
            self.brain_documents.create_index("source_id")
            self.brain_documents.create_index("source_type")
            self.brain_documents.create_index("updated_at")
            self.brain_sources.create_index("source_id", unique=True)
            self.brain_sources.create_index("updated_at")
            self.team_members.create_index("email", unique=True, sparse=True)
            self.users.create_index("email", unique=True)
            self.users.create_index("workspace_id")
            self.roles.create_index("name", unique=True)
            self.invitations.create_index("token_hash", unique=True)
            self.invitations.create_index("expires_at")
            self.shopify_sync_history.create_index("created_at")
            self.shopify_oauth_states.create_index("created_at", expireAfterSeconds=3600)
            self.audit_logs.create_index("created_at")
            self.audit_logs.create_index("resource")
            self.notifications.create_index("created_at")
            self.email_outbox.create_index("status")
            self.email_outbox.create_index("created_at")
            self.email_outbox.create_index(
                [("recipient_email", 1)],
                name="email_outbox_recipient_unique_v1",
                unique=True,
                sparse=True,
            )
            self.email_outbox.create_index(
                [("company_key", 1)],
                name="email_outbox_company_unique_v1",
                unique=True,
                sparse=True,
            )
            self.campaign_prospects.create_index("dedupe_key", unique=True, sparse=True)
            self.campaign_prospects.create_index(
                [("email_key", 1)],
                name="campaign_prospects_email_unique_v1",
                unique=True,
                sparse=True,
            )
            self.campaign_prospects.create_index("status")
            self.campaign_prospects.create_index("created_at")
            self.discovery_runs.create_index("created_at")
            self.notifications.create_index("read")
            # The bell and the inbox read a person's unread notices every few seconds.
            self.notifications.create_index([("recipient", 1), ("read", 1), ("created_at", -1)])
            self.login_failures.create_index("at", expireAfterSeconds=15 * 60)
            self.login_failures.create_index("email")
            
            logger.info("MongoDB connected successfully")
            
        except Exception as e:
            logger.error(f"MongoDB connection failed: {e}")
            self.client = None
            self.db = None
            self.last_error = str(e)
    
    def is_connected(self) -> bool:
        """Check if MongoDB is connected, reconnecting if the last attempt failed."""
        if self.client is None:
            if not self.mongo_uri or time.monotonic() - self._last_attempt < self.RECONNECT_EVERY_SECONDS:
                return False
            logger.warning("MongoDB is not connected (%s); retrying", self.last_error)
            self._connect()
            return self.client is not None

        # A ping is a network round trip (0.4 s, at times over 2 s, to Atlas) and a voice turn asks several times
        # before it can speak: trust a successful ping for a few seconds. Failed queries still raise and are handled.
        if time.monotonic() - self._last_ok < self.PING_TRUST_SECONDS:
            return True
        try:
            self.client.admin.command("ping")
            self.last_error = None
            self._last_ok = time.monotonic()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self._last_ok = 0.0
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
