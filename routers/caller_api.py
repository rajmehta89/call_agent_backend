import os
from piopiy import RestClient, Action
from dotenv import load_dotenv

# Load environment variables from your .env file
load_dotenv()

# --- Configuration ---
APP_ID = os.getenv("APP_ID")
APP_SECRET = os.getenv("APP_SECRET")
# This is your Piopiy virtual number
CALLER_ID = os.getenv("CALLER_ID")
# This is the public URL for your WebSocket server from your tunnel
WEBSOCKET_URL = os.getenv("WEBSOCKET_URL")

def clean_phone_number(phone_str):
    """Removes '+' and spaces, then converts to integer."""
    if isinstance(phone_str, str):
        return int(phone_str.replace('+', '').replace('-', '').replace(' ', ''))
    return phone_str

class OutboundCaller:
    def __init__(self):
        # Ensure all required variables are present
        if not all([APP_ID, APP_SECRET, CALLER_ID, WEBSOCKET_URL]):
            raise ValueError("One or more required environment variables are missing.")

        self.client = RestClient(int(APP_ID), APP_SECRET)
        print(f"🔧 Outbound caller initialized.")
        print(f"   - Caller ID: {CALLER_ID}")
        print(f"   - WebSocket URL: {WEBSOCKET_URL}")

    def make_call(self, customer_number_str):
        """
        Initiates an outbound call and connects it to the WebSocket voice agent.
        """
        try:
            customer_number = clean_phone_number(customer_number_str)
            piopiy_number = clean_phone_number(CALLER_ID)

            action = Action()

            # The ONLY action is to stream the call to the WebSocket server.
            # The WebSocket server will handle the greeting.
            action.stream(
                WEBSOCKET_URL,
                {
                    "listen_mode": "callee", # Listen to the person who receives the call
                    "stream_on_answer": True
                }
            )

            print(f"\n📞 Placing call to {customer_number_str}...")

            # Make the API call to Piopiy
            response = self.client.voice.call(
                to=customer_number,
                piopiy_no=piopiy_number,
                to_or_array_pcmo=action.PCMO(),
                options={'record': True} # Optional: record the call
            )

            print("✅ Call initiated successfully!")
            print("   - Response from Piopiy:", response)
            return response

        except Exception as e:
            print(f"❌ Failed to make call: {e}")
            return None

# Global instance for router access
outbound_caller = None

def get_outbound_caller():
    """Get or create outbound caller instance"""
    global outbound_caller
    if outbound_caller is None:
        try:
            outbound_caller = OutboundCaller()
        except Exception as e:
            print(f"Failed to initialize outbound caller: {e}")
            return None
    return outbound_caller

# --- Example Usage ---
if __name__ == "__main__":
    # Ensure your .env file is set up with:
    # APP_ID, APP_SECRET, PIOPIY_NUMBER, and the correct public WEBSOCKET_URL

    caller = OutboundCaller()

    # Replace with the number you want to call
    customer_to_call = "+919898831184"

    caller.make_call(customer_to_call)
