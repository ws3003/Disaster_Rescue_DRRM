"""
Disaster_Rescue_DRRM V0.1.5 — 展示增强入口 (单帧/目录批量 + JSON 结果 + 排序面板)
================================================================================
流程 (保持 V0.1 pipeline, 不改变算法逻辑):
  config → 读图 → YOLO检测 → MediaPipe姿态 → 状态识别 → DRRM风险 → 救援排序 → 可视化输出
只做编排 / 可视化 / 结果落盘, 复用已有算法模块:
  detector.Detector / pose.PoseEstimator / state.StateClassifier /
  risk.Riskevaluator / ranking.rank / resolve_device
以及新增 writer (JSON 结果写出)。

输入支持 (V0.1.5 Task1):
  1) 单图片:  --input data/test.jpg            (兼容 V0.1, 输出 v0.1_result.jpg)
  2) 图片目录: --input data/demo/              (自动批量处理目录下所有图片)
  3) 视频文件 (V0.2-M0): --input data/xxx.mp4  (逐帧回放, 输出标注 mp4 + JSONL 特征流)
  4) 实时摄像头 (Demo V0.1): --camera          (Pi 5 + Picamera2, 实时窗口 + JSONL 特征流;
                                               或 config input.type: "camera")
JSON 输出 (V0.1.5 Task2):
  每张图 -> outputs/json/<图片主名>.json
视频特征流 (V0.2-M0, PRD 4.2 三层解耦接口):
  每个视频 -> outputs/stream/<视频主名>.jsonl (header + 逐帧感知记录)
  标注视频 -> outputs/videos/<视频主名>_annotated.mp4
可视化增强 (V0.1.5 Task3):
  保留 bbox + 33 关键点骨架 + 状态 + risk 叠加, 新增左上角救援排序榜 (Top-K)

V0.2-M0 约束: 视频模式逐帧复用 process_one, 算法模块零改动;
  不含 tracking / TSF / 动态 DRRM (pid 为帧内编号, 每帧重置)。
"""

import argparse
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
from mediapipe.python.solutions.pose import POSE_CONNECTIONS

import writer
from camera import CameraFrameReader
from detector import Detection, Detector, load_config, resolve_device
from pose import PoseEstimator, PoseResult
from ranking import PersonEvidence, rank
from risk import Riskevaluator
from state import StateClassifier
from video import VideoFrameReader, VideoFrameWriter

# 可处理的图片扩展名
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp")
# 视频扩展名兜底 (config video.extensions 优先)
FALLBACK_VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv")

# 显示配色 (BGR)
COLOR_PERSON = (0, 200, 0)        # civilian bbox 绿 (救援对象)
COLOR_RESCUER = (255, 0, 0)       # rescuer bbox 蓝 (仅标注, 不参与排序)
COLOR_NONPERSON = (160, 160, 160)  # 非人员(动物) bbox 灰
COLOR_SKELETON = (220, 220, 0)     # 骨架 青
COLOR_LOW = (0, 200, 0)            # LOW 风险 绿
COLOR_MED = (0, 165, 255)          # MEDIUM 风险 橙
COLOR_HIGH = (0, 0, 255)           # HIGH 风险 红
COLOR_TEXT_BG = (40, 40, 40)
COLOR_TEXT_WHITE = (240, 240, 240)


def _risk_color(score: float):
    # 与 ranking.py _priority 分级阈值保持一致 (V0.1.5 Step5: 0.75/0.50 -> 0.60/0.45)
    if score >= 0.60:
        return COLOR_HIGH
    if score >= 0.45:
        return COLOR_MED
    return COLOR_LOW


def _draw_label(img, text: str, org, color, font_scale: float, thickness: int, bg: bool = True):
    """在指定位置绘带底色(或不带)的文字标签。"""
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
        cv2.line(img, pts[a], pts[b], COLOR_SKELETON,
                 thickness if isinstance(thickness, int) else thickness[0])


