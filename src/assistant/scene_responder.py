"""src/assistant/scene_responder.py — 救援现场中文回答层 (V4.4)。

职责: 把 context_tool 查询到的现场上下文(context dict)渲染为一段面向救援人员
的中文回答文本。纯文本生成, 不调用 LLM / TTS / Agent / 数据库。

只依赖传入的 context, 输入结构:
{
    persons: [
        { person_id, state, risk_score, priority }
    ],
    latest_task: dict|None,   # {task_id, status, ...}
    latest_alert: dict|None,
    latest_event: dict|None,
    risk_summary: Any,
}

输出: 一段中文回答(str)。

规则:
  - 无人员 → "当前没有检测到受困人员。系统将继续监测现场。"
  - 单人员 → 状态 + 风险等级 + "建议优先处理。"
  - 多人员 → 按 priority HIGH > MEDIUM > LOW 排序输出, 并建议优先处理最高优先级者。
  - 存在 latest_task → 追加 "最近救援任务xxx，当前状态xxx。"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 优先级排序权重(HIGH 最高)
_PRIORITY_ORDER: Dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
_STATE_ZH: Dict[str, str] = {
    "Fall": "跌倒",
    "Lying": "躺地",
    "Help-Waving": "呼救",
    "Sitting": "坐",
    "Standing": "站立",
}


class SceneResponder:
    """把现场上下文渲染为中文救援回答(仅文本生成)。"""

    @staticmethod
    def _priority_key(p: Dict[str, Any]) -> tuple:
        pri = str(p.get("priority") or "").upper()
        return (_PRIORITY_ORDER.get(pri, 3), int(p.get("person_id") or 0))

    @staticmethod
    def _state_text(state: Any) -> str:
        s = str(state or "Unknown")
        return _STATE_ZH.get(s, s)

    def answer_scene(self, context: Dict[str, Any]) -> str:
        persons: List[dict] = context.get("persons") or []
        latest_task: Optional[dict] = context.get("latest_task")

        if not persons:
            base = "当前没有检测到受困人员。系统将继续监测现场。"
        else:
            # 按优先级排序(HIGH > MEDIUM > LOW), 同优先级按 person_id 升序
            ordered = sorted(persons, key=self._priority_key)
            head = f"当前发现{len(ordered)}名人员。\n"
            lines: List[str] = []
            for i, p in enumerate(ordered, start=1):
                state = self._state_text(p.get("state"))
                pri = str(p.get("priority") or "未知")
                if len(ordered) == 1:
                    lines.append(f"人员{p.get('person_id')}处于{state}状态，\n风险等级{pri}。")
                else:
                    lines.append(f"{i}号人员处于{state}状态，\n风险等级{pri}。")
            if len(ordered) == 1:
                base = head + "\n".join(lines) + "\n建议优先处理。"
            else:
                top = ordered[0]
                top_pri = str(top.get("priority") or "未知")
                base = (head + "\n".join(lines)
                        + f"\n建议优先处理{top.get('person_id')}号人员（风险等级{top_pri}）。")

        # 存在任务则统一追加
        if latest_task:
            task_id = latest_task.get("task_id")
            status = latest_task.get("status")
            base += f"\n最近救援任务{task_id}，当前状态{status}。"
        return base