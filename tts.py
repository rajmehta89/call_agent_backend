"""
tts.py  –  Text-to-Speech helper for Piopiy calls
• Uses Deepgram Speak because we already have a DG key.
• Returns raw 8 kHz μ-law bytes ready for Piopiy.
"""

import os, json, aiohttp, asyncio

DG_KEY = os.getenv("DG_API_KEY")
if not DG_KEY:
    raise RuntimeError("DG_API_KEY missing in environment")

# Deepgram supports several English voices. Pick one you like.
VOICE_MODEL = "aura-asteria-en"      # glossy female
SAMPLE_RATE = 8000                   # matches Piopiy μ-law stream

async def text_to_mulaw(text: str) -> bytes:
    """
    Async → returns μ-law PCM bytes (8 kHz).
    Raise RuntimeError on any non-200 response.
    """
    url = f"https://api.deepgram.com/v1/speak?model={VOICE_MODEL}"
    hdr = {
        "Authorization": f"Token {DG_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "text": text,
        "encoding": "mulaw",
        "sample_rate": SAMPLE_RATE,
        "utterance_id": "agent-reply"
    }

    async with aiohttp.ClientSession() as ses:
        async with ses.post(url, headers=hdr, data=json.dumps(body)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TTS error {resp.status}: "
                                   f"{await resp.text()}")
            return await resp.read()
