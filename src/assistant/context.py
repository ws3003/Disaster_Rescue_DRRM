"""Assistant 上下文聚合器 (只读, 信息类)。

聚合已有信息供回答:
  - scene:    当前场景概要
  - latest_task: 最近一条 SQLite 任务记录
  - snapshot:  最近一张现场快照文件名
  - weather / location / map: 未来扩展占位(当前为空 dict)

约束: 只读取(DB / 磁盘), 不调用任何工具(rescue/communication/vision); 不产生任何动作。
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

_DEFAULT_DB = "data/drrm.db"
_DEFAULT_SNAPSHOT_DIR = "data/snapshots"
_SNAPSHOT_GLOB = "*_analysis.*"


class AssistantContext:
    """只读上下文聚合。构造时指定数据源路径以方便测试/隔离; 默认指向项目运行时数据。

    weather_provider: 可选天气提供回调, 形如 (lat, lng) -> {ok, weather: {...}}。
                       为 None 时天气字段返回 None(未接入/不可用)。
    location: 查询天气使用的坐标(可选 dict: {lat, lng})。
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        snapshot_dir: Optional[str] = None,
        weather_provider: Optional[callable] = None,
        location: Optional[Dict[str, Any]] = None,
    ):
        self.db_path = db_path or _DEFAULT_DB
        self.snapshot_dir = snapshot_dir or _DEFAULT_SNAPSHOT_DIR
        self._weather_provider = weather_provider
        self._location = location or {}

    # ---------------------------------------------------------------- 数据读取
    def _latest_task(self) -> Dict[str, Any]:
        try:
            if not os.path.exists(self.db_path):
                return {}
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT task_id, status, trigger_event, plan_trace, created, finished "
                    "FROM tasks ORDER BY created DESC LIMIT 1"
                ).fetchone()
            if row is None:
                return {}
            out = dict(row)
            # plan_trace 是 JSON 文本, 提取 event_summary 摘要(尽力而为)
            try:
                plan_trace = json.loads(out.get("plan_trace") or "[]")
                tmp = plan_trace[0] if isinstance(plan_trace, list) and plan_trace else {}
                out["event_summary"] = (tmp.get("event_summary")
                                        if isinstance(tmp, dict) else None)
            except (TypeError, ValueError):
                out["event_summary"] = None
            return out
        except Exception:
            return {}

    def _scene(self, latest_task: Dict[str, Any]) -> Dict[str, Any]:
        # 最小化: 由最近任务推出概要; 无数据时为空
        if not latest_task:
            return {}
        return {
            "summary": latest_task.get("event_summary") or latest_task.get("trigger_event"),
            "status": latest_task.get("status"),
        }

    def _latest_snapshot(self) -> Dict[str, Any]:
        try:
            d = Path(self.snapshot_dir)
            if not d.is_dir():
                return {}
            files = sorted(d.glob(_SNAPSHOT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                return {}
            return {"file": files[0].name}
        except Exception:
            return {}

    def _weather(self) -> Optional[Dict[str, Any]]:
        """有 weather_provider 时查询天气(信息类, mock/future 真实); 无则 None。"""
        if self._weather_provider is None:
            return None
        try:
            res = self._weather_provider(
                lat=self._location.get("lat"),
                lng=self._location.get("lng"),
            )
            if isinstance(res, dict) and res.get("ok") and res.get("weather"):
                return res["weather"]
            return None
        except Exception:
            return None

    # ---------------------------------------------------------------- 聚合并对外
    def collect(self) -> Dict[str, Any]:
        """返回结构化上下文: 包含现用字段与未来扩展占位(weather 未接入时为 None)。"""
        latest_task = self._latest_task()
        scene = self._scene(latest_task)
        snapshot = self._latest_snapshot()
        weather = self._weather()
        return {
            # 当前可用
            "scene": scene,
            "latest_task": latest_task,
            "snapshot": snapshot,
            "weather": weather,
            # 未来扩展占位(当前为空)
            "location": {},
            "map": {},
        }