"""mu-law volume adjustment used for the Deepgram voice."""
import numpy as np

from voice_audio import adjust_ulaw_gain, pcm_to_ulaw, ulaw_to_pcm


def test_mulaw_round_trip_is_stable():
    data = bytes(range(256))
    assert (ulaw_to_pcm(pcm_to_ulaw(ulaw_to_pcm(data))) == ulaw_to_pcm(data)).all()  # +0 and -0 share a value


def test_gain_makes_audio_louder_without_overflow():
    tone = (8000 * np.sin(np.arange(8000) / 8000 * 2 * np.pi * 440)).astype(np.int32)
    quiet = pcm_to_ulaw(tone)
    loud = adjust_ulaw_gain(quiet, 7)
    rms = lambda b: float(np.sqrt(np.mean(ulaw_to_pcm(b).astype(np.float64) ** 2)))
    assert 1.9 < rms(loud) / rms(quiet) < 2.4          # +7 dB is about 2.2 times the level
    assert np.abs(ulaw_to_pcm(adjust_ulaw_gain(pcm_to_ulaw(tone * 4), 12))).max() <= 32124  # limited, no wrap-around


def _tone(level_dbfs, seconds=0.5):
    import numpy as np
    from voice_audio import pcm_to_ulaw
    t = np.arange(int(8000 * seconds)) / 8000
    amplitude = 32768 * 10 ** (level_dbfs / 20) * np.sqrt(2)
    return pcm_to_ulaw(amplitude * np.sin(2 * np.pi * 300 * t))


def test_level_matcher_brings_loud_and_quiet_speech_to_one_level():
    from voice_audio import LevelMatcher, _voiced_dbfs, ulaw_to_pcm
    matcher = LevelMatcher(target_dbfs=-13)
    for level in (-9, -15, -20):
        out = matcher.whole(_tone(level))
        assert abs(_voiced_dbfs(ulaw_to_pcm(out)) - (-13)) < 1.0
    matcher.start()
    streamed = b"".join(matcher.process(_tone(-18, 0.2)) for _ in range(5))
    assert abs(_voiced_dbfs(ulaw_to_pcm(streamed)) - (-13)) < 1.5


def test_level_matcher_leaves_silence_alone():
    from voice_audio import LevelMatcher
    silence = bytes([0xFF]) * 1600  # mu-law zero
    assert LevelMatcher().whole(silence) == silence
