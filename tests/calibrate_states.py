"""
Disaster_Rescue_DRRM V0.1.5 Step3 — 状态识别阈值标定实验台
=========================================================
职责: 在不修改 src/state.py 的前提下, 通过 monkeypatch 模块级阈值常量做扫描实验,
      并 dump 真实几何特征以支撑阈值决策。

Part 0  基线特征 dump: 每张图每个 civilian 框的 body_angle / knee / wrist_ratio /
        leg_extended / visibility, 用实测数值定阈值。
Part A  阈值扫描: monkeypatch state.LYING_ANGLE / WAVE_RATIO / SIT_KNEE 等,
        每个 config 跑全量 5 图, 输出准确率 + 五状态召回 + 逐图预测。
Part B  Sitting 辅助特征探针: 在 001(Standing) vs 005(Sitting) 上计算候选压缩特征
        (垂直腿长比 / 髋部位置 / 身体压缩度) 及关键点可见度, 判断新特征是否可行。

约束: 只读算法, 不写任何源码/输出文件; 003 检测漏检不计入状态层指标 (上限 4/5)。
运行: venv\\Scripts\\python.exe tests\\calibrate_states.py
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))  # 复用 evaluate_states

import cv2  # noqa: E402

import evaluate_states as ev  # noqa: E402
import state as state_mod  # noqa: E402
from detector import Detector  # noqa: E402
from pose import PoseEstimator  # noqa: E402
from state import StateClassifier  # noqa: E402

# MediaPipe 关键点索引 (与 state.py 一致)
_LSH, _RSH = 11, 12
_LHP, _RHP = 23, 24
_LKN, _RKN = 25, 26
_LAN, _RAN = 27, 28

# state.py 基线常量 (实验后恢复用)
BASELINE = {
    "LYING_ANGLE": state_mod.LYING_ANGLE,
    "FALL_KNEE": state_mod.FALL_KNEE,
    "FALL_BODYLEN": state_mod.FALL_BODYLEN,
    "WAVE_RATIO": state_mod.WAVE_RATIO,
    "SIT_KNEE": state_mod.SIT_KNEE,
    "STAND_KNEE": state_mod.STAND_KNEE,
}

FIVE_STATES = ["Standing", "Sitting", "Lying", "Fall", "Help-Waving"]


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def reset_baseline():
    for k, v in BASELINE.items():
        setattr(state_mod, k, v)


def apply_overrides(overrides: dict):
    reset_baseline()
    for k, v in overrides.items():
        setattr(state_mod, k, v)


def run_all(labels, images_dir, det, pe, clf):
    """跑全量图片, 返回 [(file, gt, pred, {box_preds...})]; pred 为图片级聚合结果。"""
    rows = []
    for item in labels:
        fname, gt = item["file"], item["gt_state"]
        img = cv2.imread(str(images_dir / fname))
        preds, n_civ, skips = ev.evaluate_image(img, det, pe, clf)
        pred = ev.aggregate(preds, n_civ)
        rows.append((fname, gt, pred, preds, skips))
    return rows


def summarize(tag: str, overrides: dict, rows):
    n = len(rows)
    correct = sum(1 for r in rows if r[1] == r[2])
    ov = ", ".join(f"{k}={v}" for k, v in overrides.items()) or "基线参数"
    print(f"\n[{tag}] {ov}")
    print(f"  准确率: {correct}/{n}")
    # 五状态召回
    recalls = []
    for st in FIVE_STATES:
        gt_rows = [r for r in rows if r[1] == st]
        if gt_rows:
            hit = sum(1 for r in gt_rows if r[2] == st)
            recalls.append(f"{st} {hit}/{len(gt_rows)}")
    print("  召回: " + " | ".join(recalls))
    # 逐图
    for fname, gt, pred, preds, skips in rows:
        mark = "✓" if gt == pred else "✗"
        boxes = ", ".join(f"{s}({c:.2f})" for s, c in preds) if preds else "-"
        print(f"    {mark} {fname:<22} GT={gt:<12} Pred={pred:<12} 框:[{boxes}]")
    return correct, n


# ---------------------------------------------------------------------------
# Part 0: 基线特征 dump
# ---------------------------------------------------------------------------
def part0_features(labels, images_dir, det, pe, clf):
    print("=" * 90)
    print("Part 0  基线几何特征 dump (state.py 原始阈值)")
    print("=" * 90)
    reset_baseline()
    for item in labels:
        fname = item["file"]
        img = cv2.imread(str(images_dir / fname))
        detections = det.detect_frame(img)
        civilians = [d for d in detections if d.is_person and d.class_name == "civilian"]
        poses = pe.estimate_persons(img, civilians)
        print(f"\n{fname}  (GT={item['gt_state']}, civilian 框数={len(civilians)})")
        for i, (d, p) in enumerate(zip(civilians, poses), start=1):
            if not p.is_ok:
                print(f"  框#{i}: 姿态失败 {p.status} {p.skip_reason}")
                continue
            sr = clf.classify(p)
            f = sr.features
            print(f"  框#{i}: Pred={sr.state} conf={sr.confidence:.2f}")
            print(f"        body_angle={f.body_angle_deg:5.1f}°  knee={f.knee_angle_deg:5.1f}°  "
                  f"wrist_ratio={f.wrist_height_ratio:.2f}  leg_ext={f.leg_extended}  "
                  f"torso={f.torso_len_px:.0f}px  vis={f.keypoint_visibility:.2f}")


# ---------------------------------------------------------------------------
# Part B: Sitting 辅助特征探针
# ---------------------------------------------------------------------------
def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def part_b_sitting_probe(target_files, images_dir, det, pe, clf):
    print("\n" + "=" * 90)
    print("Part B  Sitting 辅助特征探针 (001 Standing vs 005 Sitting)")
    print("=" * 90)
    reset_baseline()
    for fname in target_files:
        img = cv2.imread(str(images_dir / fname))
        detections = det.detect_frame(img)
        civilians = [d for d in detections if d.is_person and d.class_name == "civilian"]
        poses = pe.estimate_persons(img, civilians)
        print(f"\n{fname}")
        for i, (d, p) in enumerate(zip(civilians, poses), start=1):
            if not p.is_ok:
                print(f"  框#{i}: 姿态失败 {p.status}")
                continue
            L = p.landmarks
            sh = _mid((L[_LSH].x, L[_LSH].y), (L[_RSH].x, L[_RSH].y))
            hp = _mid((L[_LHP].x, L[_LHP].y), (L[_RHP].x, L[_RHP].y))
            an = _mid((L[_LAN].x, L[_LAN].y), (L[_RAN].x, L[_RAN].y))
            torso = abs(sh[1] - hp[1])  # 躯干垂直像素长
            vert_leg = abs(hp[1] - an[1])  # 髋-踝垂直跨距
            x1, y1, x2, y2 = d.bbox
            box_h = y2 - y1
            hip_pos = (hp[1] - y1) / box_h          # 髋在 bbox 高度中的相对位置 (0顶 1底)
            vert_leg_ratio = vert_leg / torso if torso > 1 else 0.0
            sh_an_vert = abs(sh[1] - an[1])
            body_compress = box_h / sh_an_vert if sh_an_vert > 1 else 0.0
            vis_ankle = (L[_LAN].visibility + L[_RAN].visibility) / 2
            vis_hip = (L[_LHP].visibility + L[_RHP].visibility) / 2
            vis_knee = (L[_LKN].visibility + L[_RKN].visibility) / 2
            sr = clf.classify(p)
            print(f"  框#{i}: Pred={sr.state}")
            print(f"        vert_leg_ratio(髋踝垂直/躯干垂直) = {vert_leg_ratio:.2f}   "
                  f"[站立预期 ~1.8-2.2, 坐姿预期明显减小]")
            print(f"        hip_pos_in_box(髋在框中相对高度) = {hip_pos:.2f}   "
                  f"[0=框顶 1=框底]")
            print(f"        body_compress(框高/肩踝垂直距)  = {body_compress:.2f}")
            print(f"        visibility: hip={vis_hip:.2f} knee={vis_knee:.2f} ankle={vis_ankle:.2f}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="V0.1.5 Step3 状态阈值标定实验台")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--images", default=str(ROOT / "data" / "test_images"))
    parser.add_argument("--labels", default=str(ROOT / "tests" / "test_labels.json"))
    args = parser.parse_args()

    labels = ev.load_labels(Path(args.labels))
    images_dir = Path(args.images)
    det = Detector(args.config)
    pe = PoseEstimator(args.config)
    clf = StateClassifier()
    print(f"[device] {det.device}  基线常量: {BASELINE}")

    # Part 0
    part0_features(labels, images_dir, det, pe, clf)

    # Part A — 阈值扫描
    print("\n" + "=" * 90)
    print("Part A  阈值扫描实验 (monkeypatch state.py 常量, 源码不变)")
    print("=" * 90)

    experiments = [
        ("Baseline", {}),
        ("Exp-Fall-1", {"LYING_ANGLE": 50.0}),
        ("Exp-Fall-2", {"LYING_ANGLE": 45.0}),
        ("Exp-Wave-1", {"WAVE_RATIO": 0.35}),
        ("Exp-Wave-2", {"WAVE_RATIO": 0.25}),
        ("Exp-Sit-1", {"SIT_KNEE": 135.0}),
        ("Exp-Sit-2", {"SIT_KNEE": 145.0}),
    ]
    for tag, ov in experiments:
        apply_overrides(ov)
        rows = run_all(labels, images_dir, det, pe, clf)
        summarize(tag, ov, rows)

    # Part B (全部有检出的图片, 用于确认新规则的副作用)
    part_b_sitting_probe(
        ["001_standing.jpg", "002_fall.jpg", "004_help.jpg", "005_multi_person.jpg"],
        images_dir, det, pe, clf,
    )

    reset_baseline()
    print("\n[done] 实验结束, state.py 常量已恢复基线 (未修改源码)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
