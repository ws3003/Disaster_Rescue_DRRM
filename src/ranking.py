"""
Ranking layer — rescue priority ranking (DDRRM V0.1, Step 6)
============================================================
职责: 基于人员状态和 DRRM risk_score 对场景内人员输出救援优先级排序。

V0.1.1:
  - 风险等级由最终识别状态直接决定。
  - Standing  -> LOW
  - Sitting   -> LOW
  - Lying     -> MEDIUM
  - Fall      -> HIGH
  - Help-Waving -> HIGH
  - risk_score 仍用于同一风险等级内的救援排序。
  - 保留检测置信度和姿态可见度作为 tie-break。
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from detector import Detection
from pose import PoseResult
from risk import RiskResult
from state import StateResult


# ---------------------------------------------------------------------------
# 状态危险等级
# 数值越小表示危险程度越高。
# 用于 risk_score 相同情况下的排序 tie-break。
# ---------------------------------------------------------------------------
STATE_ORDER: dict = {
    "Fall": 0,
    "Help-Waving": 1,
    "Lying": 2,
    "Sitting": 3,
    "Standing": 4,
}


# ---------------------------------------------------------------------------
# 固定风险等级映射
# ---------------------------------------------------------------------------
STATE_PRIORITY: dict = {
    "Fall": "HIGH",
    "Help-Waving": "HIGH",
    "Lying": "MEDIUM",
    "Sitting": "LOW",
    "Standing": "LOW",
}


@dataclass
class PersonEvidence:
    """单个人员的完整证据, 供排序使用。"""

    detection: Detection
    state: Optional[StateResult]
    risk: Optional[RiskResult]
    pose_vis: float = 0.0


@dataclass
class RankingResult:
    """单一人员的排序结果。"""

    rank: int
    class_name: str
    state: str
    risk_score: float
    priority: str
    details: dict
    detection: Optional[Detection] = None


def _priority(state: str, risk_score: float) -> str:
    """
    根据最终状态确定风险等级。

    五种状态的固定映射：

        Standing    -> LOW
        Sitting     -> LOW
        Lying       -> MEDIUM
        Fall        -> HIGH
        Help-Waving -> HIGH

    risk_score 不再直接决定 HIGH/MEDIUM/LOW。
    risk_score 仍然用于人员之间的优先级排序。
    """

    if state in STATE_PRIORITY:
        return STATE_PRIORITY[state]

    # 未知状态采用 risk_score 作为安全兜底。
    if risk_score >= 0.60:
        return "HIGH"
    if risk_score >= 0.45:
        return "MEDIUM"
    return "LOW"


def _sort_key(ev: PersonEvidence) -> Tuple:
    """
    排序规则：

    1. risk_score 降序
    2. 状态危险等级
    3. detection confidence 降序
    4. pose visibility 降序
    """

    r = ev.risk

    state_name = ev.state.state if ev.state else "Standing"

    return (
        -r.risk_score,
        STATE_ORDER.get(state_name, 4),
        -ev.detection.confidence,
        -ev.pose_vis,
    )


def rank(evidences: List[PersonEvidence]) -> List[RankingResult]:
    """
    对证据列表进行救援优先级排序。

    risk=None 的人员不参与排序。
    """

    valid = [
        ev
        for ev in evidences
        if ev.risk is not None
    ]

    ordered = sorted(valid, key=_sort_key)

    results: List[RankingResult] = []

    for i, ev in enumerate(ordered, start=1):

        r = ev.risk

        state_name = (
            ev.state.state
            if ev.state
            else "Standing"
        )

        det_conf = float(
            ev.detection.confidence
        )

        priority = _priority(
            state_name,
            r.risk_score,
        )

        details = {
            "f1": round(r.f1, 3),
            "f6": round(r.f6, 3),
            "det_conf": round(det_conf, 3),
            "pose_vis": round(ev.pose_vis, 3),
            "state_risk_order": STATE_ORDER.get(
                state_name,
                4,
            ),
            "priority_source": "state",
        }

        # tie-break 依据说明
        prev = ordered[i - 2] if i >= 2 else None

        if (
            prev is not None
            and abs(
                prev.risk.risk_score
                - r.risk_score
            ) < 1e-9
        ):

            prev_state = (
                prev.state.state
                if prev.state
                else "?"
            )

            details["tie_reason"] = (
                f"risk_score 相同, 按状态危险等级 "
                f"({prev_state} vs {state_name})"
            )

        else:

            details["tie_reason"] = (
                "依 risk_score 排序"
            )

        results.append(
            RankingResult(
                rank=i,
                class_name=ev.detection.class_name,
                state=state_name,
                risk_score=round(
                    r.risk_score,
                    4,
                ),
                priority=priority,
                details=details,
                detection=ev.detection,
            )
        )

    return results


# ---------------------------------------------------------------------------
# 独立测试入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":

    import cv2

    from detector import Detector
    from pose import PoseEstimator
    from risk import Riskevaluator
    from state import StateClassifier

    img = cv2.imread(
        "data/test.jpg"
    )

    if img is None:
        raise SystemExit(
            "无法读取图像: data/test.jpg"
        )

    det = Detector(
        "config/config.yaml"
    )

    persons = det.detect_frame(
        img
    )

    pe = PoseEstimator(
        "config/config.yaml"
    )

    poses = pe.estimate_persons(
        img,
        persons,
    )

    clf = StateClassifier()

    rk = Riskevaluator(
        "config/config.yaml"
    )

    evidences: List[PersonEvidence] = []

    for p in poses:

        if not p.is_ok:

            print(
                f"  [skip] "
                f"{p.detection.class_name:8s} "
                f"{p.status} "
                f"(无姿态)"
            )

            continue

        sr = clf.classify(p)

        rr = (
            rk.assess(
                p.detection,
                p,
                sr,
            )
            if sr
            else None
        )

        if rr is None:

            print(
                f"  [skip] "
                f"{p.detection.class_name:8s} "
                f"证据不足, 不参与排序"
            )

            continue

        evidences.append(
            PersonEvidence(
                detection=p.detection,
                state=sr,
                risk=rr,
                pose_vis=p.visibility_mean,
            )
        )

    print(
        "\n=== 救援优先级排序榜 ==="
    )

    for rr in rank(evidences):

        print(
            f"  #{rr.rank} "
            f"{rr.class_name:8s} "
            f"state={rr.state:12s} "
            f"risk={rr.risk_score:.3f} "
            f"priority={rr.priority:6s} "
            f"f1={rr.details['f1']} "
            f"f6={rr.details['f6']} | "
            f"{rr.details['tie_reason']}"
        )

    if not evidences:

        print(
            "  (无有效人员参与排序)"
        )
