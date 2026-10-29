"""X4/E6 服务端 YOLO 推理适配器（`src/x4/model_runner.py`）。

把 Ultralytics YOLO 加载/推理封装为可注入、懒加载、可测试的 runner：
- 只接受服务端配置好的权重路径（client 不传路径）；白名单由 `server/x4_predict` 解析决定。
- **懒加载**：构造/import/普通测试不加载真实权重；首次 `run()` 才 `YOLO(path)` 并缓存实例。
- 输入为 OpenCV BGR ndarray；输出为符合 X4 `Detection` 契约的 `Tuple[Detection,...]`。
- 从真实模型读取 `boxes.xyxy/.cls/.conf` 与 `names` 映射（类别名不硬编码）。
- 无目标 → 空元组；加载/推理/输出非法 → 抛 `ModelRunnerError`（不得当作空检测）。
"""

from __future__ import annotations

import math
from typing import Any, Optional, Tuple

from src.x4.contracts import Detection


class ModelRunnerError(Exception):
    """模型加载/推理/输出非法（服务端层内部；外层负责映射为 5xx 且不外泄细节）。"""


class ModelRunner:
    def __init__(self, weights_path: str, *, conf: float = 0.25, imgsz: int = 640):
        if not isinstance(weights_path, str) or not weights_path.strip():
            raise ModelRunnerError("weights_path 必须为非空字符串")
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
                or not math.isfinite(float(conf)) or not (0.0 < float(conf) <= 1.0):
            raise ModelRunnerError(f"conf 必须为 (0,1], 得到 {conf!r}")
        if isinstance(imgsz, bool) or not isinstance(imgsz, int) or imgsz <= 0:
            raise ModelRunnerError(f"imgsz 必须为正整数, 得到 {imgsz!r}")
        self.weights_path = weights_path.strip()
        self.conf = float(conf)
        self.imgsz = int(imgsz)
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from ultralytics import YOLO
            self._model = YOLO(self.weights_path)
        except Exception as exc:  # noqa: BLE001
            raise ModelRunnerError(f"model load failed: {type(exc).__name__}") from exc
        return self._model

    def run(self, image_bgr: Any) -> Tuple[Detection, ...]:
        """对 BGR 图像推理，返回 X4 Detection 元组（无目标→空）。加载/推理/非法输出→抛 ModelRunnerError。"""
        if not hasattr(image_bgr, "shape") or getattr(image_bgr, "ndim", 0) != 3:
            raise ModelRunnerError("invalid image: 需为 3 维 BGR ndarray")
        model = self._load()
        try:
            results = model(image_bgr, conf=self.conf, imgsz=self.imgsz)
        except Exception as exc:  # noqa: BLE001
            raise ModelRunnerError(f"inference failed: {type(exc).__name__}") from exc
        if not results or not getattr(results[0], "boxes", None):
            return ()
        boxes = getattr(results[0].boxes, "xyxy", None)
        cls_ids = getattr(results[0].boxes, "cls", None)
        confs = getattr(results[0].boxes, "conf", None)
        names = getattr(results[0], "names", None)
        if boxes is None or cls_ids is None or confs is None:
            return ()
        names = names if isinstance(names, dict) else {}
        import numpy as np
        boxes = _as_np(boxes)
        cls_ids = _as_np(cls_ids).astype(int).tolist()
        confs = _as_np(confs)
        out: list[Detection] = []
        for (x1, y1, x2, y2), cid, c in zip(boxes, cls_ids, confs):
            conf = float(c)
            bbox = (float(x1), float(y1), float(x2), float(y2))
            if not (isinstance(cid, int) and math.isfinite(conf) and 0.0 <= conf <= 1.0
                    and all(math.isfinite(v) for v in bbox)):
                raise ModelRunnerError("invalid model output")
            out.append(Detection(
                class_id=int(cid),
                class_name=str(names.get(int(cid), str(int(cid)))),
                confidence=conf,
                bbox=bbox,
            ))
        return tuple(out)


def _as_np(x: Any):
    import numpy as np
    return x.cpu().numpy() if hasattr(x, "cpu") and hasattr(x, "numpy") else np.asarray(x)