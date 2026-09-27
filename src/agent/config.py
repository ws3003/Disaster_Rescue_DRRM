"""
src/agent/config.py — 统一 B 配置加载 (P0-20)
=============================================
读取 config/config.yaml 的 B 相关段，与旧默认值合并，处理缺失/null/类型错误，
返回可直接注入 Agent / Tools / Database 的配置 dict。

容错规则 (P0-20):
  文件不存在 / section 缺失 / 字段缺失 / 字段为 null  -> 使用旧默认, 不报错
  字段类型错误 -> warnings.warn + 回退旧默认 (不使 B 无法启动, 也不静默吞掉)

无第三方框架: 复用项目已有 PyYAML。
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

# B 侧默认值 (与现有代码默认保持一致; 字段名对应各模块构造参数)
_DEFAULTS: Dict[str, Any] = {
    "agent": {
        "trigger": {
            "confirm_frames": 3,
            "high_risk_must_alert": True,
            "heartbeat_minutes": 5,
        },
        "memory": {
            "cooldown_minutes": 30,
        },
        "planner": {
            "max_steps": 3,
            "cooldown_minutes": 30,
        },
    },
    "db": {"path": "data/drrm.db"},
    "vision": {
        "snapshot_path": "data/latest_vision.json",
        "recent_path": "data/vision_snapshots.jsonl",
        "scene_window_sec": 10,
    },
    "communication": {
        "channel": "mock",
        "max_retries": 2,
        "backoff_seconds": [1, 4],
        "timeout": 10,
        "log_path": "data/communication_log.jsonl",
        "pending_path": "data/pending_communication.jsonl",
        # 刻意无 webhook 字段: secret(WECOM_WEBHOOK_URL) 仅来自环境变量
    },
    "rescue": {
        "snapshot_path": "data/latest_vision.json",
    },
}

_MISSING = object()


def _coerce(value: Any, default: Any, key: str, section: str) -> Any:
    """单字段类型化: null/类型不符 -> warn + 默认; 合法 -> 转换。"""
    if value is None:
        return default
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
    elif isinstance(default, int):
        if isinstance(value, bool):
            pass
        elif isinstance(value, int):
            return value
        elif isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                pass
    elif isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    elif isinstance(default, list):
        if isinstance(value, list):
            return value
    elif isinstance(default, str):
        if isinstance(value, str):
            return value
    warnings.warn(
        f"[config] B 配置项 {section}.{key} 类型不符({type(value).__name__}), 回退默认 {default!r}"
    )
    return default


def _merge_section(raw: Any, default_section: Dict[str, Any], which: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        if raw is not None:
            warnings.warn(f"[config] B 配置段 '{which}' 类型不符, 使用默认")
        return dict(default_section)
    out: Dict[str, Any] = {}
    for key, dflt in default_section.items():
        if isinstance(dflt, dict):
            # 嵌套 dict 段递归合并 (如 agent.trigger)
            out[key] = _merge_section(raw.get(key), dflt, f"{which}.{key}")
        elif key in raw:
            out[key] = _coerce(raw[key], dflt, key, which)
        else:
            out[key] = dflt
    return out


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """读取 B 配置段并合并默认值; 永不因缺失/类型错误抛异常。"""
    path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
    raw: Any = {}
    if path.exists():
        try:
            import yaml
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            warnings.warn(f"[config] 读取 config.yaml 失败({exc}), 使用默认值")
            raw = {}
    if not isinstance(raw, dict):
        warnings.warn("[config] config.yaml 顶层不是对象, 使用默认值")
        raw = {}

    cfg: Dict[str, Any] = {}
    for section, defsec in _DEFAULTS.items():
        cfg[section] = _merge_section(raw.get(section), defsec, section)
    return cfg