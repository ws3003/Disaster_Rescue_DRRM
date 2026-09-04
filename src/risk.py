"""
Risk layer — DRRM single-frame risk scoring (DDRRM V0.1, Step 5)
===============================================================
职责: 对单一人员做单帧 DRRM 动态风险评分 (对标 PRD 第 6 章, 单帧退化版)。

范围约束 (V0.1):
  - 仅实现单帧评分, 聚合 F1(状态风险) × F6(证据权重); F2-F5 保持中性 (alpha=0)。
  - 不实现时序 / 历史记忆 / 持续时长 / TSF / 排序。

评分框架 (PRD 6.4 R = Σ αᵢ·wᵢ·fᵢ, 单帧仅 F1 激活, F6 为统一证据调制):
  f1      = state_risk[state] 按状态置信度向中性值 0.5 回归
            -> f1 = 0.5 + (base - 0.5) * confidence    (置信度越高越贴近先验, 越低越保守收敛)
  f6      = 0.5 + 0.5 × clip(检测置信度 × pose可见度均值, 0, 1)   (V0.1.5 Step5 标定软化)
  risk_score = f1 × f6       (证据越充分, 风险越可信; 遮挡/模糊帧整体降权)

V0.1.5 Step5 标定 (tests/calibrate_risk.py):
  原 f6 = clip(det_conf×pose_vis, w_min, 1) 为纯乘性腰斩, 典型证据 0.5~0.7 使
  risk_score 全塌缩在 0.15~0.37 (全员 LOW, HIGH/MEDIUM 不可达)。
  软化后 f6 ∈ [0.5, 1.0]: 证据差仍降权但不腰斩, 风险动态范围与 priority 分级
  (HIGH>=0.60 / MEDIUM>=0.45, 见 ranking.py) 重新对齐。R = f1 × f6 设计不变。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from detector import Detection, load_config
from pose import PoseResult
from state import StateResult

NEUTRAL_RISK = 0.5   # 状态不确定时的风险回归中性值 (可解释: 不知道 => 不夸大也不轻视)


@dataclass
class RiskResult:
    """单一人员的单帧 DRRM 风险输出。"""

    state: str                # 瞬时状态
    f1: float                 # F1 状态风险 (按置信度调整后)
    f6: float                 # F6 证据权重
    risk_score: float         # 聚合风险分 = f1 × f6, [0,1]


class Riskevaluator:
    """DRRM 单帧评分器。"""

    def __init__(self, config_path: str | Path | None = None):
        cfg_path = Path(config_path) if config_path else Path("config/config.yaml")
        self.cfg = load_config(cfg_path)

        dr = self.cfg["drrm"]
        self.state_risk: dict = dict(dr["state_risk"])   # state -> 先验危险度
        self.w_min: float = float(dr["w_min"])           # F6 证据权重下限

    # ------------------------------------------------------------------
    # 单帧评分
    # ------------------------------------------------------------------
    def assess(
        self,
        detection: Detection,
        pose: PoseResult,
        state: StateResult,
    ) -> Optional[RiskResult]:
        """对一名有效人员做单帧风险评分; 关键证据缺失时返回 None(交上层跳过)。"""
        if pose is None or not pose.is_ok or pose.landmarks is None:
            return None
        if state is None:
            return None

        # ---- F1: 状态风险 (先验危险度 × 置信度回归) ----
        base = float(self.state_risk.get(state.state, NEUTRAL_RISK))
        conf = max(0.0, min(1.0, float(state.confidence)))
        f1 = NEUTRAL_RISK + (base - NEUTRAL_RISK) * conf
        f1 = max(0.0, min(1.0, f1))

        # ---- F6: 证据权重 (V0.1.5 Step5 软化) ----
        # 原始证据 = 检测置信度 × 姿态可见度 ∈ [0,1]; 线性软化到 [0.5, 1.0]:
        # 证据充分时 f6→1, 证据差时最低 0.5 (而非乘性腰斩到 0.25~0.5)。
        # w_min 作为硬下限保留 (软化后自然下界 0.5 已高于 w_min, 通常不触发)。
        det_conf = float(detection.confidence)
        pose_vis = float(getattr(pose, "visibility_mean", 0.0) or 0.0)
        raw_ev = max(0.0, min(1.0, det_conf * pose_vis))
        f6 = max(self.w_min, min(1.0, 0.5 + 0.5 * raw_ev))

        # ---- 聚合: risk_score = f1 × f6 ----
        risk_score = max(0.0, min(1.0, f1 * f6))

        return RiskResult(state=state.state, f1=f1, f6=f6, risk_score=risk_score)


# ---------------------------------------------------------------------------
# 最小验证 (Step 5 验收): detector -> pose -> state -> risk 输出每人分项
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import cv2

    from detector import Detector
    from pose import PoseEstimator
    from state import StateClassifier

    img = cv2.imread("data/test.jpg")
    if img is None:
        raise SystemExit("无法读取图像: data/test.jpg")

    det = Detector("config/config.yaml")
    persons = det.detect_frame(img)

    pe = PoseEstimator("config/config.yaml")
    poses = pe.estimate_persons(img, persons)

    clf = StateClassifier()
    rk = Riskevaluator("config/config.yaml")

    print(f"[persons] 检测 {len(persons)} 人")
    for p in poses:
        if not p.is_ok:
            print(f"  [{p.status}] {p.detection.class_name:8s} (无姿态, 跳过) {p.skip_reason}")
            continue
        sr = clf.classify(p)
        if sr is None:
            print(f"  [skip] {p.detection.class_name:8s} 无有效状态")
            continue
        rr = rk.assess(p.detection, p, sr)
        if rr is None:
            print(f"  [skip] {p.detection.class_name:8s} 证据不足")
            continue
        print(f"  [{p.detection.class_name:8s}] state={rr.state:12s} "
              f"f1={rr.f1:.3f}  f6={rr.f6:.3f}  risk_score={rr.risk_score:.3f}")