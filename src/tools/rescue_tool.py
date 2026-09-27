"""
src/tools/rescue_tool.py — Rescue Tool (P0-18)
===============================================
Agent 的"风险参谋": 提供 DRRM 救援排序与单人多 (f1/f6) 分解。

复用原则 (PRD 4.5 Tool2 要求):
  - 底层复用现有 src/ranking.py 的 `rank()` 与 `_priority()` 分级语义, **零复制、零修改**;
  - 本工具只做"Agent 人员字典 → PersonEvidence"的最小适配(sim shim), 喂给现有排序算法;
  - 不重新发明风险 / 排序算法。

数据来源 (与 vision_tool 同源):
  - 优先取调用方传入 kwargs["persons"] (Agent 侧 person_events 列表);
  - 否则回退读 data/latest_vision.json 快照的 persons 段(由视觉管线/vision_tool 写入)。

Actions:
  - get_ranking        返回按 risk_score 降序、PRD 结构化的救援排序表
  - assess_person      对单人多返回风险(f1/f6)分解
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

# ---- 路径引导 (对齐 vision_tool 约定) ----------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SRC_DIR = Path(__file__).resolve().parents[1]  # src/
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from src.tools.base import Tool  # noqa: E402

_DEFAULT_SNAPSHOT = "data/latest_vision.json"


class RescueTool(Tool):
    """DRRM 风险与救援排序工具 (P0-18)。"""

    def __init__(self, snapshot_path: str = _DEFAULT_SNAPSHOT):
        self.snapshot_path = snapshot_path

    # ------------------------------------------------------------------
    # Tool 契约
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "rescue_tool"

    @property
    def description(self) -> str:
        return ("DRRM 风险与救援排序参谋: get_ranking(整体排序) / "
                "assess_person(单人 f1/f6 分解)")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get_ranking", "assess_person"]},
                "persons": {"type": "array", "description": "person_events 列表(可选)"},
                "person_id": {"type": ["string", "integer"], "description": "assess_person 目标"},
            },
            "additionalProperties": True,
        }

    def run(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        if not action:
            return {"ok": False, "error": "Missing required parameter: action."}
        try:
            if action == "get_ranking":
                return self._get_ranking(kwargs)
            if action == "assess_person":
                return self._assess_person(kwargs)
            return {"ok": False, "error": f"Unsupported rescue action: {action}", "action": action}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "action": action}

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------
    def _load_persons(self, kwargs: dict[str, Any]) -> List[dict]:
        """优先 kwargs['persons']，否则回退 latest_vision.json 快照 persons 段。"""
        persons = kwargs.get("persons")
        if persons is None:
            try:
                with open(self.snapshot_path, encoding="utf-8") as fh:
                    persons = json.load(fh).get("persons", [])
            except Exception:
                persons = []
        if not isinstance(persons, list):
            return []
        return [p for p in persons if isinstance(p, dict)]

    # ------------------------------------------------------------------
    # 复用 ranking.rank 做排序 (懒加载, 不拖慢启动)
    # ------------------------------------------------------------------
    def _rank_persons(self, persons: List[dict]) -> List[dict]:
        """把 Agent 人员字典适配成 PersonEvidence(sim shim) 喂给现有 rank()。

        人员字典需含: person_id / state / risk_score;
        可选: f1 / f6 / det_conf / pose_vis / class_name / confidence。
        缺 f1/f6 时以 f1=risk_score、f6=1.0(保持 risk_score≈f1*f6) 作为中性兜底。
        """
        from ranking import PersonEvidence, rank  # SRC_DIR 已在 sys.path

        evidences: List[PersonEvidence] = []
        person_id_by_det = {}

        for p in persons:
            pid = p.get("person_id")
            risk = p.get("risk_score")
            if pid is None or risk is None:
                continue  # 无效人员不计入排序

            state = str(p.get("state", "Standing"))
            try:
                risk_val = float(risk)
            except (TypeError, ValueError):
                continue

            det = SimpleNamespace(
                class_name=str(p.get("class_name", "civilian")),
                confidence=float(p.get("det_conf", p.get("confidence", 1.0))),
            )
            st = SimpleNamespace(state=state)
            rk = SimpleNamespace(
                state=state,
                f1=float(p.get("f1", risk_val)),
                f6=float(p.get("f6", 1.0)),
                risk_score=risk_val,
            )
            ev = PersonEvidence(
                detection=det,
                state=st,
                risk=rk,
                pose_vis=float(p.get("pose_vis", 0.0)),
            )
            person_id_by_det[id(det)] = str(pid)
            evidences.append(ev)

        if not evidences:
            return []

        ranked = rank(evidences)
        out: List[dict] = []
        for rr in ranked:
            out.append(
                {
                    "rank": rr.rank,
                    "person_id": person_id_by_det.get(id(rr.detection)) if rr.detection else None,
                    "class_name": rr.class_name,
                    "state": rr.state,
                    "risk_score": rr.risk_score,
                    "priority": rr.priority,
                    "details": rr.details,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _get_ranking(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        persons = self._load_persons(kwargs)
        ranking = self._rank_persons(persons)
        return {
            "ok": True,
            "action": "get_ranking",
            "count": len(ranking),
            "ranking": ranking,
        }

    def _assess_person(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        pid = kwargs.get("person_id")
        if pid is None:
            return {"ok": False, "action": "assess_person",
                    "error": "Missing required parameter: person_id."}
        persons = self._load_persons(kwargs)
        target = [p for p in persons if str(p.get("person_id")) == str(pid)]
        if not target:
            return {"ok": False, "action": "assess_person", "person_id": str(pid),
                    "error": f"person_id not found: {pid}"}
        ranked = self._rank_persons(target)
        if not ranked:
            return {"ok": False, "action": "assess_person", "person_id": str(pid),
                    "error": "person lacks valid risk evidence"}
        return {"ok": True, "action": "assess_person", "person_id": str(pid),
                "assessment": ranked[0]}