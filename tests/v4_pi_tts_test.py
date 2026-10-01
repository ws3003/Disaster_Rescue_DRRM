from pathlib import Path

from src.tools.voice_tool import VoiceTool


def test_pi_edge_tts():

    tool = VoiceTool()

    result = tool.text_to_audio(
        text="当前检测到一名人员，状态为倒地，风险等级高"
    )

    print(result)

    assert result["ok"] is True
    assert result["provider"] == "edge"

    audio = result.get("audio_path") or result.get("audio_url") or result.get("audio_url")

    assert audio is not None

    path = Path(audio)

    assert path.exists()
    assert path.stat().st_size > 0


if __name__ == "__main__":
    test_pi_edge_tts()
    print("PI EDGE TTS OK")
