"""
Pose layer — MediaPipe per-person pose estimation (DRRM V0.1, Step 3)
===================================================================
职责: 对 detector.py 输出的人员 Detection 逐个做 MediaPipe 姿态估计, 输出 33 关键点,
     并把 ROI 归一化坐标转回原始图像坐标, 供后续 state.py 做几何计算。

范围约束 (V0.1):
  - 仅对人员类别 (civilian/rescuer) 做姿态, 非人员不处理。
  - 单帧静态图片推理: static_image_mode=True, smooth_landmarks=False (由 config 决定)。
  - 不实现 Standing/Sitting/Lying/Fall/Help-Waving/DRRM/救援排序。
  - 不加入视频/摄像头/跟踪/时序逻辑。
  - 只依赖当前已装且实测可用的 MediaPipe 0.10.9 (无需额外模型文件)。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from mediapipe.python.solutions import pose as mp_pose

from detector import Detection, load_config


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Landmark:
    """单个关键点, 坐标已映射回原始图像像素系。"""

    x: float            # 原图像素 x
    y: float            # 原图像素 y
    z: float            # MediaPipe 归一化深度(相对), 保留原始值
    visibility: float   # 关键点可见度 [0,1]


@dataclass
class PoseResult:
    """单个人员的姿态估计结果。"""

    detection: Detection                 # 对应检测
    status: str                          # ok | skipped | no_landmarks | error
    roi: List[int]                       # 实际使用的 ROI [x1,y1,x2,y2] (原图坐标)
    landmarks: Optional[List[Landmark]]  # 33 个关键点; 非 ok 时为 None
    skip_reason: str = ""                # skipped 时的原因
    visibility_mean: float = 0.0         # 关键点可见度均值 (供后续证据因子参考)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


class PoseEstimator:
    """基于 MediaPipe 的按 ROI 单人姿态估计。"""

    def __init__(self, config_path: str | Path | None = None):
        cfg_path = Path(config_path) if config_path else Path("config/config.yaml")
        self.cfg = load_config(cfg_path)

        pcfg = self.cfg["pose"]
        self.roi_pad_ratio = float(pcfg["roi_pad_ratio"])
        self.min_person_box_h = int(pcfg["min_person_box_h"])

        # MediaPipe Pose 实例 (全部参数来自 config, 静态图片模式)
        self._pose = mp_pose.Pose(
            static_image_mode=bool(pcfg["static_image_mode"]),
            model_complexity=int(pcfg["model_complexity"]),
            smooth_landmarks=bool(pcfg["smooth_landmarks"]),
            min_detection_confidence=float(pcfg["min_detection_confidence"]),
            min_tracking_confidence=float(pcfg["min_tracking_confidence"]),
        )

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def estimate_persons(self, image_bgr: np.ndarray, detections: List[Detection]) -> List[PoseResult]:
        """
        对一批检测结果中的人员逐个做姿态估计。

        参数:
            image_bgr: 原始 BGR 图像 (H, W, 3)
            detections: detector.py 输出的检测列表
        """
        results: List[PoseResult] = []
        for det in detections:
            if not det.is_person:
                continue  # 只对人员做姿态
            results.append(self._estimate_single(image_bgr, det))
        return results

    # ------------------------------------------------------------------
    # 单人多步实现
    # ------------------------------------------------------------------
    def _estimate_single(self, image_bgr: np.ndarray, detection: Detection) -> PoseResult:
        H, W = image_bgr.shape[:2]
        x1, y1, x2, y2 = [float(v) for v in detection.bbox]
        bh = y2 - y1

        # 1) 肢体过小则跳过, 明确记录原因, 不崩溃
        if bh < self.min_person_box_h:
            return PoseResult(
                detection=detection,
                status="skipped",
                roi=[int(x1), int(y1), int(x2), int(y2)],
                landmarks=None,
                skip_reason=f"bbox_h={bh:.1f} < min_person_box_h={self.min_person_box_h}",
            )

        # 2) ROI 向外扩展, 防止头部/脚部关键点被 bbox 切掉
        pad = self.roi_pad_ratio * bh
        rx1, ry1 = x1 - pad, y1 - pad
        rx2, ry2 = x2 + pad, y2 + 3 * pad  # 脚部方向多留, 防踩空

        # 3) ROI 边界裁剪, 不得越界
        rx1, ry1 = max(0, int(rx1)), max(0, int(ry1))
        rx2, ry2 = min(W, int(rx2)), min(H, int(ry2))
        roi = [rx1, ry1, rx2, ry2]

        if rx2 <= rx1 or ry2 <= ry1:
            return PoseResult(
                detection=detection,
                status="skipped",
                roi=roi,
                landmarks=None,
                skip_reason=f"empty ROI after clamp: {roi}",
            )

        # 4) 裁剪并转 RGB + 调用 MediaPipe 单人推理
        roi_img = image_bgr[ry1:ry2, rx1:rx2]
        try:
            rgb = cv2.cvtColor(roi_img, cv2.COLOR_BGR2RGB)
            mp_res = self._pose.process(rgb)
        except Exception as e:  # 单人不因异常拖垮整个管线
            return PoseResult(
                detection=detection,
                status="error",
                roi=roi,
                landmarks=None,
                skip_reason=f"mediapipe exception: {e}",
            )

        # 5) 关键点归一化坐标 -> 原图坐标
        if mp_res.pose_landmarks is None:
            return PoseResult(
                detection=detection,
                status="no_landmarks",
                roi=roi,
                landmarks=None,
                skip_reason="no pose_landmarks in ROI",
            )

        roi_w = rx2 - rx1
        roi_h = ry2 - ry1
        raw = mp_res.pose_landmarks.landmark
        landmarks: List[Landmark] = []
        vis_sum = 0.0
        for lm in raw:
            px = rx1 + lm.x * roi_w  # 归一化 x -> 原图像素
            py = ry1 + lm.y * roi_h  # 归一化 y -> 原图像素
            landmarks.append(
                Landmark(x=float(px), y=float(py), z=float(lm.z), visibility=float(lm.visibility))
            )
            vis_sum += float(lm.visibility)

        return PoseResult(
            detection=detection,
            status="ok",
            roi=roi,
            landmarks=landmarks,
            visibility_mean=vis_sum / len(landmarks) if landmarks else 0.0,
        )


# ---------------------------------------------------------------------------
# 最小验证 (Step 3 验收): detector -> pose 对 test.jpg 的 3 名人员逐个估姿态
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    img_path = sys.argv[1] if len(sys.argv) > 1 else "data/test.jpg"
    from detector import Detector

    img = cv2.imread(img_path)
    if img is None:
        raise SystemExit(f"无法读取图像: {img_path}")

    det = Detector("config/config.yaml")
    persons = det.detect_frame(img)

    pe = PoseEstimator("config/config.yaml")
    poses = pe.estimate_persons(img, persons)

    n_ok = sum(1 for p in poses if p.is_ok)
    n_skip = sum(1 for p in poses if p.status in ("skipped", "no_landmarks", "error"))
    print(f"[persons] 检测到 {len(persons)} 人  ->  姿态成功 {n_ok} / 跳过/失败 {n_skip}")
    for p in poses:
        n_lm = len(p.landmarks) if p.landmarks else 0
        print(f"  [{p.status}] {p.detection.class_name:8s} roi={p.roi} "
              f"landmarks={n_lm} vis_mean={p.visibility_mean:.2f}")
        if p.is_ok and p.landmarks:
            l0 = p.landmarks[0]
            print(f"      nose(x) -> 原图坐标 ({l0.x:.0f},{l0.y:.0f})  z={l0.z:.3f} vis={l0.visibility:.2f}")
        if p.skip_reason:
            print(f"      原因: {p.skip_reason}")