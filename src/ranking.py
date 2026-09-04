"""
Ranking layer — rescue priority ranking (DDRRM V0.1, Step 6)
============================================================
职责: 基于 DRRM 单帧 risk_score 对场景内人员输出救援优先级排序 (对标 PRD 第 7 章, 单帧静态版)。

范围约束 (V0.1):
  - 纯风险分排序, 不加入视频 / 时序 / TSF / Static Lying / 历史记忆 / DRRM 新增因子。
  - 不做路径规划 / 资源调度 (PRD 7.1 范围决策)。

排序规则 (PRD 7.2 单帧退化版):
  主键   : risk_score 降序
  次键   : 状态危险等级 Fall > Lying > Help-Waving > Sitting > Standing
  末键   : 检测confidence 降序, 再 pose visibility 降序
priority 分级: >=0.75 HIGH | 0.5..0.75 MEDIUM | else LOW
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from detector import Detection
from pose import PoseResult
from risk import RiskResult
from state import StateResult

# 状态危险等级 (数值越小越危险; 用于次键 tie-break)
STATE_ORDER: dict = {
    "Fall": 0,
    "Lying": 1,
    "Help-Waving": 2,
    "Sitting": 3,
    "Standing": 4,
}


@dataclass
class PersonEvidence:
    """单个人员的完整证据, 供排序用; risk 为 None 则不计入排序。"""

    detection: Detection
    state: Optional[StateResult]
    risk: Optional[RiskResult]
    pose_vis: float = 0.0    # 用于末键 tie-break (pose visibility)


@dataclass
class RankingResult:
    """单一人员的排序结果。"""

    rank: int                 # 1-based, 越靠前越优先
    class_name: str
    state: str
    risk_score: float
    priority: str             # HIGH | MEDIUM | LOW
    details: dict             # 分项明细与 tie-break 依据 (可解释)


def _priority(risk_score: float) -> str:
    if risk_score >= 0.75:
        return "HIGH"
    if risk_score >= 0.5:
        return "MEDIUM"
    return "LOW"


def _sort_key(ev: PersonEvidence) -> Tuple:
    r = ev.risk
    return (
        -r.risk_score,                              # 主键: 风险降序
        STATE_ORDER.get(ev.state.state if ev.state else "Standing", 4),  # 次键: 状态危险等级
        -ev.detection.confidence,                   # 末键: 检测置信度降序
        -ev.pose_vis,                               # 末键: pose 可见度降序
    )


def rank(evidences: List[PersonEvidence]) -> List[RankingResult]:
    """对证据列表排序并输出 RankingResult 序列。无效 (risk=None) 不计入。"""
    valid = [ev for ev in evidences if ev.risk is not None]
    ordered = sorted(valid, key=_sort_key)
    results: List[RankingResult] = []
    for i, ev in enumerate(ordered, start=1):
        r = ev.risk
        det_conf = float(ev.detection.confidence)
        # 可解释明细
        details = {
            "f1": round(r.f1, 3),
            "f6": round(r.f6, 3),
            "det_conf": round(det_conf, 3),
            "pose_vis": round(ev.pose_vis, 3),
            "state_risk_order": STATE_ORDER.get(ev.state.state if ev.state else "Standing", 4),
        }
        # tie-break 依据说明
        prev = ordered[i - 2] if i >= 2 else None
        if prev is not None and abs(prev.risk.risk_score - r.risk_score) < 1e-9:
            details["tie_reason"] = (
                f"risk_score 相同, 按状态危险等级 "
                f"({prev.state.state if prev.state else '?'} vs {ev.state.state if ev.state else '?'})"
            )
        else:
            details["tie_reason"] = "依 risk_score 排序"

        results.append(
            RankingResult(
                rank=i,
                class_name=ev.detection.class_name,
                state=r.state,
                risk_score=round(r.risk_score, 4),
                priority=_priority(r.risk_score),
                details=details,
            )
        )
    return results


# ---------------------------------------------------------------------------
# 独立测试入口 (Step 6 验收): detector -> pose -> state -> risk -> rank
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import cv2

    from detector import Detector
    from pose import PoseEstimator
    from risk import Riskevaluator
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

    evidences: List[PersonEvidence] = []
    for p in poses:
        if not p.is_ok:
            print(f"  [skip] {p.detection.class_name:8s} {p.status} (无姿态)")
            continue
        sr = clf.classify(p)
        rr = rk.assess(p.detection, p, sr) if sr else None
        if rr is None:
            print(f"  [skip] {p.detection.class_name:8s} 证据不足, 不参与排序")
            continue
        evidences.append(PersonEvidence(detection=p.detection, state=sr, risk=rr, pose_vis=p.visibility_mean))

    print("\n=== 救援优先级排序榜 ===")
    for rr in rank(evidences):
        print(f"  #{rr.rank}  {rr.class_name:8s} state={rr.state:12s} "
              f"risk={rr.risk_score:.3f} priority={rr.priority:6s} "
              f"f1={rr.details['f1']} f6={rr.details['f6']} | {rr.details['tie_reason']}")
    if not evidences:
        print("  (无有效人员参与排序)")