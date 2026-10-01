"""src/voice/speaker.py — 喇叭播放预留接口。

树莓派: aplay; Windows: winsound(系统无额外依赖)。播放失败仅提示, 不阻塞主流程。
"""

from __future__ import annotations

import os
import shutil
import subprocess


class Speaker:
    @staticmethod
    def play(audio_path: str) -> None:
        """播放音频; 失败时打印 [TTS unavailable] 并静默返回。"""
        try:
            if not audio_path or not os.path.exists(audio_path):
                print("[TTS] play: no audio file")
                return
            if os.name == "nt":
                import winsound
                winsound.PlaySound(audio_path, winsound.SND_FILENAME)
            else:
                if shutil.which("aplay"):
                    subprocess.run(["aplay", audio_path], check=False)
                else:
                    print("[TTS unavailable] no aplay")
            print("[TTS] played:", audio_path)
        except Exception as exc:  # noqa: BLE001
            print("[TTS unavailable]", exc)