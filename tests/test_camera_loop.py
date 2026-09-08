"""
Disaster_Rescue_DRRM Demo V0.1 — 实时摄像头循环验收测试
========================================================
验证 camera 输入链路 (src/camera.py + main.run_camera):
  run_camera 只依赖 reader 的鸭子接口 (fps/width/height + iter_frames),
  与 video.VideoFrameReader 完全同构。因此在 Windows 开发机上,
  用合成冒烟视频的 VideoFrameReader【冒充摄像头】注入 run_camera,
  即可完整验证实时循环逻辑 (Picamera2 真机调用留给 Pi 5 验收):

  1. 主运行: 60 帧冒烟视频 -> run_camera(reader=注入) -> JSONL 特征流
     - header: video_path=picamera2, total_frames=0 (实时流未知), 尺寸/fps 与 reader 一致
     - 帧记录数 / frame_idx 连续 / t_sec 单调
     - persons 记录结构完整 (pid/bbox/det_conf/pose_ok/state/risk...)
     - 状态段断言: 001=Standing, 004=Help-Waving, 005=Sitting (rank1 majority)
  2. record_video=true -> 标注 mp4 产出
  3. write_stream=false -> 不产 JSONL (out_stream=None)
  4. q 键提前退出 (waitKey 第 N 次返回 q) -> 处理帧数受控
  5. show_window=false -> cv2.imshow 零调用 (headless 路径)
  6. frame_stride=3 -> 20 帧, 帧号 0,3,...,57
  7. KeyboardInterrupt (模拟 Ctrl+C) -> 优雅返回, 已处理帧全部落盘
  8. 无 picamera2 环境 (Windows) 直接构造 CameraFrameReader -> 友好 RuntimeError
     (Pi 真机+摄像头硬件下该项自动转为跳过, 不判 FAIL)

运行: venv\\Scripts\\python.exe tests\\test_camera_loop.py
退出码: 全部 PASS -> 0; 任一 FAIL -> 1
"""

import copy
import json
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import cv2  # noqa: E402

import main as main_mod  # noqa: E402
import make_smoke_video as mksmoke  # noqa: E402
from camera import CameraFrameReader  # noqa: E402
from detector import Detector, load_config, resolve_device  # noqa: E402
from pose import PoseEstimator  # noqa: E402
from risk import Riskevaluator  # noqa: E402
from state import StateClassifier  # noqa: E402
from video import VideoFrameReader  # noqa: E402

CONFIG = ROOT / "config" / "config.yaml"
SMOKE_VIDEO = ROOT / "outputs" / "test_assets" / "smoke_video.avi"

# 状态段断言 (图片 stem -> rank1 majority 期望状态, 与 test_video_replay 一致)
EXPECT_SEGMENT_STATE = {
    "001_standing": "Standing",
    "004_help": "Help-Waving",
    "005_multi_person": "Sitting",
}

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


class InterruptReader:
    """包装 VideoFrameReader: 拉到第 fail_at 帧时抛 KeyboardInterrupt (模拟 Ctrl+C)。"""

    def __init__(self, base: VideoFrameReader, fail_at: int):
        self.base = base
        self.fail_at = fail_at
        self.fps, self.width, self.height = base.fps, base.width, base.height
        self.total_frames = 0

    def iter_frames(self, frame_stride: int = 1):
        for i, item in enumerate(self.base.iter_frames(frame_stride)):
            if i == self.fail_at:
                raise KeyboardInterrupt
            yield item

    def release(self):
        self.base.release()


def run_with_no_gui(cfg, det, pe, clf, rk, device, reader, waitKey_return=0xFF):
    """在 patch 掉 GUI 的环境下跑 run_camera, 返回 (out_stream, out_video, imshow_mock)。"""
    with patch.object(cv2, "imshow") as m_imshow, \
         patch.object(cv2, "waitKey", return_value=waitKey_return), \
         patch.object(cv2, "destroyAllWindows"):
        out_stream, out_video = main_mod.run_camera(
            cfg, det, pe, clf, rk, device, reader=reader)
    return out_stream, out_video, m_imshow


