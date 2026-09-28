"""
src/edge_client.py — Raspberry Pi Edge Client (Stage 7-B)
==========================================================
独立负责: 摄像头采集关键帧 → JPEG 编码 → HTTP multipart 上传到
Windows FastAPI Gateway → 接收 VLM + LLM 分析结果并打印。

设计要点:
  - 与 src/main.py 及感知算法链**解耦**, 不 import 任何感知模块;
  - 不打开 cv2 窗口 (支持 headless);
  - 模块级纯函数/类便于用 FakeCamera + FakeTransport 离线测试。
    (运行不需要真实摄像头与服务器; 测试中注入替身)

硬件依赖: Python 3.11, picamera2(系统包) / opencv-python / requests。
配置: config/edge.yaml; 命令行 --server 可覆盖 API 地址。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple

import cv2
import requests
import yaml

# 保证可从 `cd src && python edge_client.py` 运行 (顶层 import camera/edge_perception)
# 同时把 repo 根加入 sys.path, 使 V4.0-B Agent 用 b 分支真实约定 `from src.agent.agent`
# 导入 (src/ 无 __init__.py, 为 namespace package, 不可用 `from agent import`)。
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_SRC_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

# 默认配置文件 (repo 根/config/edge.yaml)
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "edge.yaml"
)


class EdgeEncodeError(Exception):
    """JPEG 编码失败。"""


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """读取 edge.yaml 为 dict; 缺失返回空 dict (由 main/调用方兜底)。"""
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg


# ----------------------------------------------------------------------
# 摄像头 (Picamera2)
# ----------------------------------------------------------------------
class EdgeCam:
    """兼容包装: 复用 V0.1 已验证摄像头链路 src/camera.py 的 CameraFrameReader。

    内部不再创建 Picamera2; 采集格式/背压/旋转等均由 CameraFrameReader 统一负责
    (create_video_configuration, 与 V0.2 实时视频链路一致)。
    capture() 返回单帧 BGR (供 cv2/JPEG 编码), 保持 edge_client 原有接口。
    """

    def __init__(
        self,
        width: int = 1280,
        height: int = 720,
        fps: float = 15.0,
        reader: Any = None,
    ):
        """入参 width/height/fps 供真实 reader 初始化; reader 可注入(Fake 测试)。"""
        if reader is None:
            # 懒加载: 仅真实实例化时引入 src/camera.py (脚本/测试可摆脱 Pi 依赖)
            from camera import CameraFrameReader
            reader = CameraFrameReader(
                {"camera": {"width": int(width), "height": int(height), "fps": float(fps)}}
            )
        self._reader = reader
        self._frames = iter(reader.iter_frames())

        self.width = int(getattr(reader, "width", width))
        self.height = int(getattr(reader, "height", height))

    def capture(self):
        """阻塞取最新一帧, 返回 BGR uint8 ndarray。"""
        _, _, frame_bgr = next(self._frames)
        return frame_bgr

    def close(self) -> None:
        try:
            self._reader.stop()
        except Exception:
            pass


# ----------------------------------------------------------------------
# 图片编码
# ----------------------------------------------------------------------
def encode_jpeg(frame, quality: int = 90) -> bytes:
    """BGR 帧 → JPEG bytes; 非法帧或编码失败抛 EdgeEncodeError。"""
    # 先校验帧类型, 避免 cv2.imencode 对非数组抛非预期异常
    if not hasattr(frame, "shape") or getattr(frame, "ndim", 0) not in (2, 3):
        raise EdgeEncodeError("JPEG encode failed: 非法帧类型")
    ok, buf = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not ok or buf is None:
        raise EdgeEncodeError("JPEG encode failed")
    return buf.tobytes()


# ----------------------------------------------------------------------
# HTTP 上传
# ----------------------------------------------------------------------
class EdgeUploader:
    """单次把 JPEG + person_events 以 multipart 上传到 Gateway。

    transport 缺省 requests.post; 测试注入 FakeTransport。
    """

    def __init__(
        self,
        api_url: str,
        transport: Optional[Callable[..., Any]] = None,
        timeout: float = 30.0,
    ):
        self.api_url = api_url
        self._post = transport if transport is not None else requests.post
        self.timeout = timeout

    def upload(
        self, image_bytes: bytes, person_events: Optional[list] = None
    ) -> Tuple[int, Any]:
        """上传一帧, 返回 (HTTP status, 响应 JSON)。"""
        files = {"image": ("frame.jpg", image_bytes, "image/jpeg")}
        data = {"person_events": json.dumps(person_events or [], ensure_ascii=False)}
        resp = self._post(self.api_url, files=files, data=data, timeout=self.timeout)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return int(resp.status_code), body


# ----------------------------------------------------------------------
# 结果解析/打印
# ----------------------------------------------------------------------
def extract_summary(body: Dict[str, Any]) -> Dict[str, Any]:
    """从 Gateway 成功响应提取摘要 (Scene / Severity / Tasks)。"""
    vlm = body.get("vlm_output") or {}
    llm = body.get("llm_output") or {}
    tasks = [p.get("action") for p in (llm.get("task_proposals") or [])]
    return {
        "scene": vlm.get("scene_type", "unknown"),
        "severity": vlm.get("overall_severity", "unknown"),
        "tasks": tasks or [],
    }


def _print_summary(body: Dict[str, Any]) -> None:
    s = extract_summary(body)
    print("================================")
    print("Edge AI Response")
    print("Scene:")
    print(s["scene"])
    print("Severity:")
    print(s["severity"])
    print("Tasks:")
    print(", ".join(s["tasks"]) if s["tasks"] else "none")
    print("================================")


def _print_log(ts: str, w: int, h: int, status: int) -> None:
    print("[EdgeClient]")
    print(ts)
    print(f"uploaded: {w}x{h}")
    print(f"response: {status}")


# ----------------------------------------------------------------------
# 预览模式 (开发/调试用; opt-in, 默认关闭)
# ----------------------------------------------------------------------
def render_overlay(frame, person_events):
    """在帧上叠加 person_events 文字信息 (仅文本, 不画 bbox)。

    供 --preview 调试; 不改变 perception 输出 schema。
    """
    out = frame.copy()
    y = 24
    for i, ev in enumerate(person_events, start=1):
        _ = i  # person_id 优先取自事件
        lines = [
            f"Person {ev.get('person_id', '-')}",
            f"State: {ev.get('state', '-')}",
            f"Risk: {ev.get('risk_score', 0):.2f}",
            f"Priority: {ev.get('priority', '-')}",
        ]
        for line in lines:
            cv2.putText(out, line, (12, y), cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2, cv2.LINE_AA)
            y += 24
        y += 10
    return out


def _show_preview(frame, person_events) -> bool:
    """显示预览窗口; 返回 False 表示用户按 q 退出。"""
    cv2.imshow("DRRM Edge Preview", render_overlay(frame, person_events))
    return (cv2.waitKey(1) & 0xFF) != ord("q")


def run_preview_only(
    cam,
    max_frames: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    show: Optional[Callable[[Any, list], bool]] = None,
) -> int:
    """纯预览调试 (--preview-only): 连续采集并显示实时画面。

    - 不执行 YOLO/MediaPipe, 不 JPEG 编码, 不 HTTP 上传, 不依赖 Windows Gateway;
    - 复用 EdgeCam(内部 CameraFrameReader, 含 ov5647 颜色/旋转修复);
    - 窗口标题 "DRRM Edge Preview", 按 q 正常退出;
    - headless(无显示) 抛错时打印并结束, 不卡死。

    show 可注入(Fake 测试); max_frames 限制显示帧数(测试); sleep 每帧极短空转
    以便 waitKey 及时处理事件。返回实际显示帧数。
    """
    show = show if show is not None else _show_preview
    frames = 0
    while True:
        if max_frames is not None and frames >= max_frames:
            break
        try:
            frame = cam.capture()
        except Exception as exc:
            print(f"[PreviewOnly] capture failed: {exc}")
            sleep(0.5)
            continue
        try:
            if not show(frame, []):  # 按 q 退出
                break
        except Exception as exc:
            print(f"[PreviewOnly] display failed (headless?): {exc}")
            break
        frames += 1
        sleep(0.001)  # 让 waitKey(1) 获得事件处理机会
    return frames


# ----------------------------------------------------------------------
# 主循环
# ----------------------------------------------------------------------
def run_loop(
    uploader: EdgeUploader,
    cam,
    interval: float = 10,
    quality: int = 90,
    max_uploads: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    perception: Optional[Any] = None,
    preview: bool = False,
    agent: Optional[Any] = None,
    observation: Optional[Dict[str, Any]] = None,
    flush_state: Optional[Dict[str, Any]] = None,
) -> int:
    """周期性 采集→(感知)→编码→上传→打印; preview 时叠加显示相机帧。

    任意单步失败(采集/编码/感知/网络/API错误)均打印并**继续**, 不退出。
    max_uploads 限制迭代次数(供测试/--once); sleep 可注入(测试用 no-op)。
    perception: 本地感知桥接(EdgePerception), 提供 analyze(frame)->person_events;
                为 None 时上传空 person_events。
    agent: V4.0-B Agent 决策辅助(旁路). 为 None 时完全不启用;
           异常只打印不阻断 upload. 必须由调用方在 main() 中初始化一次(跨帧复用,
           保证 memory cooldown 生效, 避免重复报警).
    observation: 观测配置 {enabled,status_every,flush_every}; 为 None 时不输出摘要。
    flush_state: pending 补偿状态 {caller, flush_every}; caller 为可调用对象,
                 每 flush_every 帧调一次(no-arg) 用于补发 communication pending。
    """
    obs_cfg = observation if isinstance(observation, dict) else {}
    obs_enabled = bool(obs_cfg.get("enabled", True))
    status_every = int(obs_cfg.get("status_every", 50))
    flush_every = int(
        (flush_state or {}).get("flush_every")
        if flush_state
        else obs_cfg.get("flush_every", 50)
    )
    flush_caller = (flush_state or {}).get("caller") if flush_state else None

    iterations = 0
    while True:
        if max_uploads is not None and iterations >= max_uploads:
            break
        iterations += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            frame = cam.capture()
            # 非图像帧(缺 shape)视作采集失败
            if not hasattr(frame, "shape"):
                raise TypeError("非法帧: 缺少 shape")
            w, h = frame.shape[1], frame.shape[0]
        except Exception:
            print("[EdgeClient]")
            print(ts)
            print("camera capture failed")
            sleep(interval)
            continue

        try:
            jpeg = encode_jpeg(frame, quality)
        except EdgeEncodeError as exc:
            print("[EdgeClient]")
            print(ts)
            print(f"{exc}")
            sleep(interval)
            continue

        # 本地感知桥接: 生成 person_events (失败不阻塞, 仅上传 image)
        try:
            person_events = perception.analyze(frame) if perception else []
        except Exception:
            print("[EdgeClient]")
            print(ts)
            print("perception analyze failed")
            person_events = []

        # 调试可观测性: 发送前打印本地感知结果 (不改变上传协议/数据结构)
        if perception is not None:
            print("[Perception]")
            print("person_events:")
            print(json.dumps(person_events, indent=2, ensure_ascii=False))

        # V4.0-B Agent 决策辅助: 旁路接入, 不修改 person_events, 不影响 upload
        # 失败只打印并继续, 绝不阻断上传链路
        if agent is not None and person_events:
            try:
                # ── Phase 2.5: HIGH 人员保存现场快照, 供 DingTalk 图片报警 ──
                # 在 frame_record 构建前, 把快照保存给 HIGH 人员, 并注入 snapshot_path。
                from src.snapshot import save_snapshot
                _events = list(person_events)  # 浅拷贝, 不修改原上传 payload
                for _ev in _events:
                    if not isinstance(_ev, dict):
                        continue
                    _prio = str(_ev.get("priority", "")).upper()
                    _risk = _ev.get("risk_score")
                    _high = (_prio == "HIGH") or (
                        isinstance(_risk, (int, float)) and _risk >= 0.60
                    )
                    if _high:
                        _snap = save_snapshot(frame, _ev.get("person_id"))
                        if _snap:
                            _ev["snapshot_path"] = _snap
                            print("[Snapshot] saved: " + str(_snap))

                from src.agent.input_adapter import person_events_to_frame_record
                frame_record = person_events_to_frame_record(
                    _events,
                    timestamp=ts,
                )
                agent.process_frame(frame_record)
            except Exception as exc:  # noqa: BLE001
                print("[EdgeClient]")
                print(ts)
                print(f"agent process failed (upload continues): {exc}")

        # ── 阶段1A: Agent 运行状态周期摘要(不阻断主循环) ──
        if obs_enabled and agent is not None and status_every > 0 \
                and iterations % status_every == 0:
            try:
                stats = getattr(agent, "stats", {}) or {}
                print("[AgentStatus]")
                print("frames:")
                print(iterations)
                print("triggers_received:")
                print(stats.get("triggers_received", 0))
                print("plans_created:")
                print(stats.get("plans_created", 0))
                print("tasks_completed:")
                print(stats.get("tasks_completed", 0))
                print("tasks_failed:")
                print(stats.get("tasks_failed", 0))
                print("tasks_suppressed:")
                print(stats.get("tasks_suppressed", 0))
            except Exception:  # noqa: BLE001 — 观测失败绝不阻断上传
                print("[EdgeClient] observation status output failed")

        # ── 阶段1B: communication pending 自动补偿(周期 flush, 不阻断) ──
        if flush_caller is not None and flush_every > 0 \
                and iterations % flush_every == 0:
            try:
                result = flush_caller()
                if isinstance(result, dict):
                    flushed = result.get("flushed")
                    remaining = result.get("remaining")
                    print("[EdgeClient] flush_pending result: "
                          f"flushed={flushed} remaining={remaining}")
            except Exception:  # noqa: BLE001
                print("[EdgeClient] flush_pending failed")

        # 预览模式: 叠加 person_events 文字显示相机帧; 按 q 退出
        # (headless/无显示环境抛错时自动关闭预览, 不影响后续上传)
        if preview:
            try:
                if not _show_preview(frame, person_events):
                    break
            except Exception:
                preview = False

        try:
            status, body = uploader.upload(jpeg, person_events=person_events)
        except requests.RequestException:
            print("[EdgeClient]")
            print(ts)
            print("API connection failed")
            sleep(interval)
            continue

        _print_log(ts, w, h, status)
        if status == 200:
            _print_summary(body if isinstance(body, dict) else {})
        else:
            err = body.get("error") if isinstance(body, dict) else ""
            print(f"server error: {status} {err}")

        sleep(interval)
    return iterations


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Disaster_Rescue_DRRM Edge Client")
    parser.add_argument(
        "--server", help="覆盖 Gateway API URL (默认读 config/edge.yaml)"
    )
    parser.add_argument("--interval", type=int, help="上传间隔秒数")
    parser.add_argument("--quality", type=int, help="JPEG 质量 0-100")
    parser.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    parser.add_argument(
        "--once", action="store_true", help="只上传一帧后退出 (调试/测试)"
    )
    parser.add_argument(
        "--preview", action="store_true", help="显示相机实时预览窗口 (仅叠加文字, 按 q 退出)"
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="纯预览调试: 连续显示摄像头实时画面, 不感知/不编码/不上传, 不需 Gateway (按 q 退出)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    server_cfg = cfg.get("server", {}) or {}
    cam_cfg = cfg.get("camera", {}) or {}
    up_cfg = cfg.get("upload", {}) or {}

    # 纯预览调试: 不需要 api_url / uploader / perception, 独立于上传链路提前返回
    if args.preview_only:
        cam = EdgeCam(
            width=cam_cfg.get("width", 1280), height=cam_cfg.get("height", 720)
        )
        try:
            frames = run_preview_only(cam)
        finally:
            cam.close()
        print(f"[PreviewOnly] preview ended, shown {frames} frames")
        return 0

    api_url = args.server or server_cfg.get("api_url")
    if not api_url:
        print("缺少 server.api_url 配置 (config/edge.yaml 或 --server)")
        return 1

    uploader = EdgeUploader(api_url)
    cam = EdgeCam(
        width=cam_cfg.get("width", 1280), height=cam_cfg.get("height", 720)
    )
    interval = args.interval if args.interval is not None else up_cfg.get("interval", 10)
    quality = args.quality if args.quality is not None else up_cfg.get("jpeg_quality", 90)

    # 本地感知桥接: 默认开启; 初始化失败回退为仅上传图片, 不阻塞
    perception = None
    perception_cfg = cfg.get("perception", {}) or {}
    if perception_cfg.get("enabled", True):
        try:
            import edge_perception
            perception = edge_perception.EdgePerception()
        except Exception as exc:  # noqa: BLE001
            print(f"[EdgeClient] perception init failed, upload image only: {exc}")

    # V4.0-B Agent 决策辅助: main() 中一次性初始化, 跨帧复用 (memory cooldown 生效)
    # agent.enabled=true 才初始化; 失败回退为仅上传(不阻塞 camera/perception/upload)。
    agent = None
    agent_ready = False
    agent_cfg = cfg.get("agent", {}) or {}
    if agent_cfg.get("enabled", False):
        try:
            from src.agent.agent import Agent
            agent = Agent(config=cfg)
            agent_ready = True
        except Exception:  # noqa: BLE001
            print("[EdgeClient][Agent] initialization failed, "
                  "fallback to upload only:")
            traceback.print_exc()
            agent = None
            agent_ready = False

    # ── 观测/补偿配置 (阶段1 A+B) ──
    obs_cfg = cfg.get("observation", {}) or {}
    obs_enabled = bool(obs_cfg.get("enabled", True))
    status_every = int(obs_cfg.get("status_every", 50))
    flush_every = int(obs_cfg.get("flush_every", 50))

    # ── 阶段1B: 独立 CommunicationTool 只用于 pending 补发 ──
    # 与 Agent 内部 communication 实例分隔; 共享同一 pending_path(读取同一队列)。
    # 通道由环境变量 COMMUNICATION_CHANNEL 决定(mock|wecom|dingtalk)。
    flush_caller = None
    if obs_enabled and flush_every > 0:
        try:
            from src.tools.communication_tool import CommunicationTool
            comm_cfg = cfg.get("communication", {}) or {}
            _flusher = CommunicationTool(
                log_path=comm_cfg.get("log_path", "data/communication_log.jsonl"),
                pending_path=comm_cfg.get("pending_path",
                                          "data/pending_communication.jsonl"),
                channel="auto",
            )
            flush_caller = _flusher.flush_pending
        except Exception:  # noqa: BLE001 — 补偿不可用不阻断主流程
            print("[EdgeClient] communication flusher init failed "
                  "(pending recovery disabled):")
            traceback.print_exc()
            flush_caller = None

    try:
        run_loop(
            uploader, cam, interval=interval, quality=quality,
            max_uploads=1 if args.once else None,
            perception=perception,
            preview=args.preview,
            agent=agent,
            observation={
                "enabled": obs_enabled,
                "status_every": status_every,
                "flush_every": flush_every,
            },
            flush_state={
                "flush_every": flush_every,
                "caller": flush_caller,
            },
        )
    finally:
        cam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())