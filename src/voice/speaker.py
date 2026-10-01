"""src/voice/speaker.py — 喇叭播放接口。

树莓派: mp3 → mpg123 (edge-tts 输出), wav → aplay (录音/旧链路);
Windows: winsound(系统无额外依赖)。播放失败仅提示, 不阻塞主流程。
"""

from __future__ import annotations

import os
import shutil
import subprocess


class Speaker:
    @staticmethod
    def play(audio_path: str) -> None:
        """播放音频; 失败时打印 [TTS unavailable] 并静默返回。

        Linux 按扩展名分发: .mp3 走 mpg123, 其余(wav 等)走 aplay。
        """
        try:
            if not audio_path or not os.path.exists(audio_path):
                print("[TTS] play: no audio file")
                return
            if os.name == "nt":
                import winsound
                winsound.PlaySound(audio_path, winsound.SND_FILENAME)
            else:
                is_mp3 = audio_path.lower().endswith(".mp3")
                player = shutil.which("mpg123") if is_mp3 else shutil.which("aplay")
                if player:
                    subprocess.run([player, audio_path], check=False)
                else:
                    print(f"[TTS unavailable] no {'mpg123' if is_mp3 else 'aplay'}")
            print("[TTS] played:", audio_path)
        except Exception as exc:  # noqa: BLE001
            print("[TTS unavailable]", exc)