# utils.py
import audioop

def pcm16_to_ulaw_8000(raw_linear16: bytes, in_rate: int) -> bytes:
    """
    Down-samples 16-bit little-endian PCM to 8 kHz μ-law (1-byte samples).
    """
    if in_rate != 8_000:
        raw_linear16, _ = audioop.ratecv(
            raw_linear16, 2, 1, in_rate, 8_000, None
        )
    return audioop.lin2ulaw(raw_linear16, 2)
