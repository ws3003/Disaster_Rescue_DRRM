"""
Disaster_Rescue_DRRM V0.2-M0 — 视频回放框架验收测试
====================================================
验证 PRD 4.2 视频回放 + 逐帧感知特征流:
  1. 合成冒烟视频 (make_smoke_video: 5 张测试图各重复 12 帧, 60 帧/12fps)
  2. 走 main.run_video 真实入口产出 标注 mp4 + JSONL 特征流
  3. JSONL header / 帧数 / 帧号 / 时间轴 校验
  4. golden 一致性: 视频各段 vs 图片模式 process_one 同图结果
     冒烟视频使用 HFYU 无损编码, 解码帧与写入像素逐位一致,
     因此视频管线结果必须与图片模式【零漂移】:
     - state / priority / rank / class_name / pid 集合: 完全一致
     - risk_score / f1 / f6 / det_conf / pose_vis / state_conf: 容差 1e-6
     - bbox: 容差 0.5 px
     (有损编码 mp4v 的压缩噪声另见 V0.2_M0_report.md 实测记录, 属感知鲁棒性话题)
  5. 状态段断言: 001=Standing, 004=Help-Waving, 005=Sitting (rank1 majority)
  6. 边界: frame_stride=3 (20 帧, 帧号 0,3,...57); stream_landmarks=false 无 landmarks
  7. 无人帧: append_frame_record 空 persons 不崩溃且帧索引连续

运行: venv\\Scripts\\python.exe tests\\test_video_replay.py
退出码: 全部 PASS -> 0; 任一 FAIL -> 1
"""

import copy
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import cv2  # noqa: E402

import main as main_mod  # noqa: E402
import make_smoke_video as mksmoke  # noqa: E402
import writer  # noqa: E402
from detector import Detector, load_config, resolve_device  # noqa: E402
from pose import PoseEstimator  # noqa: E402
from risk import Riskevaluator  # noqa: E402
from state import StateClassifier  # noqa: E402

CONFIG = ROOT / "config" / "config.yaml"
IMAGES_DIR = ROOT / "data" / "test_images"
SMOKE_VIDEO = ROOT / "outputs" / "test_assets" / "smoke_video.avi"

# 状态段断言 (图片 stem -> rank1 majority 期望状态)
EXPECT_SEGMENT_STATE = {
    "001_standing": "Standing",
    "004_help": "Help-Waving",
    "005_multi_person": "Sitting",
}

# HFYU 无损 roundtrip: 视频帧与参考帧像素逐位一致, 感知结果必须零漂移
FLOAT_TOL = 1e-6
BBOX_TOL = 0.5

results = []  # (name, ok, detail)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def load_jsonl(path):
    header, frames = None, []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj["type"] == "header":
                header = obj
            elif obj["type"] == "frame":
                frames.append(obj)
    return header, frames


def compare_persons(ref, vid):
    """按 pid 匹配比对单帧人员记录; 返回 (issues, max_float_drift)。"""
    issues = []
    max_drift = 0.0
    ref_by_pid = {p["pid"]: p for p in ref}
    vid_by_pid = {p["pid"]: p for p in vid}
    if set(ref_by_pid) != set(vid_by_pid):
        issues.append(f"pid 集合不一致 ref={sorted(ref_by_pid)} vid={sorted(vid_by_pid)}")
        return issues, max_drift
    for pid, rp in ref_by_pid.items():
        vp = vid_by_pid[pid]
        tag = f"pid{pid}"
        if rp["class_name"] != vp["class_name"]:
            issues.append(f"{tag} class_name {rp['class_name']} != {vp['class_name']}")
        if rp.get("state") != vp.get("state"):
            issues.append(f"{tag} state {rp.get('state')} != {vp.get('state')}")
        if rp.get("priority") != vp.get("priority"):
            issues.append(f"{tag} priority {rp.get('priority')} != {vp.get('priority')}")
        if rp.get("rank") != vp.get("rank"):
            issues.append(f"{tag} rank {rp.get('rank')} != {vp.get('rank')}")
        for k in ("risk_score", "f1", "f6", "det_conf", "pose_vis", "state_conf"):
            rv, vv = rp.get(k), vp.get(k)
            if rv is None and vv is None:
                continue
            if rv is None or vv is None:
                issues.append(f"{tag} {k} 一方为 null: ref={rv} vid={vv}")
                continue
            drift = abs(rv - vv)
            max_drift = max(max_drift, drift)
            if drift > FLOAT_TOL:
                issues.append(f"{tag} {k} 漂移 {drift:.4f} > {FLOAT_TOL} (ref={rv} vid={vv})")
        for ax, (a, b) in enumerate(zip(rp["bbox"], vp["bbox"])):
            if abs(a - b) > BBOX_TOL:
                issues.append(f"{tag} bbox 轴{ax} 漂移 {abs(a - b):.1f}px > {BBOX_TOL}")
    return issues, max_drift


