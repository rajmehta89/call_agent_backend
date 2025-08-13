#!/usr/bin/env python3
"""
Start ngrok tunnels for public webhook exposure
Exposes webhook and WebSocket servers to the internet
"""

import subprocess
import sys
import time
import os
import requests
import json
from config import NGROK_API


def start_ngrok():
    """Start ngrok with the configuration file and keep it running."""
    try:
        print("🚀 Starting ngrok tunnels...")

        # Resolve config path relative to this file so it works from any CWD
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "ngrok.yml")

        # Check if ngrok.yml exists
        if not os.path.exists(config_path):
            print("❌ ngrok.yml not found. Please ensure the configuration file exists.")
            print(f"Looked for: {config_path}")
            return False

        # Start ngrok with config file; inherit stdout/stderr so buffers don't fill
        process = subprocess.Popen([
            "ngrok", "start", "--all", "--config", config_path
        ])

        # Wait a bit for ngrok to start
        time.sleep(3)

        # Check if ngrok is running
        try:
            response = requests.get(f"{NGROK_API}/api/tunnels", timeout=5)
            tunnels = response.json().get("tunnels", [])

            if not tunnels:
                print("❌ No tunnels found. Ngrok may not have started properly.")
                # Fall through to keepalive loop which will show if process exited
            else:
                print("\n✅ Ngrok tunnels started successfully!")
                print("=" * 60)

                webhook_url = None
                websocket_url = None

                for tunnel in tunnels:
                    name = tunnel.get("name", "")
                    public_url = tunnel.get("public_url", "")
                    local_addr = tunnel.get("config", {}).get("addr", "")

                    print(f"🌐 {name.upper()}:")
                    print(f"   Public URL: {public_url}")
                    # local_addr already contains scheme and host, just print it
                    print(f"   Local: {local_addr}")
                    
                    if name.lower() == "webhook":
                        webhook_url = public_url
                    elif name.lower() == "websocket":
                        websocket_url = public_url.replace("http", "ws")

                    print()

                print("=" * 60)
                print("📋 IMPORTANT URLS FOR PIOPIY DASHBOARD:")
                print("=" * 60)

                if webhook_url:
                    print(f"🔗 Webhook URL (for Piopiy Dashboard):")
                    print(f"   {webhook_url}/python/inbound")
                    print(f"   {webhook_url}/piopiy/events")
                    print()

                if websocket_url:
                    print(f"🔗 WebSocket URL (for .env file):")
                    print(f"   WEBSOCKET_URL={websocket_url}")
                    print()

                print("📝 SETUP INSTRUCTIONS:")
                print("1. Copy the Webhook URL above")
                print("2. Go to your Piopiy Dashboard")
                print("3. Set the webhook URL for incoming calls")
                print("4. Update your .env file with the WebSocket URL")
                print("5. Restart your backend services")
                print()

        except requests.exceptions.ConnectionError:
            print("❌ Could not connect to ngrok API. Make sure ngrok is installed and running.")
        except Exception as e:
            print(f"❌ Error checking ngrok status: {e}")

        # Keep ngrok running and monitor the child process
        print("🔄 Ngrok is running. Press Ctrl+C to stop...")
        try:
            while True:
                ret = process.poll()
                if ret is not None:
                    print(f"🛑 Ngrok process exited with code {ret}")
                    return False
                time.sleep(2)
        except KeyboardInterrupt:
            print("\n🛑 Stopping ngrok...")
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            print("✅ Ngrok stopped.")
            return True

    except FileNotFoundError:
        print("❌ ngrok command not found. Please install ngrok first:")
        print("   Visit: https://ngrok.com/download")
        return False
    except Exception as e:
        print(f"❌ Error starting ngrok: {e}")
        return False


def check_ngrok_auth():
    """Check if ngrok is authenticated"""
    try:
        result = subprocess.run(["ngrok", "config", "check"], capture_output=True, text=True)
        return result.returncode == 0
    except Exception:
        return False


def main():
    """Main entry point"""
    print("🎯 AI Agent Ngrok Tunnel Setup")
    print("=" * 40)

    # Check if ngrok is installed
    try:
        subprocess.run(["ngrok", "version"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ Ngrok is not installed or not in PATH")
        print("Please install ngrok from: https://ngrok.com/download")
        sys.exit(1)

    # Check if ngrok is authenticated
    if not check_ngrok_auth():
        print("⚠️  Ngrok may not be authenticated.")
        print("If you encounter auth errors, run: ngrok config add-authtoken YOUR_TOKEN")
        print()

    # Start ngrok
    success = start_ngrok()

    if not success:
        print("❌ Failed to start ngrok tunnels")
        sys.exit(1)


if __name__ == "__main__":
    main() 