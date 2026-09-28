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
  person_id  : 已由 StableIdAssociator 生成的跨帧稳定人员 ID(不再依赖单帧检测顺序)
               [Stable-ID] 该 ID 由检测框空间匹配维持, 同一人连续帧保持稳定。
  bbox       : [x1, y1, x2, y2] 检测框像素坐标 (加入输出, 供调试/未来决策)
  state      : 人员状态 (Standing/Sitting/Lying/Fall/Help-Waving)
  risk_score : DRRM 聚合风险分, 0~1
  priority   : HIGH / MEDIUM / LOW

依赖: 本模块被 src/edge_client.py 以模块级形式 `import edge_perception`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from detector import Detector
from pose import PoseEstimator
from ranking import PersonEvidence, rank
from risk import Riskevaluator
from state import StateClassifier


# ============================================================================
# Stable ID 轻量关联器 (Stage: Stable ID)
# ----------------------------------------------------------------------------
# 目标: 缓解 person_id 每帧漂移导致 Agent cooldown 失效。
# 方法: 基于已有 YOLO bbox, 用归一化中心距离做跨帧最近邻匹配, 维持 stable_id。
# 刻意**不引入** DeepSORT / 任何 tracking 模型; 仅短期帧间关联。
# 冲突: 同一 stable_id 不分配给两个检测; 连失 max_miss 帧后释放该 id。
# ============================================================================


@dataclass
class Track:
    stable_id: int
    bbox: List[float]          # [x1, y1, x2, y2] 像素
    miss_count: int = 0
    last_seen: float = 0.0     # 帧序号(单调递增)


class StableIdAssociator:
    """跨帧基于 bbox 中心距离的轻量人员稳定 ID 关联器。"""

    def __init__(
        self,
        match_dist: float = 0.10,
        size_ratio: float = 2.0,
        max_miss: int = 5,
    ):
        self.match_dist = match_dist      # 归一化中心距离阈值
        self.size_ratio = size_ratio      # 尺寸容忍上限 (倍数)
        self.max_miss = max_miss          # 连失像素擦除
        self.tracks: dict[int, Track] = {}
        self._next_id: int = 1            # 稳定 id 递增
        self._frame_seq: int = 0

    def _center(self, bbox: List[float]) -> tuple:
        x1, y1, x2, y2 = bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def _norm_dist(self, a: tuple, b: tuple, iw: float, ih: float) -> float:
        dx = (a[0] - b[0]) / max(1.0, iw)
        dy = (a[1] - b[1]) / max(1.0, ih)
        return math.hypot(dx, dy)

    def _size_ok(self, cur: List[float], ref: List[float]) -> bool:
        wc, wref = cur[2] - cur[0], ref[2] - ref[0]
        hc, href = cur[3] - cur[1], ref[3] - ref[1]
        if wref <= 0 or href <= 0 or wc <= 0 or hc <= 0:
            return True
        wr, hr = wc / wref, hc / href
        lo, hi = 1.0 / self.size_ratio, self.size_ratio
        return lo <= wr <= hi and lo <= hr <= hi

    def associate(self, bboxes: List[List[float]], iw: float, ih: float) -> List[int]:
        """给当前帧每条 bbox 分配稳定 id (与 bboxes 等长, 顺序对应)。

        匹配: 归一化中心距离最近 + 尺寸容忍 且 < match_dist -> 沿用 track id;
        否则 -> 生成新 id。一个 track 本帧至多被一个检测占用(分配后移除待选)。
        未匹配到的旧 track miss_count+1, 超 max_miss 移除。
        """
        self._frame_seq += 1
        # 上一帧 track 拷贝为候选(匹配未成功仍保留原 bbox)
        available = {sid: tr for sid, tr in self.tracks.items()}
        assigned_ids: List[int] = []

        for bbox in bboxes:
            cx, cy = self._center(bbox)
            best_sid, best_dist = None, None
            for sid, tr in available.items():
                ref = self._center(tr.bbox)
                d = self._norm_dist((cx, cy), ref, iw, ih)
                if d > self.match_dist:
                    continue
                if not self._size_ok(bbox, tr.bbox):
                    continue
                if best_dist is None or d < best_dist:
                    best_dist, best_sid = d, sid

            if best_sid is not None:
                # 沿用稳定 id
                self.tracks[best_sid].bbox = list(bbox)
                self.tracks[best_sid].miss_count = 0
                self.tracks[best_sid].last_seen = self._frame_seq
                assigned_ids.append(best_sid)
                del available[best_sid]
            else:
                # 新 id
                sid = self._next_id
                self._next_id += 1
                self.tracks[sid] = Track(
                    stable_id=sid, bbox=list(bbox),
                    miss_count=0, last_seen=self._frame_seq,
                )
                assigned_ids.append(sid)

        # 未匹配旧 track 递增 miss; 超阈值移除
        for sid in list(self.tracks.keys()):
            if sid in assigned_ids:
                continue
            self.tracks[sid].miss_count += 1
            if self.tracks[sid].miss_count > self.max_miss:
                del self.tracks[sid]

        return assigned_ids


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
        stable_match_dist: float = 0.10,
        stable_size_ratio: float = 2.0,
        stable_max_miss: int = 5,
    ):
        # 生产默认: 各自用 V0.1.5 默认配置(config/config.yaml)构建
        self._det = det if det is not None else Detector()
        self._pe = pe if pe is not None else PoseEstimator()
        self._clf = clf if clf is not None else StateClassifier()
        self._rk = rk if rk is not None else Riskevaluator()
        self._rank = rank_fn

        # Stable ID 关联器 (保持检测人员跨帧稳定身份; 可经构造参数调阈值)
        self._tracker = StableIdAssociator(
            match_dist=stable_match_dist,
            size_ratio=stable_size_ratio,
            max_miss=stable_max_miss,
        )

    def analyze(self, frame) -> List[dict]:
        """对一帧图像跑感知管线, 返回 person_events 列表(可空)。

        装配与 src/main.py 保持一致: 检测→分流 civilian→姿态→状态+风险→排序。
        """
        # 1) 检测(含全部类别) → 所有人员由 StableIdAssociator 分配跨帧稳定 id
        persons_all = self._det.detect_frame(frame)
        person_dets = [d for d in persons_all if d.is_person]
        h, w = (int(frame.shape[0]), int(frame.shape[1])) \
            if hasattr(frame, "shape") else (0, 0)
        bboxes = [list(d.bbox) for d in person_dets]
        stable_ids = self._tracker.associate(bboxes, float(w), float(h))
        pid_of = {id(d): sid for d, sid in zip(person_dets, stable_ids)}
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

        # 6) 转为 person_events (person_id 为跨帧稳定 id; 附带 bbox 供调试/决策)
        events: List[dict] = []
        for rr in ranked:
            events.append(
                {
                    "person_id": pid_of[id(rr.detection)],
                    "bbox": list(rr.detection.bbox),
                    "state": rr.state,
                    "risk_score": round(float(rr.risk_score), 4),
                    "priority": rr.priority,
                }
            )
        return events