def main():
    # 0) 素材 + 模型 (只构建一次, 各次运行复用)
    # 生成 (或覆盖重生成, 内容确定) 冒烟视频并拿到段映射
    print("[setup] 合成冒烟视频 ...")
    _out, segments = mksmoke.build_smoke_video()

    cfg = load_config(CONFIG)
    det = Detector(CONFIG)
    pe = PoseEstimator(CONFIG)
    clf = StateClassifier()
    rk = Riskevaluator(CONFIG)
    device = resolve_device(cfg)
    print(f"[setup] device={device}, 段映射: "
          f"{[(s['stem'], s['start'], s['end']) for s in segments]}")

    # 1) 主运行: stride=1, landmarks=true (走 run_video 真实入口)
    print("\n--- 主运行: stride=1, stream_landmarks=true ---")
    out_video, out_stream = main_mod.run_video(cfg, SMOKE_VIDEO, det, pe, clf, rk, device)
    check("标注视频产出", Path(out_video).exists() and Path(out_video).stat().st_size > 0,
          str(out_video))
    check("JSONL 特征流产出", Path(out_stream).exists(), str(out_stream))

    header, frames = load_jsonl(out_stream)
    check("header 存在且 version=0.2-m0",
          header is not None and header.get("version") == "0.2-m0",
          f"version={header.get('version') if header else None}")
    check("header frame_stride=1", header and header.get("frame_stride") == 1)
    check("header 尺寸/fps 元数据完整",
          header and header.get("width") > 0 and header.get("height") > 0
          and header.get("fps") > 0 and header.get("total_frames") == 60,
          f"fps={header.get('fps') if header else None} "
          f"{header.get('width') if header else '?'}x{header.get('height') if header else '?'} "
          f"total={header.get('total_frames') if header else None}")

    check("帧记录数 = 60", len(frames) == 60, f"实际 {len(frames)}")
    idxs = [fr["frame_idx"] for fr in frames]
    check("frame_idx 连续 0..59", idxs == list(range(60)), f"首={idxs[:3]} 尾={idxs[-3:]}")
    tsecs = [fr["t_sec"] for fr in frames]
    check("t_sec 单调递增", all(b > a for a, b in zip(tsecs, tsecs[1:])),
          f"首={tsecs[0]:.3f} 尾={tsecs[-1]:.3f}")

    # 2) golden 一致性 + 状态段断言
    print("\n--- golden: 视频段 vs 图片模式 (同图 process_one) ---")
    overall_drift = 0.0
    for seg in segments:
        stem = seg["stem"]
        img_path = IMAGES_DIR / seg["image"]
        # 参考帧必须与视频帧同一数组: 原图经同一 letterbox 变换 (HFYU 无损)
        img = mksmoke.letterbox(cv2.imread(str(img_path)), mksmoke.SMOKE_SIZE)
        _, _, ref_records = main_mod.process_one(
            cfg, img, det, pe, clf, rk, img_path, device, verbose=False)

        seg_frames = [fr for fr in frames if seg["start"] <= fr["frame_idx"] < seg["end"]]
        seg_issues = []
        for fr in seg_frames:
            issues, drift = compare_persons(ref_records, fr["persons"])
            overall_drift = max(overall_drift, drift)
            seg_issues += [f"帧{fr['frame_idx']}: {i}" for i in issues]
        check(f"golden {stem} ({len(seg_frames)} 帧)", not seg_issues,
              "; ".join(seg_issues[:3]) + (" ..." if len(seg_issues) > 3 else ""))

        # rank1 majority 状态
        if stem in EXPECT_SEGMENT_STATE:
            top_states = []
            for fr in seg_frames:
                ranked = [p for p in fr["persons"] if p.get("rank") is not None]
                if ranked:
                    top = min(ranked, key=lambda p: p["rank"])
                    top_states.append(top["state"])
            majority = Counter(top_states).most_common(1)[0][0] if top_states else "(无)"
            check(f"状态段 {stem} majority={EXPECT_SEGMENT_STATE[stem]}",
                  majority == EXPECT_SEGMENT_STATE[stem],
                  f"实际={majority} 分布={dict(Counter(top_states))}")
    print(f"[info] golden 浮点最大漂移: {overall_drift:.4f} (容差 {FLOAT_TOL})")

    # landmarks 存在性抽查 (004/005 段有有效姿态)
    has_lm = any("landmarks" in p
                 for fr in frames for p in fr["persons"] if p.get("pose_ok"))
    check("stream_landmarks=true 时含 33 关键点", has_lm)

    # 3) 边界: frame_stride=3
    print("\n--- 边界: frame_stride=3 ---")
    cfg_s3 = copy.deepcopy(cfg)
    cfg_s3["video"]["frame_stride"] = 3
    _, out_stream_s3 = main_mod.run_video(cfg_s3, SMOKE_VIDEO, det, pe, clf, rk, device)
    _, frames_s3 = load_jsonl(out_stream_s3)
    idxs_s3 = [fr["frame_idx"] for fr in frames_s3]
    check("stride=3 帧数=20", len(frames_s3) == 20, f"实际 {len(frames_s3)}")
    check("stride=3 帧号为 0,3,...,57",
          idxs_s3 == list(range(0, 60, 3)), f"首={idxs_s3[:3]} 尾={idxs_s3[-3:]}")

    # 4) 边界: stream_landmarks=false
    print("\n--- 边界: stream_landmarks=false ---")
    cfg_nolm = copy.deepcopy(cfg)
    cfg_nolm["video"]["stream_landmarks"] = False
    _, out_stream_nolm = main_mod.run_video(cfg_nolm, SMOKE_VIDEO, det, pe, clf, rk, device)
    _, frames_nolm = load_jsonl(out_stream_nolm)
    no_lm = all("landmarks" not in p for fr in frames_nolm for p in fr["persons"])
    check("stream_landmarks=false 时无 landmarks 字段", no_lm)
    check("landmarks=false 帧数仍=60", len(frames_nolm) == 60, f"实际 {len(frames_nolm)}")

    # 5) 无人帧: 空 persons 记录可写出且可解析
    print("\n--- 边界: 空 persons 帧记录 ---")
    tmp_jsonl = ROOT / "outputs" / "test_assets" / "_empty_frame_check.jsonl"
    writer.write_stream_header(tmp_jsonl, video_path="(synthetic)", fps=12.0, width=640,
                               height=480, total_frames=1, frame_stride=1, device=device)
    writer.append_frame_record(tmp_jsonl, 0, 0.0, [])
    h0, f0 = load_jsonl(tmp_jsonl)
    check("空 persons 帧写出/解析正常", h0 is not None and len(f0) == 1 and f0[0]["persons"] == [])
    tmp_jsonl.unlink(missing_ok=True)

    # 汇总
    print("\n" + "=" * 72)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"V0.2-M0 验收: {n_pass} PASS / {n_fail} FAIL (共 {len(results)} 项)")
    print("=" * 72)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
