"""
src/edge_perception.py — Edge Perception Bridge (Stage 7-C)
============================================================
职责: 把 V0.1.5 本地感知管线(YOLO→Pose→State→Risk→Rank)封装为一个
"输入 numpy 图片、输出 person_events 列表" 的桥接层, 供 Edge Client
采集帧后与云端大模型融合上传。

只做**调用组装**, 不复制任何感知算法:
  - 装配逻辑与 src/main.py 的 process_one 一致(仅取最终 person_events);
  - 底层推理器(detector/pose/state/risk/ranking)不变;
  - 运行/测试均不触碰真实摄像头与真实模型(测试注入 fake 组件)。

person_events 各字段：
  person_id  : 图内人员唯一编号(与 main.py 的 pid 一致, person 依检测顺序从 1 编号)
  state      : 人员状态 (Standing/Sitting/Lying/Fall/Help-Waving)
  risk_score : DRRM 聚合风险分, 0~1
  priority   : HIGH / MEDIUM / LOW

依赖: 本模块被 src/edge_client.py 以模块级形式 `import edge_perception`。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from detector import Detector
from pose import PoseEstimator
from ranking import PersonEvidence, rank
from risk import Riskevaluator
from state import StateClassifier


class EdgePerception:
    """V0.1.5 感知管线 → person_events 的桥接层。

    参数(测试注入 fake, 生产用默认):
        det / pe / clf / rk / rank_fn
    """

    def __init__(
        self,
        det: Any = None,
        pe: Any = None,
        clf: Any = None,
        rk: Any = None,
        rank_fn: Callable = rank,
    ):
        # 生产默认: 各自用 V0.1.5 默认配置(config/config.yaml)构建
        self._det = det if det is not None else Detector()
        self._pe = pe if pe is not None else PoseEstimator()
        self._clf = clf if clf is not None else StateClassifier()
        self._rk = rk if rk is not None else Riskevaluator()
        self._rank = rank_fn

    def analyze(self, frame) -> List[dict]:
        """对一帧图像跑感知管线, 返回 person_events 列表(可空)。

        装配与 src/main.py 保持一致: 检测→分流 civilian→姿态→状态+风险→排序。
        """
        # 1) 检测(含全部类别) → 人员统一编号
        persons_all = self._det.detect_frame(frame)
        person_dets = [d for d in persons_all if d.is_person]
        pid_of = {id(d): i for i, d in enumerate(person_dets, start=1)}
        civilians = [d for d in person_dets if d.class_name == "civilian"]

        # 2) 姿态(仅 civilian)
        civilian_poses = self._pe.estimate_persons(frame, civilians)

        # 3) 状态 + 4) 风险 (仅 civilian; 关键证据缺失则跳过)
        task: dict = {}
        for det_obj, pose in zip(civilians, civilian_poses):
            state = self._clf.classify(pose) if pose and pose.is_ok else None
            risk = self._rk.assess(det_obj, pose, state) if (pose and state) else None
            task[id(det_obj)] = {"pose": pose, "state": state, "risk": risk}

        # 5) 排序 (PersonEvidence.detection 持有源 Detection 引用, 用于身份回填)
        evidences = [
            PersonEvidence(
                detection=d,
                state=task[id(d)]["state"],
                risk=task[id(d)]["risk"],
                pose_vis=task[id(d)]["pose"].visibility_mean if task[id(d)]["pose"] else 0.0,
            )
            for d in civilians if task[id(d)]["risk"] is not None
        ]
        ranked = self._rank(evidences)

        # 6) 转为 person_events (person_id 与 main.py pid 对齐)
        events: List[dict] = []
        for rr in ranked:
            events.append(
                {
                    "person_id": pid_of[id(rr.detection)],
                    "state": rr.state,
                    "risk_score": round(float(rr.risk_score), 4),
                    "priority": rr.priority,
                }
            )
        return events