"""Phone-audio helpers: G.711 mu-law (8 kHz) volume adjustment for text-to-speech that comes out too quiet.

Deepgram Aura-2 voices measure about 5-8 dB quieter than ElevenLabs on phone audio, so callers heard them as
"too low". adjust_ulaw_gain() decodes the mu-law bytes, applies the gain with a soft limiter (no harsh clipping
on loud syllables) and re-encodes. Pure numpy; about a millisecond per second of audio."""

from __future__ import annotations

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(sign != 0, -magnitude, magnitude).astype(np.int32)


_DECODE = _decode_table()
_EXPONENT = np.array([0, 0] + [int(np.log2(value)) for value in range(2, 256)], dtype=np.int32).clip(0, 7)


def ulaw_to_pcm(data: bytes) -> np.ndarray:
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm_to_ulaw(samples: np.ndarray) -> bytes:
    samples = samples.astype(np.int32)
    sign = (samples < 0).astype(np.int32) << 7
    magnitude = np.minimum(np.abs(samples), _CLIP) + _BIAS
    exponent = _EXPONENT[(magnitude >> 7) & 0xFF]
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def adjust_ulaw_gain(data: bytes, gain_db: float) -> bytes:
    """Louder (or quieter) mu-law audio; a tanh soft limiter keeps peaks below full scale."""
    if not data or abs(gain_db) < 0.1:
        return data
    pcm = ulaw_to_pcm(data).astype(np.float64) * (10 ** (gain_db / 20))
    limited = 32000.0 * np.tanh(pcm / 32000.0)
    return pcm_to_ulaw(np.round(limited))


def _voiced_dbfs(pcm: np.ndarray) -> float | None:
    """Loudness of the speech in a piece of audio (pauses left out), in dB below full scale; None if it is mostly silence."""
    voiced = pcm[np.abs(pcm) > 300].astype(np.float64)
    if len(voiced) < 0.2 * max(len(pcm), 1):
        return None
    return 20 * np.log10(max(np.sqrt(np.mean(voiced ** 2)), 1.0) / 32768)


class LevelMatcher:
    """Keeps the agent's voice at one steady loudness for the whole call.

    Text-to-speech engines make every sentence separately, and their loudness differs from one to the next (ElevenLabs:
    "Yeah." at -9 dBFS, "Sure." at -15, replies -11 to -14), so callers hear the voice jump up and down. Each spoken
    piece is brought to the target level: stored clips (greeting, fillers) with one gain for the whole clip; streamed
    replies with a gain taken from their first moments and then adjusted gently, so the voice's natural rise and fall
    within a sentence stays."""

    def __init__(self, target_dbfs: float = -13.0, max_gain_db: float = 8.0) -> None:
        self.target = target_dbfs
        self.max_gain = max_gain_db
        self.gain: float | None = None

    def _wanted(self, level: float) -> float:
        return float(np.clip(self.target - level, -self.max_gain, self.max_gain))

    def start(self) -> None:
        """A new piece of speech begins: measure it afresh."""
        self.gain = None

    def whole(self, data: bytes) -> bytes:
        level = _voiced_dbfs(ulaw_to_pcm(data))
        return data if level is None else adjust_ulaw_gain(data, self._wanted(level))

    def process(self, chunk: bytes) -> bytes:
        level = _voiced_dbfs(ulaw_to_pcm(chunk))
        if level is not None:
            wanted = self._wanted(level)
            self.gain = wanted if self.gain is None else self.gain + 0.2 * (wanted - self.gain)
        return adjust_ulaw_gain(chunk, self.gain or 0.0)
