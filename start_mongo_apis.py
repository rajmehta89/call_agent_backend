#!/usr/bin/env python3
"""
Startup script for MongoDB-based APIs
Runs leads API, calls API, and websocket server
"""

import subprocess
import sys
import time
import os
from threading import Thread

def start_api(script_name, port, description):
    """Start an API server"""
    print(f"🚀 Starting {description} on port {port}...")
    try:
        subprocess.run([sys.executable, script_name], check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to start {description}: {e}")
    except KeyboardInterrupt:
        print(f"⏹️ Stopping {description}...")

def main():
    print("🎯 Starting MongoDB-based AI Agent APIs...")
    print("=" * 50)
    
    # Check if MongoDB is running
    print("🔍 Checking MongoDB connection...")
    try:
        from mongo_client import mongo_client
        if mongo_client.is_connected():
            print("✅ MongoDB connected successfully")
        else:
            print("❌ MongoDB connection failed")
            print("Please make sure MongoDB is running on localhost:27017")
            return
    except Exception as e:
        print(f"❌ MongoDB connection error: {e}")
        return
    
    # Start APIs in separate threads
    threads = []
    
    # Start Leads API (port 5002)
    leads_thread = Thread(target=start_api, args=("leads_api_mongo.py", 5002, "Leads API"))
    threads.append(leads_thread)
    
    # Start Calls API (port 5004)
    calls_thread = Thread(target=start_api, args=("calls_api.py", 5004, "Calls API"))
    threads.append(calls_thread)
    
    # Start WebSocket Server (port 8765)
    ws_thread = Thread(target=start_api, args=("websocket_server.py", 8765, "WebSocket Server"))
    threads.append(ws_thread)
    
    # Start all threads
    for thread in threads:
        thread.daemon = True
        thread.start()
        time.sleep(2)  # Give each API time to start
    
    print("\n✅ All APIs started successfully!")
    print("📊 Available APIs:")
    print("   - Leads API: http://localhost:5002")
    print("   - Calls API: http://localhost:5004")
    print("   - WebSocket: ws://localhost:8765")
    print("\n🔄 Press Ctrl+C to stop all services")
    
    try:
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⏹️ Shutting down all services...")

if __name__ == "__main__":
    main() 