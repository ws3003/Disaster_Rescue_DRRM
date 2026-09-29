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
_VISION_PATH = "data/vision_persons.json"
_SNAPSHOT_GLOB = "*_analysis.*"


class AssistantContext:
    """只读上下文聚合。构造时指定数据源路径以方便测试/隔离; 默认指向项目运行时数据。

    weather_provider: 可选天气提供回调, 形如 (lat, lng) -> {ok, weather: {...}}。
                       为 None 时天气字段返回 None(未接入/不可用)。
    location: 查询天气使用的坐标(可选 dict: {lat, lng})。
    map_provider: 可选地图提供器(如 MapTool), 需提供 geocode(address)/rescue_route(o,d)。
    place: 现场位置文本(用于 geocode)。
    route_origin / route_destination: 路线查询起终点(可选)。
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        snapshot_dir: Optional[str] = None,
        weather_provider: Optional[callable] = None,
        location: Optional[Dict[str, Any]] = None,
        map_provider: Optional[Any] = None,
        place: Optional[str] = None,
        route_origin: Optional[str] = None,
        route_destination: Optional[str] = None,
        vision_path: Optional[str] = None,
    ):
        self.db_path = db_path or _DEFAULT_DB
        self.snapshot_dir = snapshot_dir or _DEFAULT_SNAPSHOT_DIR
        self._weather_provider = weather_provider
        self._location = location or {}
        self._map_provider = map_provider
        self._place = place
        self._route_origin = route_origin or "现场"
        self._route_destination = route_destination or "最近救援点"
        self._vision_path = vision_path or _VISION_PATH

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

    def _map_location(self) -> Dict[str, Any]:
        """有 map_provider 且给定 place 时定位; 否则空。"""
        if self._map_provider is None or not self._place:
            return {}
        try:
            res = self._map_provider.geocode(self._place)
            if isinstance(res, dict) and res.get("ok") and res.get("location"):
                return res["location"]
            return {}
        except Exception:
            return {}

    def _map_route(self) -> Dict[str, Any]:
        """有 map_provider 时给路线建议; 否则空。仅信息, 不派单。"""
        if self._map_provider is None:
            return {}
        try:
            res = self._map_provider.rescue_route(
                origin=self._route_origin,
                destination=self._route_destination,
            )
            if isinstance(res, dict) and res.get("ok") and res.get("route"):
                return res["route"]
            return {}
        except Exception:
            return {}

    def _vision(self) -> Dict[str, Any]:
        """读取最新视况人员快照; 无/损坏返回 {}。"""
        try:
            from ..vision_snapshot import read_latest
            data = read_latest(self._vision_path)
            if isinstance(data, dict) and data.get("persons"):
                return data
            return {}
        except Exception:
            return {}

    # ---------------------------------------------------------------- 聚合并对外
    def collect(self) -> Dict[str, Any]:
        """返回结构化上下文。weather/location/map/vision 未接入(或无提供器)时为空/None。"""
        latest_task = self._latest_task()
        scene = self._scene(latest_task)
        snapshot = self._latest_snapshot()
        weather = self._weather()
        map_location = self._map_location()
        map_route = self._map_route()
        vision = self._vision()
        return {
            # 当前可用
            "scene": scene,
            "latest_task": latest_task,
            "snapshot": snapshot,
            "weather": weather,
            "location": map_location,
            "map": {"route": map_route} if map_route else {},
            "vision": vision,
        }