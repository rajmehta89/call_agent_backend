import requests
import base64
import numpy as np
import time
from groq import Groq
from config import Config
from real_estate_data import REAL_ESTATE_INFO

class AIServices:
    def __init__(self):
        self.config = Config()
        
        # Debug API key configuration
        print(f"Initializing AIServices...")
        print(f"GROQ_API_KEY present: {'Yes' if self.config.GROQ_API_KEY else 'No'}")
        if self.config.GROQ_API_KEY:
            print(f"GROQ_API_KEY length: {len(self.config.GROQ_API_KEY)}")
            print(f"GROQ_API_KEY starts with: {self.config.GROQ_API_KEY[:10]}...")
        else:
            print("GROQ_API_KEY is None or empty")
        
        try:
            self.groq_client = Groq(api_key=self.config.GROQ_API_KEY)
            print("Groq client initialized successfully")
        except Exception as e:
            print(f"Failed to initialize Groq client: {e}")
            self.groq_client = None
    
    def transcribe_audio(self, audio_data, attempt=1):
        """Transcribe audio using Google STT with retry logic"""
        try:
            # Normalize audio
            if np.max(np.abs(audio_data)) > 0:
                audio_data = audio_data / np.max(np.abs(audio_data)) * 16384
            
            audio_bytes = audio_data.astype(np.int16).tobytes()
            audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
            
            request_body = {
                "config": {
                    "encoding": "LINEAR16",
                    "sampleRateHertz": self.config.SAMPLE_RATE,
                    "languageCode": "en-US",
                    "enableAutomaticPunctuation": False,
                    "model": "command_and_search",
                    "useEnhanced": False
                },
                "audio": {"content": audio_b64}
            }
            
            response = requests.post(
                self.config.STT_URL,
                headers={"Content-Type": "application/json"},
                json=request_body,
                timeout=3
            )
            
            if response.status_code == 200:
                result = response.json()
                results = result.get('results', [])
                if results and results[0].get('alternatives'):
                    alternatives = results[0]['alternatives']
                    if alternatives and 'transcript' in alternatives[0]:
                        transcript = alternatives[0]['transcript'].strip()
                        print(f"You: {transcript}")
                        return transcript
                
                print(" STT returned no transcript.")
                return ""
            else:
                print(f"STT HTTP {response.status_code}")
                return ""
                
        except Exception as e:
            if attempt < 3:
                print(f" STT retry {attempt}/3...")
                time.sleep(0.1 * attempt)
                return self.transcribe_audio(audio_data, attempt + 1)
            print(f"STT fatal error: {e}")
            return ""
    
    def get_llm_response(self, user_input, conversation_history):
        """Get response from Groq LLM, strictly using real estate data only"""
        print("Thinking...")
        try:
            # Add user input to history
            conversation_history.append({"role": "user", "content": user_input})
            
            # Trim history
            if len(conversation_history) > self.config.MAX_CONVERSATION_HISTORY:
                conversation_history = conversation_history[-self.config.MAX_CONVERSATION_HISTORY:]
            
            # Strict system prompt with data context
            developer_name = REAL_ESTATE_INFO.get("developer", "Our Real Estate Developers")
            system_prompt = (
                f"You are a real estate assistant for {developer_name}. "
                f"You must only answer questions using the following data: {REAL_ESTATE_INFO}. "
                f"If a question is not related to these projects, amenities, or payment plans, politely reply: "
                f"'Sorry, I can only answer questions about {developer_name} and their projects.'"
            )

            messages = [
                {"role": "system", "content": system_prompt},
                *conversation_history[-4:]  # Last 2 exchanges
            ]
            
            response = self.groq_client.chat.completions.create(
                messages=messages,
                model="llama-3.3-70b-versatile",
                max_tokens=self.config.MAX_TOKENS,
                temperature=self.config.TEMPERATURE,
                top_p=0.9,
                stream=False
            )
            
            ai_text = response.choices[0].message.content
            conversation_history.append({"role": "assistant", "content": ai_text})
            
            print(f"Bot: {ai_text}")
            return ai_text
            
        except Exception as e:
            print(f"Groq Error: {e}")
            return "Sorry, can you repeat that?"
    
    def text_to_speech(self, text):
        """Convert text to speech using Google TTS"""
        if not text.strip():
            return None
        
        print("Speaking...")
        try:
            request_body = {
                "input": {"text": text},
                "voice": {
                    "languageCode": "en-US",
                    "name": "en-US-Standard-D",
                    "ssmlGender": "MALE"
                },
                "audioConfig": {
                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": self.config.SAMPLE_RATE,
                    "speakingRate": 1.2
                }
            }
            
            response = requests.post(
                self.config.TTS_URL,
                headers={"Content-Type": "application/json"},
                json=request_body,
                timeout=5
            )
            
            if response.status_code == 200:
                result = response.json()
                if 'audioContent' in result:
                    audio_content = base64.b64decode(result['audioContent'])
                    
                    # Save audio file
                    filename = "bot_response.wav"
                    with open(filename, "wb") as f:
                        f.write(audio_content)
                    
                    return filename
            
            print(f"TTS Error: {response.status_code}")
            return None
            
        except Exception as e:
            print(f"TTS Error: {e}")
            return None
