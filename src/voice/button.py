"""src/voice/button.py — GPIO 按键输入。

树莓派 5 (Python3.11): 使用 RPi.GPIO(Bookworm 下为 rpi-lgpio 提供) 等待按下。
无 GPIO 环境(Windows/虚拟/CI): 回退为"模拟按键"(稍候自动触发), 便于无硬件测试。
仅输出"是否按下"事件; 不接 ASR/TTS/录音本身。
"""

from __future__ import annotations

import time
from typing import Optional


class Button:
    """等待一次按键。真机走 GPIO 上升/下降沿; 无 GPIO 走模拟。"""

    def __init__(self, pin: int = 17, simulate_on_error: bool = True,
                 simulate_delay: float = 2.0):
        self.pin = pin
        self._gpio = None
        try:
            # 树莓派(Bookworm) RPi.GPIO 由 rpi-lgpio 提供; 失败则模拟
            # noinspection PyPackageRequirements
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self._gpio = GPIO
        except Exception:  # noqa: BLE001 — 非树莓派
            self._gpio = None
        self._simulate_on_error = simulate_on_error
        self._simulate_delay = simulate_delay

    @property
    def has_hardware(self) -> bool:
        return self._gpio is not None

    def wait_press(self, timeout: Optional[float] = None) -> bool:
        """阻塞等待一次按键按下; 返回 True 表示触发。"""
        if self._gpio is not None:
            ms = int(timeout * 1000) if timeout is not None else None
            edge = (self._gpio.wait_for_edge(self.pin, self._gpio.FALLING,
                                             bouncetime=200, timeout=ms)
                    if ms else
                    self._gpio.wait_for_edge(self.pin, self._gpio.FALLING, bouncetime=200))
            return edge is not None
        # 无 GPIO → 模拟
        if self._simulate_on_error:
            print(f"[Button] 无 GPIO(非树莓派), {self._simulate_delay} 秒后模拟按下…")
            time.sleep(self._simulate_delay)
            return True
        return False


# 别名: VoiceButton 指向 Button(接口: wait_press() -> True)
VoiceButton = Button