"""
Disaster_Rescue_DRRM V0.1.5 Step2 — 状态识别评估脚本
====================================================
职责: 对人工标注的测试图片批量跑现有管线 (YOLO 检测 -> MediaPipe 姿态 ->
      StateClassifier 五类状态), 逐图比对人工标签, 输出准确率与错误模式统计。

约束: 本脚本只做评估, 不修改 detector/pose/state/risk 任何算法; 不写输出文件。
      rescuer 按稳定化后的管线不参与状态评估, 只统计 civilian。

标签文件: tests/test_labels.json  (人工目检建立)
运行:     venv\Scripts\python.exe tests\evaluate_states.py
          (路径基于项目根自动定位, 与当前工作目录无关)
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# 项目根 = tests/ 的上一级; src/ 加入 sys.path 以复用管线模块
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from detector import Detector  # noqa: E402
from pose import PoseEstimator  # noqa: E402
from state import StateClassifier  # noqa: E402

# 图片级预测聚合时的特殊取值 (不在五类状态内, 表示管线失败)
PRED_NONE_DET = "未检出"        # YOLO 未检出 civilian
PRED_NONE_STATE = "无有效状态"   # 检出人但姿态/状态失败

FIVE_STATES = ["Standing", "Sitting", "Lying", "Fall", "Help-Waving"]


def load_labels(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["images"]


def evaluate_image(img, det: Detector, pe: PoseEstimator, clf: StateClassifier):
    """
    对单图跑检测->姿态->状态, 返回:
      predictions: [(state, confidence), ...] 每个有效 civilian 框的状态
      n_civ: 检出的 civilian 框数
      skips: [str, ...] 姿态/状态失败的明细原因
    """
    detections = det.detect_frame(img)
    civilians = [d for d in detections if d.is_person and d.class_name == "civilian"]
    poses = pe.estimate_persons(img, civilians)

    predictions, skips = [], []
    for d, p in zip(civilians, poses):
        if not p.is_ok:
            skips.append(f"姿态失败[{d.class_name}]: {p.status} {p.skip_reason}")
            continue
        sr = clf.classify(p)
        if sr is None:
            skips.append(f"状态失败: 关键点不足 (bbox h={d.bbox[3]-d.bbox[1]:.0f}px)")
            continue
        predictions.append((sr.state, float(sr.confidence)))
    return predictions, len(civilians), skips


def aggregate(predictions: list, n_civ: int) -> str:
    """图片级预测: 多框多数投票; 无人/无状态归为管线失败类。"""
    if n_civ == 0:
        return PRED_NONE_DET
    if not predictions:
        return PRED_NONE_STATE
    # 重复检测等情况下同一人可能有多框: 按状态多数投票, 平票取检测顺序在先者
    return Counter(s for s, _ in predictions).most_common(1)[0][0]


def main():
    parser = argparse.ArgumentParser(description="V0.1.5 Step2 状态识别评估")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--images", default=str(ROOT / "data" / "test_images"))
    parser.add_argument("--labels", default=str(ROOT / "tests" / "test_labels.json"))
    args = parser.parse_args()

    labels = load_labels(Path(args.labels))
    det = Detector(args.config)
    pe = PoseEstimator(args.config)
    clf = StateClassifier()
    print(f"[device] {det.device}\n")

    rows = []  # (file, gt, pred, correct, detail_lines)
    for item in labels:
        fname, gt = item["file"], item["gt_state"]
        img_path = Path(args.images) / fname
        img = cv2.imread(str(img_path))
        if img is None:
            rows.append((fname, gt, "图片读取失败", False, [f"无法读取: {img_path}"]))
            continue

        preds, n_civ, skips = evaluate_image(img, det, pe, clf)
        pred = aggregate(preds, n_civ)
        correct = pred == gt

        detail = [f"civilian 框数={n_civ}"]
        for i, (s, c) in enumerate(preds, start=1):
            detail.append(f"  框#{i}: {s} (conf={c:.2f})")
        for s in skips:
            detail.append(f"  {s}")
        rows.append((fname, gt, pred, correct, detail))

    # ---- 报告输出 ----
    print("=" * 78)
    print("状态识别评估报告 (V0.1.5 Step2)")
    print("=" * 78)
    print(f"{'图片':<22}{'真实状态':<14}{'预测状态':<14}{'结果'}")
    print("-" * 78)
    for fname, gt, pred, correct, _ in rows:
        mark = "✓ 正确" if correct else "✗ 错误"
        print(f"{fname:<22}{gt:<14}{pred:<14}{mark}")
        for line in _:
            print(f"    {line}")
    print("-" * 78)

    n_total = len(rows)
    n_correct = sum(1 for r in rows if r[3])
    n_det_fail = sum(1 for r in rows if r[2] == PRED_NONE_DET)
    n_state_fail = sum(1 for r in rows if r[2] == PRED_NONE_STATE)

    print(f"\n总体准确率: {n_correct}/{n_total} = {n_correct / n_total * 100:.1f}%")
    print(f"  其中 检测失败(未检出): {n_det_fail} 张 | 状态失败(无有效状态): {n_state_fail} 张")

    # 五状态各自召回 (按 GT 统计)
    print("\n五状态召回 (按真实状态):")
    for st in FIVE_STATES:
        gt_rows = [r for r in rows if r[1] == st]
        if not gt_rows:
            print(f"  {st:<14} 无样本")
            continue
        hit = sum(1 for r in gt_rows if r[3])
        print(f"  {st:<14} {hit}/{len(gt_rows)}")

    # 混淆去向
    print("\n混淆去向 (真实 -> 预测):")
    for fname, gt, pred, correct, _ in rows:
        if not correct:
            print(f"  {gt:<14} -> {pred:<14} ({fname})")

    return 0 if n_correct == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
