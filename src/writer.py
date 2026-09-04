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