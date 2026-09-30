"""src/audio_capture.py — 麦克风采集 (V4.2 Voice 数据输入层)。

职责: 仅负责从麦克风采集音频并写为 wav(16kHz mono), 不包含 ASR/TTS/Assistant 逻辑。
文件以时间戳命名, 不覆盖已有录音。

依赖: sounddevice(已在 requirements.txt)。
"""

from __future__ import annotations

import os
import wave
from datetime import datetime
from typing import Tuple

import numpy as np
import sounddevice as sd


def capture_audio(
    duration: float = 3.0,
    sample_rate: int = 16000,
    channels: int = 1,
    out_dir: str = "data/audio",
) -> str:
    """采集 duration 秒麦克风音频, 写为 wav, 返回路径(时间戳命名, 不覆盖)。"""
    os.makedirs(out_dir, exist_ok=True)
    frames = int(duration * sample_rate)
    data = sd.rec(frames, samplerate=sample_rate, channels=channels)
    sd.wait()
    # 归一化到 int16 单声道
    if channels == 1:
        data = data.reshape(-1)
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("int16").tobytes()

    name = datetime.now().strftime("%Y%m%d_%H%M%S") + ".wav"
    path = os.path.join(out_dir, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)   # 16-bit
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="录制麦克风音频为 wav")
    p.add_argument("--duration", type=float, default=3.0)
    a = p.parse_args()
    print(capture_audio(duration=a.duration))