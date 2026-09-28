"""
src/snapshot.py — 现场视频快照保存 (Phase 2.5)
=============================================
职责: 把当前摄像头帧保存为 jpg, 供 DingTalk 图片报警携带现场视觉证据。

接口:
    save_snapshot(frame, person_id, out_dir="data/snapshots") -> path | None

设计约束:
  - 自动创建目录, jpg 压缩, 异常兜底返回 None (不阻断主推理/上传链路);
  - 文件名: YYYYMMDD_HHMMSS_person_{id}.jpg;
  - 不依赖第三方新依赖(仅 OpenCV);
  - 不触碰摄像头/感知链路, 仅在调用方已持有 frame 时复用该帧。
"""

from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

# 默认快照根目录 (相对项目根; 侧边 edge 运行时以 cwd=repo 根为准)
DEFAULT_SNAPSHOT_DIR = "data/snapshots"


def save_snapshot(
    frame,
    person_id,
    out_dir: str = DEFAULT_SNAPSHOT_DIR,
) -> Optional[str]:
    """保存单帧为 jpg, 返回绝对/相对 path (失败返回 None)。

    frame 需为 BGR numpy 数组 (cv2 兼容); person_id 用于文件名。
    异常全部兜底: 任一失败仅打印并返回 None, 绝不抛给主流程。
    """
    if frame is None or not hasattr(frame, "shape"):
        return None
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{ts}_person_{person_id}.jpg"
        path = out / name
        # 用 cv2 编码: 文件名固定 .jpg
        import cv2
        ok, buf = cv2.imencode(
            ".jpg", frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 85],
        )
        if not ok or buf is None:
            print(f"[Snapshot] encode failed for person {person_id}")
            return None
        path.write_bytes(buf.tobytes())
        return str(path)
    except Exception:  # noqa: BLE001
        print("[Snapshot] save_snapshot failed:")
        traceback.print_exc()
        return None


def _demo_main():
    """Standalone: 生成一张灰阶测试帧并保存, 验证 snapshot 可用。"""
    import numpy as np
    frame = np.zeros((720, 1280, 3), dtype="uint8")
    p = save_snapshot(frame, person_id=2)
    print("saved:", p)


if __name__ == "__main__":
    _demo_main()