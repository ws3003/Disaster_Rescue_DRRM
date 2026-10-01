"""src/voice/recorder.py — 麦克风录音到 wav。

树莓派 5 + USB 麦克风, 16kHz mono 16bit wav。
无可用输入设备时回退为静音 wav(保证无论硬件环境都能产出可播放文件, 供无麦/CI 自测)。
仅录音, 不接 ASR/TTS。
"""

from __future__ import annotations

import os
import wave
from typing import Optional

import numpy


def record(
    duration: float = 5.0,
    sample_rate: int = 16000,
    channels: int = 1,
    out_path: str = "data/audio/input.wav",
) -> str:
    """录音 duration 秒, 写为 wav, 返回路径。"""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    frames = int(duration * sample_rate)
    try:
        import sounddevice as sd  # 惰性, 避免无音频环境 import 失败

        data = sd.rec(frames, samplerate=sample_rate, channels=channels)
        sd.wait()
        pcm = (numpy.clip(data, -1.0, 1.0) * 32767).astype("int16")
    except Exception:  # noqa: BLE001 — 无输入设备/采集失败 → 静音兜底
        pcm = numpy.zeros(frames, dtype="int16")

    if channels == 1:
        pcm = pcm.reshape(-1)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return out_path