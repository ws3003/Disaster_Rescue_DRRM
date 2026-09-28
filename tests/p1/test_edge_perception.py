"""Edge Perception Bridge 测试 (Stage 7-C, tests/p1): 全 fake 组件。

不依赖真实摄像头、真实 YOLO/MediaPipe 模型、真实 Qwen API。
"""

import json
import os
import sys
import unittest

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "src")
for _p in (ROOT, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from edge_perception import EdgePerception  # noqa: E402
from ranking import RankingResult  # noqa: E402

PRIORITY = {"Fall": "HIGH", "Help-Waving": "HIGH", "Lying": "MEDIUM",
            "Sitting": "LOW", "Standing": "LOW"}


class FakeDetection:
    def __init__(self, class_name="civilian", is_person=True, confidence=0.9,
                 bbox=(0, 0, 10, 10)):
        self.class_name = class_name
        self.is_person = is_person
        self.confidence = confidence
        self.bbox = bbox


class FakePose:
    def __init__(self, is_ok=True, visibility_mean=0.8):
        self.is_ok = is_ok
        self.visibility_mean = visibility_mean


class FakeState:
    def __init__(self, state="Fall", confidence=0.9):
        self.state = state
        self.confidence = confidence


class FakeRiskResult:
    def __init__(self, state, f1=0.7, f6=0.9, risk_score=0.63):
        self.state = state
        self.f1 = f1
        self.f6 = f6
        self.risk_score = risk_score


class FakeDetector:
    def __init__(self, detections):
        self.detections = detections

    def detect_frame(self, frame):
        return self.detections


class FakePoseEstimator:
    def __init__(self, poses=None):
        self.poses = poses  # 与 detections 对齐; 缺省每人生成 ok

    def estimate_persons(self, frame, detections):
        if self.poses is not None:
            return list(self.poses)
        return [FakePose() for _ in detections]


class FakeClassifier:
    def __init__(self, state="Fall"):
        self.state = state

    def classify(self, pose):
        st = self.state(state) if callable(self.state) else self.state
        return FakeState(state=st, confidence=0.9)


class FakeRisk:
    def assess(self, detection, pose, state):
        if pose is None or not pose.is_ok:
            return None
        return FakeRiskResult(state.state)


def fake_rank(evidences):
    out = []
    for ev in evidences:
        st = ev.state.state
        out.append(RankingResult(
            rank=len(out) + 1,
            class_name=ev.detection.class_name or "civilian",
            state=st,
            risk_score=round(float(ev.risk.risk_score), 4),
            priority=PRIORITY.get(st, "LOW"),
            details={"f1": ev.risk.f1, "f6": ev.risk.f6},
            detection=ev.detection,
        ))
    return out


def make_bridge(detections, poses=None, state="Fall"):
    return EdgePerception(
        det=FakeDetector(detections),
        pe=FakePoseEstimator(poses),
        clf=FakeClassifier(state),
        rk=FakeRisk(),
        rank_fn=fake_rank,
    )


class PerceptionBridgeTest(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((480, 640, 3), dtype=np.uint8)  # Fake image input

    def test_fake_image_produces_events(self):
        events = make_bridge([FakeDetection()]).analyze(self.frame)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "Fall")

    def test_only_civilian_enters_and_id_alignment(self):
        # 一个 rescuer 夹在两个 civilian 中间: rescuer 不进入事件; id 含 rescuer 编号
        dets = [FakeDetection("civilian"), FakeDetection("rescuer"),
                FakeDetection("civilian")]
        events = make_bridge(dets).analyze(self.frame)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["person_id"], 1)
        self.assertEqual(events[1]["person_id"], 3)  # rescuer 占编号 2

    def test_state_conversion(self):
        events = make_bridge([FakeDetection()], state="Fall").analyze(self.frame)
        self.assertEqual(events[0]["state"], "Fall")
        self.assertEqual(events[0]["priority"], "HIGH")

    def test_pose_missing_skips_person(self):
        dets = [FakeDetection("civilian")]
        events = make_bridge(dets, poses=[None]).analyze(self.frame)
        self.assertEqual(events, [])

    def test_person_events_json_format(self):
        events = make_bridge([FakeDetection(), FakeDetection()]).analyze(self.frame)
        for ev in events:
            self.assertEqual(set(ev.keys()),
                             {"person_id", "bbox", "state", "risk_score", "priority"})
            self.assertIsInstance(ev["risk_score"], float)
            # 能作为 multipart 的 JSON 字符串上传 (格式正确)
            parsed = json.loads(json.dumps(ev, ensure_ascii=False))
            self.assertEqual(parsed, ev)


if __name__ == "__main__":
    unittest.main(verbosity=2)