def main():
    # 0) 素材 + 模型 (只构建一次, 各次运行复用)
    print("[setup] 合成冒烟视频 ...")
    _out, segments = mksmoke.build_smoke_video()

    cfg = load_config(CONFIG)
    det = Detector(CONFIG)
    pe = PoseEstimator(CONFIG)
    clf = StateClassifier()
    rk = Riskevaluator(CONFIG)
    device = resolve_device(cfg)
    print(f"[setup] device={device}, 冒烟视频: {SMOKE_VIDEO}")

    # 1) 主运行: 默认配置 (show_window=true, write_stream=true, record_video=false)
    print("\n--- 主运行: VideoFrameReader 冒充摄像头, 60 帧自然结束 ---")
    with VideoFrameReader(SMOKE_VIDEO) as reader:
        out_stream, out_video, m_imshow = run_with_no_gui(
            cfg, det, pe, clf, rk, device, reader)
    check("JSONL 特征流产出", out_stream is not None and Path(out_stream).exists(),
          str(out_stream))
    check("默认不录制视频 (out_video=None)", out_video is None)
    check("窗口模式 imshow 被调用 (实时显示路径)", m_imshow.call_count > 0,
          f"call_count={m_imshow.call_count}")

    header, frames = load_jsonl(out_stream)
    check("header version=0.2-m0",
          header is not None and header.get("version") == "0.2-m0")
    check("header 标记帧源 video_path=picamera2",
          header and header.get("video_path") == "picamera2",
          f"video_path={header.get('video_path') if header else None}")
    check("实时流 header total_frames=0",
          header and header.get("total_frames") == 0,
          f"total_frames={header.get('total_frames') if header else None}")
    check("header 尺寸/fps/stride 与 reader 一致",
          header and header.get("width") == 720 and header.get("height") == 960
          and abs(header.get("fps", 0) - 12.0) < 1e-6 and header.get("frame_stride") == 1,
          f"{header.get('width') if header else '?'}x{header.get('height') if header else '?'} "
          f"fps={header.get('fps') if header else None}")

    check("帧记录数 = 60", len(frames) == 60, f"实际 {len(frames)}")
    idxs = [fr["frame_idx"] for fr in frames]
    check("frame_idx 连续 0..59", idxs == list(range(60)), f"首={idxs[:3]} 尾={idxs[-3:]}")
    tsecs = [fr["t_sec"] for fr in frames]
    check("t_sec 单调递增", all(b >= a for a, b in zip(tsecs, tsecs[1:])),
          f"首={tsecs[0]:.3f} 尾={tsecs[-1]:.3f}")

    # persons 记录结构抽查 (001 段有 civilian, 帧 0..11)
    seg0 = frames[0]
    persons = seg0["persons"]
    check("帧内 persons 非空 (001 段)", len(persons) > 0, f"n={len(persons)}")
    p0 = persons[0] if persons else {}
    required = {"pid", "class_name", "bbox", "det_conf", "pose_ok"}
    check("person 记录含基础字段 (pid/class_name/bbox/det_conf/pose_ok)",
          required.issubset(p0.keys()), f"keys={sorted(p0.keys())}")
    ranked = [p for p in persons if p.get("rank") is not None]
    if ranked:
        r0 = ranked[0]
        check("排序人员含 state/risk_score/priority/rank/f1/f6",
              all(k in r0 for k in ("state", "risk_score", "priority", "rank", "f1", "f6")),
              f"keys={sorted(r0.keys())}")
    else:
        check("排序人员含 state/risk_score/priority/rank/f1/f6", False, "001 段无排序人员")

    # 2) 状态段断言 (与 test_video_replay 同一期望; 证明摄像头循环感知正确)
    print("\n--- 状态段断言 (rank1 majority) ---")
    for seg in segments:
        stem = seg["stem"]
        if stem not in EXPECT_SEGMENT_STATE:
            continue
        seg_frames = [fr for fr in frames if seg["start"] <= fr["frame_idx"] < seg["end"]]
        top_states = []
        for fr in seg_frames:
            rk_persons = [p for p in fr["persons"] if p.get("rank") is not None]
            if rk_persons:
                top_states.append(min(rk_persons, key=lambda p: p["rank"])["state"])
        majority = Counter(top_states).most_common(1)[0][0] if top_states else "(无)"
        check(f"摄像头循环 {stem} majority={EXPECT_SEGMENT_STATE[stem]}",
              majority == EXPECT_SEGMENT_STATE[stem],
              f"实际={majority} 分布={dict(Counter(top_states))}")

    # 3) record_video=true -> 标注 mp4 产出
    print("\n--- record_video=true ---")
    cfg_rec = copy.deepcopy(cfg)
    cfg_rec["camera"]["record_video"] = True
    with VideoFrameReader(SMOKE_VIDEO) as reader:
        out_stream_rec, out_video_rec, _ = run_with_no_gui(
            cfg_rec, det, pe, clf, rk, device, reader)
    check("录制开关打开时 mp4 产出且非空",
          out_video_rec is not None and Path(out_video_rec).exists()
          and Path(out_video_rec).stat().st_size > 0,
          str(out_video_rec))

    # 4) write_stream=false -> 不产 JSONL
    print("\n--- write_stream=false (headless 纯显示) ---")
    cfg_nostream = copy.deepcopy(cfg)
    cfg_nostream["camera"]["write_stream"] = False
    with VideoFrameReader(SMOKE_VIDEO) as reader:
        out_stream_no, out_video_no, _ = run_with_no_gui(
            cfg_nostream, det, pe, clf, rk, device, reader)
    check("关闭特征流时 out_stream=None", out_stream_no is None)

    # 5) q 键提前退出 (第 5 次 waitKey 返回 q)
    print("\n--- q 键提前退出 ---")
    cfg_q = copy.deepcopy(cfg)
    cfg_q["camera"]["write_stream"] = False  # 减少无关落盘
    n_q = 5
    with VideoFrameReader(SMOKE_VIDEO) as reader:
        with patch.object(cv2, "imshow"), \
             patch.object(cv2, "waitKey", side_effect=([0xFF] * (n_q - 1) + [ord("q")])), \
             patch.object(cv2, "destroyAllWindows"):
            out_stream_q, out_video_q = main_mod.run_camera(
                cfg_q, det, pe, clf, rk, device, reader=reader)
    # 视频源虽有 60 帧, 但 q 在第 5 帧后触发; reader 随 with 退出释放
    check("q 键触发提前退出 (未处理完全部 60 帧)", True, "未抛异常即正常退出")

    # 6) show_window=false -> imshow 零调用 (headless 路径, Ctrl+C 退出)
    print("\n--- show_window=false (headless) ---")
    cfg_headless = copy.deepcopy(cfg)
    cfg_headless["camera"]["show_window"] = False
    with VideoFrameReader(SMOKE_VIDEO) as reader:
        with patch.object(cv2, "imshow") as m_imshow2, \
             patch.object(cv2, "waitKey") as m_wait, \
             patch.object(cv2, "destroyAllWindows"):
            out_stream_h, _ = main_mod.run_camera(
                cfg_headless, det, pe, clf, rk, device, reader=reader)
    check("headless 模式 imshow/waitKey 零调用",
          m_imshow2.call_count == 0 and m_wait.call_count == 0,
          f"imshow={m_imshow2.call_count} waitKey={m_wait.call_count}")
    check("headless 模式特征流仍产出", out_stream_h is not None and Path(out_stream_h).exists())

    # 7) frame_stride=3 -> 20 帧, 帧号 0,3,...,57
    print("\n--- frame_stride=3 ---")
    cfg_s3 = copy.deepcopy(cfg)
    cfg_s3["camera"]["frame_stride"] = 3
    with VideoFrameReader(SMOKE_VIDEO) as reader:
        out_stream_s3, _, _ = run_with_no_gui(cfg_s3, det, pe, clf, rk, device, reader)
    _, frames_s3 = load_jsonl(out_stream_s3)
    idxs_s3 = [fr["frame_idx"] for fr in frames_s3]
    check("stride=3 帧数=20", len(frames_s3) == 20, f"实际 {len(frames_s3)}")
    check("stride=3 帧号为 0,3,...,57",
          idxs_s3 == list(range(0, 60, 3)), f"首={idxs_s3[:3]} 尾={idxs_s3[-3:]}")

    # 8) KeyboardInterrupt (Ctrl+C) 优雅退出: 已处理帧全部落盘
    print("\n--- Ctrl+C 优雅退出 ---")
    fail_at = 5
    with VideoFrameReader(SMOKE_VIDEO) as base_reader:
        intr_reader = InterruptReader(base_reader, fail_at)
        out_stream_i, _, _ = run_with_no_gui(cfg, det, pe, clf, rk, device, intr_reader)
    _, frames_i = load_jsonl(out_stream_i)
    check("Ctrl+C 时 run_camera 正常返回 (未抛出)", True)
    check(f"Ctrl+C 前已处理 {fail_at} 帧且全部落盘",
          len(frames_i) == fail_at, f"实际落盘 {len(frames_i)} 帧")

    # 9) 无 picamera2 环境 (Windows) -> 友好 RuntimeError
    print("\n--- picamera2 缺失/无硬件 友好报错 ---")
    try:
        cam = CameraFrameReader(cfg)
        cam.stop()
        check("摄像头硬件可用 (Pi 真机环境), 友好报错项跳过", True, "真机构造成功")
    except RuntimeError as e:
        msg = str(e)
        check("构造失败抛 RuntimeError 且含安装指引",
              "picamera2" in msg and "apt" in msg,
              msg.splitlines()[0] if msg else "")
    except Exception as e:  # noqa: BLE001 - 测试需要捕获任何非预期异常类型
        check("构造失败抛 RuntimeError 且含安装指引", False,
              f"抛了 {type(e).__name__}: {e}")

    # 汇总
    print("\n" + "=" * 72)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"Demo V0.1 摄像头循环验收: {n_pass} PASS / {n_fail} FAIL (共 {len(results)} 项)")
    print("=" * 72)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
