from websocket import WebSocketApp
import websocket
import json
import threading
import os
import time
from llm.groq import chat_with_assistant
from dotenv import load_dotenv

load_dotenv()

auth_token = os.getenv("DG_API_KEY")  # Replace 'DEEPGRAM_API_KEY' with your actual authorization token
headers = {
    "Authorization": f"Token {auth_token}"
}


# WebSocket URL
ws_url = "wss://api.deepgram.com/v1/listen?sample_rate=8000&encoding=linear16&model=nova-2&language=en-IN&smart_format=true&vad_turnoff=500"
# Audio stream URL
audio_url = "http://stream.live.vc.bbcmedia.co.uk/bbc_world_service"

global ws_sales
def start_deepgram():
 # Define the WebSocket functions on_open, on_message, on_close, and on_error
 def on_open(ws):
    global wss
    print("WebSocket connection established.")
    wss = ws
    def keep_alive():
        while True:
            keep_alive_msg = json.dumps({"type": "KeepAlive"})
            ws.send(keep_alive_msg)
            time.sleep(5)
    # Start a thread for sending KeepAlive messages
    keep_alive_thread = threading.Thread(target=keep_alive)
    keep_alive_thread.daemon = True
    keep_alive_thread.start()


 def on_message(ws, message):
    try:
      response = json.loads(message)
      if response.get("type") == "Results":
          transcript = response["channel"]["alternatives"][0].get("transcript", "")
          if transcript:
              print("Transcript:", transcript)
              chat_with_assistant(transcript)
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON message: {e}")
    except KeyError as e:
        print(f"Key error: {e}")

 def on_close(ws, close_status_code, close_msg):
    print(f"WebSocket connection closed with code: {close_status_code}, message: {close_msg}")

 def on_error(ws, error):
    print("WebSocket error:", error)


# Create WebSocket connection
 ws = WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_close=on_close, on_error=on_error, header=headers)

 # Run the WebSocket
 ws_thread = threading.Thread(target=ws.run_forever)
 ws_thread.daemon = True
 ws_thread.start()



def stream_audio(stream):
  wss.send(stream,opcode=websocket.ABNF.OPCODE_BINARY)

start_deepgram()
