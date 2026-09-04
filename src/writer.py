"""
Disaster_Rescue_DRRM V0.1.5 — JSON 结果写出模块
==============================================
职责: 把每张图片的 DRRM 分析结果序列化为 JSON 并保存到输出目录。
不依赖具体算法实现, 只负责结构化结果落盘。

JSON 结构 (每张图一个文件):
  {
    "image_path": str,      # 输入图片路径
    "device": str,          # 推理设备 (cuda/cpu)
    "timestamp": str,       # ISO 生成时刻
    "persons": [            # 参与排序的人员 (按优先级)
      { id, class_name, state, confidence, risk_score, priority, rank, f1, f6 },
      ...
    ]
  }
输出: <output.json_dir>/<图片主名>.json  (默认 outputs/json/*.json)

V0.2-M0 新增: 视频逐帧感知特征流 (JSONL, PRD 4.2 三层解耦接口)
  - write_stream_header: 写流头 (视频元数据)
  - append_frame_record: 追加单帧感知记录
  输出: outputs/stream/<视频主名>.jsonl
  JSONL 每行一个 JSON 对象: 首行 type=header, 其后每行 type=frame。
  供 M2 跟踪器 / M3 TSF / 动态 DRRM 离线重放, 不必反复跑视频。
"""

import json
from datetime import datetime
from pathlib import Path


def make_payload(image_path: str | Path, device: str, persons: list, timestamp: str | None = None) -> dict:
    """构造单张图片的 JSON payload。"""
    return {
        "image_path": str(image_path),
        "device": str(device),
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
        "persons": persons,
    }


def save_json(payload: dict, out_dir: str | Path) -> Path:
    """
    以 UTF-8 无 BOM 写入 JSON 到 out_dir/{image_stem}.json。

    参数:
        out_dir: JSON 输出目录 (如 outputs/json), 不存在则自动创建
    返回:
        写入的文件路径
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(payload["image_path"]).stem
    path = out / f"{stem}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------------------
# V0.2-M0 视频逐帧感知特征流 (JSONL)
# ---------------------------------------------------------------------------
STREAM_VERSION = "0.2-m0"


def write_stream_header(jsonl_path: str | Path, *, video_path, fps: float, width: int,
                        height: int, total_frames: int, frame_stride: int,
                        device: str, version: str = STREAM_VERSION) -> Path:
    """
    新建 JSONL 特征流并写入首行 header (UTF-8 无 BOM)。
    header 存视频元数据; landmarks 归一化坐标需配合 width/height 还原像素。
    """
    path = Path(jsonl_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "header",
        "version": version,
        "video_path": str(video_path),
        "fps": round(float(fps), 3),
        "width": int(width),
        "height": int(height),
        "total_frames": int(total_frames),
        "frame_stride": int(frame_stride),
        "device": str(device),
    }
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
    return path


def append_frame_record(jsonl_path: str | Path, frame_idx: int, t_sec: float,
                        persons: list) -> None:
    """
    向 JSONL 追加一行帧记录:
      {"type":"frame","frame_idx":N,"t_sec":x.xxx,"persons":[...]}
    persons 为调用方 (main.py) 按 M0 schema 组装好的 dict 列表;
    无人帧传空列表, 保证帧索引连续。
    """
    record = {
        "type": "frame",
        "frame_idx": int(frame_idx),
        "t_sec": round(float(t_sec), 3),
        "persons": persons,
    }
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")