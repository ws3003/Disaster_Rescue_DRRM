"""src/agent/context_provider.py — V4.4 Agent 场景上下文桥接层。

作用: 把 SceneContextTool 提供的统一现场上下文, 组装为 Agent / LLM 需要的
统一输入格式。只做数据桥接:
  - 不复制数据库逻辑 / 不直接读 sqlite / 不调用 LLM。

结构:
  get_scene_context() -> {
      "scene_context": {
          "persons": [],
          "risk_summary": {},
          "events": [],
          "tasks": []
      }
  }
"""

from __future__ import annotations

from typing import Any, Dict


class ContextProvider:
    """Agent 侧获取现场上下文的桥接层(只读)。"""

    @staticmethod
    def get_scene_context() -> Dict[str, Any]:
        from src.tools.scene_context_tool import SceneContextTool  # 复用既有只读层
        data = SceneContextTool().get_current_context()
        return {
            "scene_context": {
                "persons": data.get("persons") or [],
                "risk_summary": data.get("risk_summary") or {},
                "events": data.get("events") or [],
                "tasks": data.get("tasks") or [],
            }
        }

    @staticmethod
    def get_location() -> Dict[str, Any]:
        """返回设备当前位置(抽象层); LocationTool 失败 → {} 不阻断 Agent。"""
        try:
            from src.tools.location_tool import LocationTool
            return LocationTool().get_current_location() or {}
        except Exception:  # noqa: BLE001
            return {}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(ContextProvider().get_scene_context(),
                      ensure_ascii=False, default=str, indent=2))