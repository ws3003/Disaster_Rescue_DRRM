"""
Detection layer — YOLO person detection (DRRM V0.1, Step 2)
===========================================================
职责: 对单帧图像做 YOLO 目标检测, 输出结构化检测结果, 并区分 person / non-person。

范围约束 (V0.1):
  - 仅实现检测层; 不实现 MediaPipe / 状态识别 / Fall / Lying / Help-Waving /
    DRRM / 救援排序 / 可视化。
  - 仅从 config/config.yaml 读取参数, 不硬编码路径 / 设备 / 类别。
  - 设备选择 CPU/GPU 可切换; 不把 RTX 4060 写死在代码里。
  - 使用 models/disaster_best.pt, 严禁重新下载 yolo26n.pt 或改用其他模型。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List

import cv2
import numpy as np
import yaml
from ultralytics import YOLO


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Detection:
    """单条检测结果。"""

    bbox: List[float]          # [x1, y1, x2, y2] 像素坐标
    class_id: int
    class_name: str
    confidence: float
    is_person: bool            # persons 类别(1=civilian, 5=rescuer) => True


def load_config(config_path: str | Path) -> dict:
    """加载 config/config.yaml。"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_device(cfg: dict) -> str:
    """
    设备选择 (PRD + V0.1 约定):
      - device.force 若指定则优先使用 (如 "cpu" / "0")；
      - 否则按 device.mode:
          auto: CUDA 可用则 "cuda", 否则 "cpu"
          cpu : "cpu"
          cuda: 强制 "cuda"
    不把具体 GPU 型号写死; 由 torch 侧探测或超纳 YOLO 的 device 参数处理。
    """
    device_cfg = cfg.get("device", {})
    force = device_cfg.get("force")
    if force:  # 显式指定 (可为 "cpu" / "cuda" / "0" / "0,1" 等)
        return str(force)

    mode = device_cfg.get("mode", "auto")
    if mode == "cpu":
        return "cpu"
    if mode == "cuda":
        return "cuda"
    # auto
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class Detector:
    """YOLO 检测器。"""

    def __init__(self, config_path: str | Path | None = None):
        cfg_path = Path(config_path) if config_path else Path("config/config.yaml")
        self.cfg = load_config(cfg_path)

        model_cfg = self.cfg["model"]
        self.conf_thres = float(model_cfg["conf_thres"])
        self.iou_thres = float(model_cfg["iou_thres"])
        self.imgsz = int(model_cfg["imgsz"])

        names = self.cfg["classes"]["names"]
        self.names = {int(k): v for k, v in names.items()}
        self.person_ids = set(int(c) for c in self.cfg["classes"]["persons"])
        self.non_person_ids = set(int(c) for c in self.cfg["classes"]["non_persons"])

        self.device = resolve_device(self.cfg)
        self.model_path = Path(self.cfg["model"]["path"])

        # 加载本地模型 (禁止重新下载)
        self.model = YOLO(str(self.model_path))

    # ------------------------------------------------------------------
    # 检测
    # ------------------------------------------------------------------
    def detect_frame(
        self,
        image_bgr: np.ndarray,
        conf_thres: float | None = None,
        iou_thres: float | None = None,
    ) -> List[Detection]:
        """
        对单帧 BGR 图像做 YOLO 检测。

        参数:
            image_bgr: OpenCV/BGR 图像 (H, W, 3), uint8
        """
        conf = conf_thres if conf_thres is not None else self.conf_thres
        iou = iou_thres if iou_thres is not None else self.iou_thres

        results = self.model.predict(
            source=image_bgr,
            conf=conf,
            iou=iou,
            imgsz=self.imgsz,
            device=self.device,
            verbose=False,
        )
        r = results[0]

        detections: List[Detection] = []
        # r.boxes.xyxy - Nx4 float32; cls - Nx1; conf - Nx1
        if r.boxes is None or r.boxes.xyxy is None:
            return detections

        xyxy = r.boxes.xyxy.cpu().numpy()
        cls_ids = r.boxes.cls.cpu().numpy().astype(int)
        confs = r.boxes.conf.cpu().numpy()

        for (x1, y1, x2, y2), cid, c in zip(xyxy, cls_ids, confs):
            detections.append(
                Detection(
                    bbox=[float(x1), float(y1), float(x2), float(y2)],
                    class_id=int(cid),
                    class_name=self.names.get(int(cid), str(cid)),
                    confidence=float(c),
                    is_person=bool(int(cid) in self.person_ids),
                )
            )
        return detections


# ---------------------------------------------------------------------------
# 最小验证 (Step 2 验收): 用 data/test.jpg 冒烟运行
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    img_path = sys.argv[1] if len(sys.argv) > 1 else "data/test.jpg"
    det = Detector("config/config.yaml")
    img = cv2.imread(img_path)
    if img is None:
        raise SystemExit(f"无法读取图像: {img_path}")

    print(f"[device] {det.device}")
    result = det.detect_frame(img)

    persons = [d for d in result if d.is_person]
    non_persons = [d for d in result if not d.is_person]
    print(f"[detections] total={len(result)}  persons={len(persons)}  non_persons={len(non_persons)}")
    for d in result:
        flag = "PERSON" if d.is_person else "non-person"
        print(f"  [{flag}] {d.class_name:8s} conf={d.confidence:.3f} "
              f"bbox=({d.bbox[0]:.0f},{d.bbox[1]:.0f},{d.bbox[2]:.0f},{d.bbox[3]:.0f})")