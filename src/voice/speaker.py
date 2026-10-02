"""src/voice/speaker.py

树莓派音频播放接口。

支持:
- mp3 -> mpg123
- wav -> aplay

Pi 5 固定使用 USB 声卡:
plughw:2,0

播放失败不影响主流程。
"""

from __future__ import annotations

import os
import shutil
import subprocess


class Speaker:

    @staticmethod
    def play(audio_path: str) -> None:
        """
        播放音频。

        Linux:
            wav -> aplay -D plughw:2,0
            mp3 -> mpg123

        Windows:
            winsound
        """

        try:

            if not audio_path or not os.path.exists(audio_path):
                print("[TTS] play: no audio file")
                return


            # Windows
            if os.name == "nt":

                import winsound

                winsound.PlaySound(
                    audio_path,
                    winsound.SND_FILENAME
                )

                print("[TTS] played:", audio_path)
                return



            # Linux / Raspberry Pi

            is_mp3 = audio_path.lower().endswith(".mp3")


            if is_mp3:

                player = shutil.which("mpg123")

                if player:

                    subprocess.run(
                        [
                            player,
                            audio_path
                        ],
                        check=False
                    )

                else:

                    print(
                        "[TTS unavailable] mpg123 not found"
                    )


            else:

                player = shutil.which("aplay")


                if player:

                    subprocess.run(
                        [
                            player,
                            "-D",
                            "plughw:2,0",
                            audio_path
                        ],
                        check=False
                    )

                else:

                    print(
                        "[TTS unavailable] aplay not found"
                    )


            print("[TTS] played:", audio_path)



        except Exception as exc:

            print(
                "[TTS unavailable]",
                exc
            )
