import os
from dotenv import load_dotenv
from real_estate_data import REAL_ESTATE_INFO

# Load environment variables from .env
load_dotenv()

# === Backend service hosts/ports ===
BACKEND_HOST = os.getenv("BACKEND_HOST", "0.0.0.0")

# === Frontend allowed origins (CORS) ===
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
FRONTEND_ORIGIN_ALT = os.getenv("FRONTEND_ORIGIN_ALT", "http://localhost:3001")
ALLOWED_ORIGINS = [
    FRONTEND_ORIGIN,
    FRONTEND_ORIGIN_ALT,
    os.getenv("FRONTEND_ORIGIN_2", "http://127.0.0.1:3000"),
    os.getenv("FRONTEND_ORIGIN_3", "http://127.0.0.1:3001"),
]

# === API Ports ===
CONFIG_API_PORT = int(os.getenv("CONFIG_API_PORT", 5001))
LEADS_API_PORT = int(os.getenv("LEADS_API_PORT", 5002))
CALLS_API_PORT = int(os.getenv("CALLS_API_PORT", 5004))
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 3001))
WEBSOCKET_PORT = int(os.getenv("WEBSOCKET_PORT", 8765))

# === Ngrok API (for local dev only) ===
NGROK_API = os.getenv("NGROK_API", "http://localhost:4040")


class Config:
    """Central configuration for AI Real Estate Assistant"""

    # === API Keys ===
    GOOGLE_TTS_API_KEY = os.getenv("GOOGLE_TTS_API_KEY")
    GOOGLE_STT_API_KEY = os.getenv("GOOGLE_STT_API_KEY")
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")

    # === Audio Settings ===
    SAMPLE_RATE = 16000
    CHANNELS = 1
    CHUNK_SIZE = 1024

    # === Voice Activity Detection ===
    VAD_THRESHOLD = 0.003
    INTERRUPT_THRESHOLD = 0.2
    SILENCE_DURATION = 0.8
    MIN_SPEECH_DURATION = 0.5

    # === AI Model Settings ===
    MAX_CONVERSATION_HISTORY = 6
    MAX_TOKENS = 50
    TEMPERATURE = 0.5

    # === Real Estate Bot Behavior Flags ===
    USE_KNOWLEDGE_BASE_ONLY = False  # Only answer from real estate data if True
    FALLBACK_TO_LLM = False  # Use Groq as fallback if True
    USE_CONSTRAINED_LLM = True  # Keep Groq responses restricted to given data

    # === AI System Prompt ===
    SYSTEM_PROMPT = (
        f"You are a real estate assistant for {REAL_ESTATE_INFO['developer']}. "
        f"You must only answer questions using the following data: {REAL_ESTATE_INFO}. "
        f"If a question is not related to {REAL_ESTATE_INFO['developer']}'s projects, amenities, or payment plans, "
        f"politely reply: 'Sorry, I can only answer questions about {REAL_ESTATE_INFO['developer']} and their projects.'"
    )

    # === API Endpoints ===
    @property
    def TTS_URL(self):
        """Google Text-to-Speech API URL"""
        return f"https://texttospeech.googleapis.com/v1/text:synthesize?key={self.GOOGLE_TTS_API_KEY}"

    @property
    def STT_URL(self):
        """Google Speech-to-Text API URL"""
        return f"https://speech.googleapis.com/v1/speech:recognize?key={self.GOOGLE_STT_API_KEY}"


# Debug check when running directly
if __name__ == "__main__":
    print("✅ Config loaded successfully")
    print(f"Allowed Origins: {ALLOWED_ORIGINS}")
    print(f"Google TTS API Key: {'✔ Loaded' if Config.GOOGLE_TTS_API_KEY else '❌ Missing'}")
    print(f"Google STT API Key: {'✔ Loaded' if Config.GOOGLE_STT_API_KEY else '❌ Missing'}")
    print(f"GROQ API Key: {'✔ Loaded' if Config.GROQ_API_KEY else '❌ Missing'}")
