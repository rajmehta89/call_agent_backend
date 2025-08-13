#!/usr/bin/env python3
"""
Startup script for all backend services
Runs Configuration API, WebSocket Server, and Webhook in parallel
"""

import subprocess
import sys
import time
import signal
import os
from threading import Thread
from config import CONFIG_API_PORT, LEADS_API_PORT, CALLS_API_PORT, WEBHOOK_PORT, WEBSOCKET_PORT

class BackendRunner:
    def __init__(self):
        self.processes = []
        self.running = True
    
    def run_service(self, script_name, port, description):
        """Run a service and monitor it"""
        try:
            print(f"🚀 Starting {description} on port {port}...")
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            process = subprocess.Popen([
                sys.executable, "-u", script_name
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
            
            self.processes.append(process)
            
            # Monitor process output
            while self.running and process.poll() is None:
                line = process.stdout.readline()
                if line:
                    print(f"[{description}] {line.strip()}", flush=True)
            
            if process.poll() is not None and self.running:
                print(f"❌ {description} exited with code {process.poll()}")
                
        except Exception as e:
            print(f"❌ Error starting {description}: {e}")
    
    def start_all_services(self):
        """Start all backend services"""
        print("🚀 Starting AI Agent Backend Services...")
        print("=" * 50)
        
        # Define services
        services = [
            ("config_api.py", 5001, "Configuration API"),
            ("leads_api_mongo.py", 5002, "Leads Management API (MongoDB)"),
            ("calls_api.py", 5004, "Calls API (MongoDB)"),
            ("websocket_server.py", 8765, "WebSocket Server"),
            ("webhook.py", 3001, "Webhook Server")
        ]
        
        # Start each service in a separate thread
        threads = []
        for script, port, description in services:
            if os.path.exists(script):
                thread = Thread(
                    target=self.run_service,
                    args=(script, port, description),
                    daemon=True
                )
                thread.start()
                threads.append(thread)
                time.sleep(1)  # Stagger startup
            else:
                print(f"⚠️  Warning: {script} not found")
        
        try:
            print("\n✅ All services started successfully!")
            print("\n📋 Service URLs:")
            print(f"   • Configuration API: http://localhost:{CONFIG_API_PORT}")
            print(f"   • Leads Management API: http://localhost:{LEADS_API_PORT}")
            print(f"   • Calls API: http://localhost:{CALLS_API_PORT}")
            print(f"   • WebSocket Server: ws://localhost:{WEBSOCKET_PORT}")
            print(f"   • Webhook Server: http://localhost:{WEBHOOK_PORT}")
            print("   • Frontend (after npm run dev): http://localhost:3000")
            print("\n🛑 Press Ctrl+C to stop all services")
            print("=" * 50)
            
            # Keep main thread alive
            while self.running:
                time.sleep(1)
                
        except KeyboardInterrupt:
            print("\n\n🛑 Shutting down services...")
            self.stop_all_services()
    
    def stop_all_services(self):
        """Stop all running services"""
        self.running = False
        for process in self.processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            except Exception as e:
                print(f"Error stopping process: {e}")
        
        print("✅ All services stopped.")

def main():
    """Main entry point"""
    # Change to backend directory
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(backend_dir)
    
    # Check for required files
    required_files = ["config_api.py", "leads_api_mongo.py", "calls_api.py", "websocket_server.py", "webhook.py"]
    missing_files = [f for f in required_files if not os.path.exists(f)]
    
    if missing_files:
        print(f"❌ Missing required files: {', '.join(missing_files)}")
        print("Please ensure all backend files are present.")
        sys.exit(1)
    
    # Check for .env file
    if not os.path.exists(".env"):
        print("⚠️  Warning: .env file not found.")
        print("Please create a .env file with your API keys.")
        print("See README.md for configuration details.")
    
    # Test MongoDB Atlas connection
    print("🔍 Testing MongoDB Atlas connection...")
    try:
        from mongo_client import mongo_client
        if mongo_client.is_connected():
            print("✅ MongoDB Atlas connected successfully")
        else:
            print("❌ MongoDB Atlas connection failed")
            print("Please check your MONGO_URI in .env file")
            print("Make sure to replace <db_password> with your actual password")
            sys.exit(1)
    except Exception as e:
        print(f"❌ MongoDB Atlas connection error: {e}")
        print("Please check your MongoDB Atlas configuration")
        sys.exit(1)
    
    # Start backend runner
    runner = BackendRunner()
    
    # Handle signals gracefully
    def signal_handler(signum, frame):
        print("\n🛑 Received shutdown signal...")
        runner.stop_all_services()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start all services
    runner.start_all_services()

if __name__ == "__main__":
    main() 