def _draw_ranking_panel(img, ranked, top_k: int, fs, thickness: int, pid_of: dict | None = None):
    """在左上角绘制半透明救援排序榜 (保留 Top-K 名, 仅 civilian)。"""
    if not ranked:
        return
    top = ranked[:max(0, top_k)]
    header = f"Rescue Ranking Top-{len(top)}"
    lines = [header]
    for rr in top:
        pid = pid_of[id(rr.detection)] if (pid_of and rr.detection is not None) else "-"
        lines.append(f"#{rr.rank} id{pid} {rr.state} risk={rr.risk_score:.2f} {rr.priority}")

    (tw, th), _ = cv2.getTextSize(header, cv2.FONT_HERSHEY_SIMPLEX, fs, max(1, thickness))
    line_h = th + 12
    panel_w = max(tw, 300) + 24
    panel_h = line_h * len(lines) + 16

    h, w = img.shape[:2]
    x0, y0 = 12, 12
    x1, y1 = min(w, x0 + panel_w), min(h, y0 + panel_h)
    panel_w, panel_h = x1 - x0, y1 - y0

    # 半透明深色底
    overlay = img.copy()
    cv2.rectangle(overlay, (x0, y0), (x1, y1), COLOR_TEXT_BG, -1)
    img[y0:y1, x0:x1] = cv2.addWeighted(overlay[y0:y1, x0:x1], 0.6, img[y0:y1, x0:x1], 0.4, 0)
    # 标题
    _draw_label(img, header, (x0 + 10, y0 + line_h - 4), COLOR_TEXT_WHITE, fs, thickness, bg=False)
    # 各排位
    for i, rr in enumerate(top, start=1):
        color = _risk_color(rr.risk_score)
        _draw_label(img, lines[i], (x0 + 18, y0 + line_h * (i + 1) - 4), color, fs, thickness, bg=False)


