"""E6-01 云端选帧器（`src/x4/frame_selector.py`）。

- 仅控制 CLOUD_PREFERRED 下的普通云端复核请求频率：按 `interval_seconds` 单调时钟窗口，
  窗口内至多向云发送一帧。
- `enabled=false`（默认）→ `should_send()` 恒 True，等价"不选帧、每帧云"（与现版本一致）。
- 不参与熔断/冷却/恢复探测：探测路径在帧循环外直接 `cloud.infer`，不经本选帧器。
"""

from __future__ import annotations

import time
from typing import Any, Callable


class FrameSelector:
    """按固定时间间隔选出向云发送的帧（单调时钟；now 可注入以便测试）。"""

    def __init__(
        self,
        *,
        enabled: bool,
        interval_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError(f"frame_selection.enabled 必须为 bool, 得到 {enabled!r}")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) \
                or float(interval_seconds) <= 0:
            raise ValueError(f"frame_selection.interval_seconds 必须为正数, 得到 {interval_seconds!r}")
        self.enabled = enabled
        self.interval = float(interval_seconds)
        self._now = now
        self._next = 0.0

    def should_send(self, frame: Any = None) -> bool:
        """返回本帧是否应请求云端。仅当 enabled 且已过窗口才 True。"""
        if not self.enabled:
            return True
        t = self._now()
        if t < self._next:
            return False
        self._next = t + self.interval
        return True