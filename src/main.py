"""
Disaster_Rescue_DRRM V0.1 — 单帧端到端演示入口
=============================================
流程: config → 读图 → YOLO检测 → MediaPipe姿态 → 状态识别 → DRRM风险 → 救援排序 → 可视化输出
只做编排与可视化, 不重新实现任何算法逻辑, 复用已有模块:
  detector.Detector / pose.PoseEstimator / state.StateClassifier /
  risk.Riskevaluator / ranking.rank

用法:
  venv\\Scripts\\python.exe src/main.py --config config/config.yaml --input data/test.jpg
输出:
  outputs/v0.1_result.jpg   (可视化: bbox + 类别 + 33关键点骨架 + 状态 + risk + rank)
"""

import argparse
import os
from pathlib import Path

import cv2
import numpy as np
from mediapipe.python.solutions.pose import POSE_CONNECTIONS

from detector import Detection, Detector, load_config
from pose import PoseEstimator, PoseResult
from ranking import PersonEvidence, rank
from risk import Riskevaluator
from state import StateClassifier

# 显示配色 (BGR)
COLOR_PERSON = (0, 200, 0)        # 人员 bbox 绿
COLOR_NONPERSON = (160, 160, 160)  # 非人员(动物) bbox 灰
COLOR_SKELETON = (220, 220, 0)     # 骨架 青
COLOR_LOW = (0, 200, 0)            # LOW 风险 绿
COLOR_MED = (0, 165, 255)          # MEDIUM 风险 橙
COLOR_HIGH = (0, 0, 255)           # HIGH 风险 红
COLOR_TEXT_BG = (40, 40, 40)


def _risk_color(score: float):
    if score >= 0.75:
        return COLOR_HIGH
    if score >= 0.5:
        return COLOR_MED
    return COLOR_LOW


def _draw_label(img, text: str, org, color, font_scale: float, thickness: int, bg: bool = True):
    """在 bbox 上方绘带底色的文字标签。"""
    if bg:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        x, y = int(org[0]), int(org[1])
        cv2.rectangle(img, (x, y - th - 6), (x + tw + 4, y + 2), COLOR_TEXT_BG, -1)
        cv2.putText(img, text, (x + 2, y - 1), cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness)
    else:
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness)


def _draw_skeleton(img, pose: PoseResult, thickness: int):
    if pose.landmarks is None:
        return
    pts = [(int(lm.x), int(lm.y)) for lm in pose.landmarks]
    for a, b in POSE_CONNECTIONS:
        if a >= len(pts) or b >= len(pts):
            continue
        cv2.line(img, pts[a], pts[b], COLOR_SKELETON, tuple([thickness])[0] if isinstance(thickness, tuple) else thickness)


def run(config_path: str | Path, input_path: str | Path):
    cfg = load_config(config_path)
    img = cv2.imread(str(input_path))
    if img is None:
        raise SystemExit(f"无法读取输入图片: {input_path}")

    # 1) 检测 (含全部类别)
    det = Detector(config_path)
    persons_all = det.detect_frame(img)

    # 2) 姿态 (仅人员)
    pe = PoseEstimator(config_path)
    person_poses = pe.estimate_persons(img, persons_all)

    # 3) 状态 + 4) 风险 + 5) 排序
    clf = StateClassifier()
    rk = Riskevaluator(config_path)

    # <det_id> -> 该人员的结果 (含 pose / state / risk / rank)
    task: dict = {}
    pose_idx = 0
    for det_obj in persons_all:
        if not det_obj.is_person:
            continue
        pose = person_poses[pose_idx] if pose_idx < len(person_poses) else None
        pose_idx += 1
        state = clf.classify(pose) if pose and pose.is_ok else None
        risk = rk.assess(det_obj, pose, state) if (pose and state) else None
        task[id(det_obj)] = {"pose": pose, "state": state, "risk": risk}

    evidences = [
        PersonEvidence(detection=d, state=task[id(d)]["state"], risk=task[id(d)]["risk"],
                       pose_vis=task[id(d)]["pose"].visibility_mean if task[id(d)]["pose"] else 0.0)
        for d in persons_all if d.is_person and task[id(d)]["risk"] is not None
    ]
    ranked = rank(evidences)

    # 6) 可视化
    vcfg = cfg["visualize"]
    fs = float(vcfg["panel_font_scale"])
    thk = int(vcfg["box_thickness"])
    annot = img.copy()

    rank_lookup = _build_rank_lookup(persons_all, ranked)

    for det_obj in persons_all:
        x1, y1, x2, y2 = [int(v) for v in det_obj.bbox]
        color = COLOR_PERSON if det_obj.is_person else COLOR_NONPERSON
        cv2.rectangle(annot, (x1, y1), (x2, y2), color, thk)

        if det_obj.is_person:
            t = task[id(det_obj)]
            pose = t["pose"]
            info = rank_lookup.get(id(det_obj))
            label = f"{det_obj.class_name}"
            if info is not None:
                label += f" R#{info.rank} {info.state} {info.risk_score:.2f}"
            else:
                st = t["state"]
                label += f" {st.state if st else 'n/a'}"
            rc = _risk_color(info.risk_score) if info else COLOR_PERSON
            # bbox + 类别
            _draw_label(annot, f"{det_obj.class_name}", (x1, y1 - 4), color, fs, thk)
            # 骨架
            if vcfg["draw_skeleton"] and pose and pose.is_ok:
                _draw_skeleton(annot, pose, thk)
            # 状态 + risk + rank
            if vcfg.get("draw_states", True):
                _draw_label(annot, label.replace(f"{det_obj.class_name} ", ""), (x1, y2 + 2), rc, fs, thk - 1)
        else:
            _draw_label(annot, f"{det_obj.class_name}", (x1, y1 - 4), color, fs, thk)

    out_path = Path(cfg["output"]["dir"]) / cfg["output"]["annotated_image"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), annot)

    # 控制台汇总
    _console_summary(persons_all, task, ranked)
    print(f"[saved] {out_path}")
    return out_path


def _build_rank_lookup(persons_all, ranked):
    """按类别名排队回填, 生成 id -> RankingResult 映射 (同类多目标时顺序稳定)。"""
    rk_list = list(ranked)
    lookup = {}
    for d in persons_all:
        if not d.is_person:
            continue
        for rr in rk_list:
            if rr.class_name == d.class_name:
                lookup[id(d)] = rr
                rk_list.remove(rr)
                break
    return lookup


def _console_summary(persons_all, task, ranked):
    print("=== Disaster Rescue DRRM V0.1 ===")
    n_person = sum(1 for d in persons_all if d.is_person)
    n_non = sum(1 for d in persons_all if not d.is_person)
    print(f"检测人数: {n_person} 人 (另有非人员目标 {n_non} 个)")
    print("状态统计:")
    from collections import Counter
    cnt = Counter(t["state"].state for t in task.values() if t["state"])
    if not cnt:
        print("  (无有效状态)")
    for k, v in cnt.most_common():
        print(f"  {k}: {v}")
    print("救援排序:")
    for rr in ranked:
        print(f"  rank={rr.rank}  class={rr.class_name}  state={rr.state}  "
              f"risk={rr.risk_score:.3f}  priority={rr.priority}")


def main():
    parser = argparse.ArgumentParser(description="Disaster_Rescue_DRRM V0.1 单帧端到端演示")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--input", default="data/test.jpg", help="输入图片路径")
    args = parser.parse_args()
    run(Path(args.config), Path(args.input))


if __name__ == "__main__":
    main()