def collect_inputs(src: Path) -> list:
    """根据输入路径整理待处理图片列表 (单文件 || 目录批量)。"""
    if src.is_file():
        return [src]
    if src.is_dir():
        return sorted(p for p in src.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    return []


def _build_frame_records(cfg, img, person_dets, pid_of, task, rank_lookup):
    """
    V0.2-M0: 组装逐帧感知记录 (JSONL persons 字段), 供视频特征流落盘。
    纯读取 process_one 已有中间结果, 不参与算法/可视化。
      - pid: 帧内编号 (M0 无跟踪器, 每帧重置; M2 追加 track_id);
      - landmarks: 归一化坐标 [x/W, y/H, visibility], 配合流头 W/H 还原像素;
      - rescuer / 姿态失败: pose_ok=false, 无状态/风险字段;
      - 未进入排序的 civilian: f1/f6/risk_score/priority/rank 为 null。
    """
    h, w = img.shape[:2]
    stream_lm = bool(cfg.get("video", {}).get("stream_landmarks", True))
    records = []
    for d in person_dets:
        rec = {
            "pid": pid_of[id(d)],
            "class_name": d.class_name,
            "bbox": [round(float(v), 2) for v in d.bbox],
            "det_conf": round(float(d.confidence), 4),
        }
        t = task.get(id(d))
        if d.class_name == "rescuer" or t is None:
            # rescuer 不进分析管线 (与图片模式一致); 无 task 记录同理
            rec["pose_ok"] = False
            records.append(rec)
            continue
        pose, st = t["pose"], t["state"]
        ok = bool(pose and pose.is_ok)
        rec["pose_ok"] = ok
        rec["pose_vis"] = round(float(pose.visibility_mean), 4) if pose else 0.0
        if ok and stream_lm and pose.landmarks:
            rec["landmarks"] = [
                [round(lm.x / w, 4), round(lm.y / h, 4), round(float(lm.visibility), 2)]
                for lm in pose.landmarks
            ]
        if st is not None:
            rec["state"] = st.state
            rec["state_conf"] = round(float(st.confidence), 4)
        else:
            rec["state"] = None
            rec["state_conf"] = None
        rr = rank_lookup.get(id(d))
        if rr is not None:
            rec.update({
                "f1": rr.details["f1"],
                "f6": rr.details["f6"],
                "risk_score": rr.risk_score,
                "priority": rr.priority,
                "rank": rr.rank,
            })
        else:
            rec.update({"f1": None, "f6": None, "risk_score": None,
                        "priority": None, "rank": None})
        records.append(rec)
    return records


def process_one(cfg, img, det, pe, clf, rk, img_path, device, verbose: bool = True):
    """
    对单张图片/单帧执行完整 pipeline, 返回 (标注图, JSON payload, frame_records)。
    适用 V0.1 原有逻辑: 检测 -> 姿态 -> 状态 -> 风险 -> 排序 -> 可视化。

    V0.1.5 稳定化约束:
      - 每名人员拥有图内唯一 id (pid, 按检测顺序), 可视化标签与 JSON 共用;
      - rescuer 仅显示 bbox+类名, 不做姿态/状态/风险, 不进排序;
      - rank 回填通过 RankingResult.detection 对象身份精确匹配, 不按 class_name。
    V0.2-M0:
      - 算法逻辑零改动, 仅追加第三个返回值 frame_records (逐帧感知记录, 见 _build_frame_records);
      - verbose=False 时静默控制台汇总 (视频逐帧调用避免刷屏); 图片模式默认 True 不变。
    """
    # 1) 检测 (含全部类别)
    persons_all = det.detect_frame(img)

    # 人员唯一 id (civilian+rescuer 统一按检测顺序编号); civilian/rescuer 分流
    person_dets = [d for d in persons_all if d.is_person]
    pid_of = {id(d): i for i, d in enumerate(person_dets, start=1)}
    civilians = [d for d in person_dets if d.class_name == "civilian"]
    rescuers = [d for d in person_dets if d.class_name == "rescuer"]

    # 2) 姿态 (仅 civilian; rescuer 不进分析管线)
    civilian_poses = pe.estimate_persons(img, civilians)

    # 3) 状态 + 4) 风险 (仅 civilian)
    task: dict = {}
    for det_obj, pose in zip(civilians, civilian_poses):
        state = clf.classify(pose) if pose and pose.is_ok else None
        risk = rk.assess(det_obj, pose, state) if (pose and state) else None
        task[id(det_obj)] = {"pose": pose, "state": state, "risk": risk}

    # 5) 排序 (仅 civilian; PersonEvidence.detection 持有源 Detection 引用)
    evidences = [
        PersonEvidence(detection=d, state=task[id(d)]["state"], risk=task[id(d)]["risk"],
                       pose_vis=task[id(d)]["pose"].visibility_mean if task[id(d)]["pose"] else 0.0)
        for d in civilians if task[id(d)]["risk"] is not None
    ]
    ranked = rank(evidences)
    # 身份映射: 用 Detection 对象身份精确回填, 禁止按 class_name 匹配 (同类多人会错位)
    rank_lookup = {id(rr.detection): rr for rr in ranked}

    # 6) 可视化: bbox + 类别 + 骨架 + 状态 + risk, 叠加排序面板
    vcfg = cfg["visualize"]
    fs = float(vcfg["panel_font_scale"])
    thk = int(vcfg["box_thickness"])
    annot = img.copy()
    for det_obj in persons_all:
        x1, y1, x2, y2 = [int(v) for v in det_obj.bbox]
        if not det_obj.is_person:
            # 非人员 (动物): 灰框 + 类名, 不分析
            cv2.rectangle(annot, (x1, y1), (x2, y2), COLOR_NONPERSON, thk)
            _draw_label(annot, det_obj.class_name, (x1, y1 - 4), COLOR_NONPERSON, fs, thk)
            continue
        pid = pid_of[id(det_obj)]
        if det_obj.class_name == "rescuer":
            # rescuer: 蓝框 + id+类名, 无骨架/状态/risk/排名
            cv2.rectangle(annot, (x1, y1), (x2, y2), COLOR_RESCUER, thk)
            _draw_label(annot, f"#{pid} rescuer", (x1, y1 - 4), COLOR_RESCUER, fs, thk)
            continue
        # civilian (救援对象): 绿框 + id, 框下方标注 rank/状态/风险
        cv2.rectangle(annot, (x1, y1), (x2, y2), COLOR_PERSON, thk)
        _draw_label(annot, f"#{pid} civilian", (x1, y1 - 4), COLOR_PERSON, fs, thk)
        t = task[id(det_obj)]
        pose = t["pose"]
        info = rank_lookup.get(id(det_obj))
        if info is not None:
            sub = f"R#{info.rank} {info.state} {info.risk_score:.2f}"
            rc = _risk_color(info.risk_score)
        else:
            st = t["state"]
            sub = st.state if st else "n/a"
            rc = COLOR_PERSON
        if vcfg["draw_skeleton"] and pose and pose.is_ok:
            _draw_skeleton(annot, pose, thk)
        if vcfg.get("draw_states", True):
            _draw_label(annot, sub, (x1, y2 + 2), rc, fs, thk - 1)

    # 排序面板 (Top-K, 仅 civilian)
    if vcfg.get("draw_ranking_panel", True):
        top_k = int(vcfg.get("top_k", len(ranked)))
        _draw_ranking_panel(annot, ranked, top_k, fs, thk, pid_of)

    # 7) JSON payload (仅进入排序的 civilian; id 为图内唯一 pid, 与框标签一致)
    persons_json = []
    for rr in ranked:
        st = task[id(rr.detection)]["state"]
        persons_json.append({
            "id": pid_of[id(rr.detection)],
            "class_name": rr.class_name,
            "state": rr.state,
            "confidence": round(st.confidence, 4) if st else None,
            "risk_score": rr.risk_score,
            "priority": rr.priority,
            "rank": rr.rank,
            "f1": rr.details["f1"],
            "f6": rr.details["f6"],
        })
    payload = writer.make_payload(img_path, device, persons_json)

    # V0.2-M0: 逐帧感知记录 (视频特征流用; 图片模式不消费)
    frame_records = _build_frame_records(cfg, img, person_dets, pid_of, task, rank_lookup)

    # 控制台汇总
    if verbose:
        _console_summary(pid_of, civilians, rescuers, persons_all, task, ranked)
    return annot, payload, frame_records


def _feed_agent(agent_runner, vision_tool, person_events: list) -> list:
    """把感知 person_events 接入生产输入闭环(仅 --agent 时调用)。

    只做: 视觉快照写入(vision_adapter->VisionTool) + frame_record 转换 +
    调用 Agent 正式入口 process_frame()。不在此重实现任何 Agent 逻辑。
    """
    if not person_events:
        return []
    # 1) 视觉快照: person_events -> standard snapshot -> VisionTool.update_snapshot
    from src.tools.vision_adapter import to_snapshot
    vision_tool.run(action="update_snapshot", snapshot=to_snapshot(person_events))
    # 2) frame_record -> Agent 正式入口 (Trigger/Memory/Planner/Executor/DB 均在 Agent 内部)
    from src.agent.input_adapter import person_events_to_frame_record
    frame_record = person_events_to_frame_record(person_events)
    return agent_runner.process_frame(frame_record)


def run(config_path: str | Path, input_path: str | Path | None = None,
        use_camera: bool = False, agent: bool = False):
    cfg = load_config(config_path)

    # 模型/推理器只构建一次 (批量时复用)
    det = Detector(config_path)
    pe = PoseEstimator(config_path)
    clf = StateClassifier()
    rk = Riskevaluator(config_path)
    device = resolve_device(cfg)

    # --agent: 生产输入闭环 (复用现有 Agent 全部能力; 仅在显式开启时激活)
    agent_runner = None
    vision_tool = None
    if agent:
        import sys as _sys
        _repo_root = str(Path(__file__).resolve().parent.parent)
        if _repo_root not in _sys.path:
            _sys.path.insert(0, _repo_root)
        from src.agent.agent import Agent
        from src.agent.config import load_config as load_agent_config
        from src.tools.vision_tool import VisionTool
        bcfg = load_agent_config()
        agent_runner = Agent(config=bcfg, db_path=bcfg["db"]["path"])
        vision_tool = VisionTool(
            snapshot_path=bcfg["vision"]["snapshot_path"],
            recent_path=bcfg["vision"]["recent_path"],
        )

    # Demo V0.1: 实时摄像头模式 (Raspberry Pi 5 + Picamera2), 优先于文件输入
    if use_camera or str(cfg.get("input", {}).get("type", "")).lower() == "camera":
        return run_camera(cfg, det, pe, clf, rk, device,
                          agent_runner=agent_runner, vision_tool=vision_tool)

    src = Path(input_path) if input_path else Path(cfg["input"]["path"])

    # V0.2-M0: 视频文件 -> 视频回放链路 (逐帧复用 process_one)
    video_exts = tuple(str(e).lower()
                       for e in cfg.get("video", {}).get("extensions", FALLBACK_VIDEO_EXTS))
    if src.is_file() and src.suffix.lower() in video_exts:
        return run_video(cfg, src, det, pe, clf, rk, device,
                         agent_runner=agent_runner, vision_tool=vision_tool)

    image_paths = collect_inputs(src)
    if not image_paths:
        raise SystemExit(f"未找到可处理的输入: {src} "
                         f"(图片需 {IMAGE_EXTS}, 视频需 {video_exts}, 或含图片的目录)")
    is_single = len(image_paths) == 1

    out_dir = Path(cfg["output"]["dir"])
    json_dir = out_dir / cfg["output"]["json_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for img_path in image_paths:
        img = cv2.imread(str(img_path))
        if img is None:
            print(f"[skip] 无法读取: {img_path}")
            continue
        print(f"\n[input] {img_path}")
        annot, payload, _frame_records = process_one(cfg, img, det, pe, clf, rk, img_path, device)
        if agent_runner is not None:
            _person_events = [
                {"person_id": p.get("id"), "state": p.get("state"),
                 "risk_score": p.get("risk_score"), "priority": p.get("priority"),
                 # 传递现场图片路径, 供 Planner -> communication_tool -> upload_snapshot(OSS) 报警带图
                 "image_path": str(img_path)}
                for p in (payload.get("persons", []) or []) if p.get("id") is not None
            ]
            _r = _feed_agent(agent_runner, vision_tool, _person_events)
            # 兼容两种 Executor 返回: 旧格式含 results:list; 新格式为 {ok,action,task_id,..}。
            _n = len(_person_events)
            _result_count = 0
            _task = None
            if _r and isinstance(_r[0], dict):
                _res = _r[0].get("results")
                if isinstance(_res, list):
                    _result_count = len(_res)
                    _task = (_res[0].get("task_id")
                             if _res and isinstance(_res[0], dict) else None)
                else:
                    _result_count = 1
                    _task = _r[0].get("task_id")
            print(f"[agent] 已接入 {_n} 人员 -> {_result_count} 结果, task={_task}")
        annotated_name = cfg["output"]["annotated_image"] if is_single else f"{img_path.stem}.jpg"
        annotated_path = out_dir / annotated_name
        cv2.imwrite(str(annotated_path), annot)
        json_path = writer.save_json(payload, json_dir)
        print(f"[saved] {annotated_path}")
        print(f"[json ] {json_path}")
        saved.append(annotated_path)
    return saved


def run_video(cfg, video_path: Path, det, pe, clf, rk, device,
              agent_runner=None, vision_tool=None):
    """
    V0.2-M0 视频回放: 逐帧复用 process_one (算法零改动), 产出:
      1) 标注视频 <output.dir>/<video.out_video_dir>/<stem>_annotated.mp4
      2) JSONL 特征流 <output.dir>/<video.stream_dir>/<stem>.jsonl
    输出 fps = 源 fps / frame_stride, 保证回放时长与源视频一致。
    """
    vcfg = cfg.get("video", {})
    stride = max(1, int(vcfg.get("frame_stride", 1)))
    codec = str(vcfg.get("out_codec", "mp4v"))
    out_dir = Path(cfg["output"]["dir"])
    video_dir = out_dir / str(vcfg.get("out_video_dir", "videos"))
    stream_dir = out_dir / str(vcfg.get("stream_dir", "stream"))

    print(f"[video] 输入: {video_path} (frame_stride={stride})")
    with VideoFrameReader(video_path) as reader:
        fps, w, h = reader.fps, reader.width, reader.height
        total = reader.total_frames
        out_video = video_dir / f"{video_path.stem}_annotated.mp4"
        out_stream = stream_dir / f"{video_path.stem}.jsonl"
        writer.write_stream_header(
            out_stream, video_path=video_path, fps=fps, width=w, height=h,
            total_frames=total, frame_stride=stride, device=device)

        n_frames = 0
        n_person_frames = 0
        with VideoFrameWriter(out_video, fps=fps / stride, size=(w, h), codec=codec) as vw:
            for frame_idx, t_sec, frame in reader.iter_frames(stride):
                # 逐帧复用图片模式同一感知链路; verbose=False 避免逐帧刷屏
                annot, _payload, records = process_one(
                    cfg, frame, det, pe, clf, rk, video_path, device, verbose=False)
                if agent_runner is not None:
                    _person_events = [
                        {"person_id": p.get("id"), "state": p.get("state"),
                         "risk_score": p.get("risk_score"), "priority": p.get("priority")}
                        for p in (_payload.get("persons", []) or []) if p.get("id") is not None
                    ]
                    _feed_agent(agent_runner, vision_tool, _person_events)
                vw.write(annot)
                writer.append_frame_record(out_stream, frame_idx, t_sec, records)
                n_frames += 1
                if records:
                    n_person_frames += 1
                if n_frames % 30 == 0:
                    print(f"[video] 已处理 {n_frames} 帧 (源帧 idx={frame_idx}/{total})")

    print(f"[video] 完成: 处理 {n_frames} 帧, 含人员帧 {n_person_frames}")
    print(f"[saved] {out_video}")
    print(f"[stream] {out_stream}")
    return out_video, out_stream


def run_camera(cfg, det, pe, clf, rk, device, reader=None,
               agent_runner=None, vision_tool=None):
    """
    Demo V0.1 实时摄像头模式 (Raspberry Pi 5 + Picamera2):
    逐帧复用 process_one (算法零改动), 与 run_video 同构, 区别仅在帧源:
      - reader=None: 构造 CameraFrameReader (Picamera2, 仅 Pi 可用);
      - reader 可注入: 任何满足 VideoFrameReader 鸭子接口的对象
        (fps/width/height + iter_frames), 供 Windows 侧测试用视频文件冒充。
    产物:
      1) 实时标注窗口 (camera.show_window, q 键退出; headless 置 false)
      2) JSONL 特征流 outputs/stream/camera_<时间戳>.jsonl (camera.write_stream)
      3) 可选标注视频 outputs/videos/camera_<时间戳>_annotated.mp4 (camera.record_video)
    退出: q 键 / Ctrl+C 均优雅释放资源; 结束打印实测 FPS。
    """
    ccfg = cfg.get("camera", {}) or {}
    stride = max(1, int(ccfg.get("frame_stride", 1)))
    show_window = bool(ccfg.get("show_window", True))
    quit_key = ord(str(ccfg.get("quit_key", "q")).lower().strip()[:1])
    record_video = bool(ccfg.get("record_video", False))
    write_stream = bool(ccfg.get("write_stream", True))
    codec = str(cfg.get("video", {}).get("out_codec", "mp4v"))

    out_dir = Path(cfg["output"]["dir"])
    video_dir = out_dir / str(cfg.get("video", {}).get("out_video_dir", "videos"))
    stream_dir = out_dir / str(cfg.get("video", {}).get("stream_dir", "stream"))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_stream = stream_dir / f"camera_{stamp}.jsonl" if write_stream else None
    out_video = video_dir / f"camera_{stamp}_annotated.mp4" if record_video else None

    own_reader = reader is None
    if own_reader:
        try:
            reader = CameraFrameReader(cfg)
        except RuntimeError as e:
            # picamera2 缺失 / 摄像头硬件不可用: 友好退出, 不打 traceback
            raise SystemExit(f"[camera] 启动失败: {e}") from e
    fps, w, h = reader.fps, reader.width, reader.height

    print(f"[camera] 实时摄像头模式启动 ({w}x{h} @ {fps:.0f}fps 上限, stride={stride}, "
          f"device={device})")
    print(f"[camera] 窗口={show_window}, 录制={record_video}, 特征流={write_stream}")
    if show_window:
        print("[camera] 按 q 键退出; 无窗口环境用 Ctrl+C 退出")
    if out_stream is not None:
        # 实时流总帧数未知, total_frames=0; video_path 标记帧源
        writer.write_stream_header(
            out_stream, video_path="picamera2", fps=fps, width=w, height=h,
            total_frames=0, frame_stride=stride, device=device)

    n_frames = 0
    n_person_frames = 0
    vw = None
    t_start = time.monotonic()
    interrupted = False
    try:
        if out_video is not None:
            vw = VideoFrameWriter(out_video, fps=max(1.0, fps / stride),
                                  size=(w, h), codec=codec)
        for frame_idx, t_sec, frame in reader.iter_frames(stride):
            # 逐帧复用图片/视频模式同一感知链路; img_path 仅为 payload 文本标记
            annot, _payload, records = process_one(
                cfg, frame, det, pe, clf, rk, "picamera2", device, verbose=False)
            if agent_runner is not None:
                _person_events = [
                    {"person_id": p.get("id"), "state": p.get("state"),
                     "risk_score": p.get("risk_score"), "priority": p.get("priority")}
                    for p in (_payload.get("persons", []) or []) if p.get("id") is not None
                ]
                _feed_agent(agent_runner, vision_tool, _person_events)
            if vw is not None:
                vw.write(annot)
            if out_stream is not None:
                writer.append_frame_record(out_stream, frame_idx, t_sec, records)
            n_frames += 1
            if records:
                n_person_frames += 1
            if show_window:
                cv2.imshow("Disaster Rescue DRRM", annot)
                if (cv2.waitKey(1) & 0xFF) == quit_key:
                    print("\n[camera] 收到退出键 q, 停止...")
                    break
            if n_frames % 30 == 0:
                el = time.monotonic() - t_start
                print(f"[camera] 已处理 {n_frames} 帧, 实测 {n_frames / el:.1f} FPS")
    except KeyboardInterrupt:
        interrupted = True
        print("\n[camera] 收到 Ctrl+C, 停止...")
    finally:
        if vw is not None:
            vw.release()
        if own_reader:
            reader.release()
        if show_window:
            cv2.destroyAllWindows()

    elapsed = time.monotonic() - t_start
    meas_fps = n_frames / elapsed if elapsed > 0 else 0.0
    print(f"[camera] 结束: 处理 {n_frames} 帧, 含人员帧 {n_person_frames}, "
          f"实测 {meas_fps:.2f} FPS ({elapsed:.1f}s)"
          + (" [Ctrl+C]" if interrupted else ""))
    if out_stream is not None:
        print(f"[stream] {out_stream}")
    if out_video is not None:
        print(f"[saved] {out_video}")
    return out_stream, out_video


def _console_summary(pid_of, civilians, rescuers, persons_all, task, ranked):
    print("=== Disaster Rescue DRRM ===")
    n_non = sum(1 for d in persons_all if not d.is_person)
    print(f"检测目标: civilian {len(civilians)} 人, rescuer {len(rescuers)} 人 (不参与排序), "
          f"非人员 {n_non} 个")
    print("civilian 状态统计:")
    cnt = Counter(t["state"].state for t in task.values() if t["state"])
    if not cnt:
        print("  (无有效状态)")
    for k, v in cnt.most_common():
        print(f"  {k}: {v}")
    print("救援排序 (仅 civilian):")
    for rr in ranked:
        print(f"  id={pid_of[id(rr.detection)]}  rank={rr.rank}  state={rr.state}  "
              f"risk={rr.risk_score:.3f}  priority={rr.priority}")
    if not ranked:
        print("  (无有效 civilian 参与排序)")


def main():
    parser = argparse.ArgumentParser(
        description="Disaster_Rescue_DRRM Demo V0.1 (图片/目录/视频回放/实时摄像头)")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--input", default=None,
                        help="输入: 图片路径 / 图片目录(批量) / 视频文件(mp4/avi/mov/mkv); 默认取 config input.path")
    parser.add_argument("--camera", action="store_true",
                        help="实时摄像头模式 (Raspberry Pi 5 + Picamera2; 开发机不可用)")
    parser.add_argument("--agent", action="store_true",
                        help="接入生产输入闭环: 感知 person_events → Agent(Trigger/Memory/Planner/Executor/Tools/Database)")
    args = parser.parse_args()
    run(Path(args.config),
        Path(args.input) if args.input else None,
        use_camera=args.camera,
        agent=args.agent)


if __name__ == "__main__":
    main()
