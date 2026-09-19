"""
LLM client — 大模型推理客户端 (决策解释与建议生成, P0)
======================================================
职责: 提供 LLM 调用抽象层, provider 可切换(P0 仅内置 mock, 无任何网络请求)。

调用边界:
  - Client 负责组织 prompt(src/llm/prompts.py 的 build_advice_prompt)
    并按 provider 分发调用;
  - 返回值为模型**原始输出**(raw, 本实现为 JSON 文本);
  - 解析/校验/兜底一律由 src/llm/parser.py 完成, Client 不复制 Parser 逻辑;
  - 最终数据契约由 schemas/llm_output.json 约束。

未来接入真实 LLM(OpenAI 兼容接口)时, 注册新 provider 并实现对应
_call_* 方法即可, Parser 与 Prompt 均无需改动。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Union

from .prompts import TASK_PROPOSAL_VOCAB, build_advice_prompt


class UnsupportedProviderError(ValueError):
    """provider 不在支持列表时抛出; 不做静默降级。"""


class LLMClient:
    """LLM 调用抽象层。

    用法:
        client = LLMClient(provider="mock")
        raw = client.infer(vlm_output, person_events)
        result = llm_parser.parse(raw)   # 解析在 Parser 侧完成
    """

    SUPPORTED_PROVIDERS = ("mock",)

    def __init__(self, provider: str = "mock"):
        if provider not in self.SUPPORTED_PROVIDERS:
            raise UnsupportedProviderError(
                f"未支持的 LLM provider: {provider!r}, "
                f"当前支持: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider

    def infer(
        self, vlm_output: Dict[str, Any], person_events: List[Dict[str, Any]]
    ) -> Any:
        """基于环境证据与人员风险证据推理, 返回 LLM 原始输出。

        参数:
            vlm_output: VLM Parser 产出的标准 dict (schemas/vlm_output.json)。
            person_events: 人员事件列表, 每项含 person_id/state/risk_score 等。

        返回:
            模型原始输出(mock 下为 JSON 文本), 不做任何 schema 校验。
        """
        prompt = build_advice_prompt(vlm_output, person_events)
        return self._dispatch(prompt, vlm_output, person_events)

    # ------------------------------------------------------------------
    # provider 分发
    # ------------------------------------------------------------------
    def _dispatch(
        self,
        prompt: str,
        vlm_output: Dict[str, Any],
        person_events: List[Dict[str, Any]],
    ) -> Any:
        if self.provider == "mock":
            return self._call_mock(prompt, vlm_output, person_events)
        # __init__ 已挡住未知 provider, 此处仅防御
        raise UnsupportedProviderError(f"未支持的 LLM provider: {self.provider!r}")

    def _call_mock(
        self,
        prompt: str,
        vlm_output: Dict[str, Any],
        person_events: List[Dict[str, Any]],
    ) -> str:
        """Mock provider: 不发任何网络请求, 依据输入证据生成一份
        可被 parser 解析为合法 llm_output 的 JSON 文本。

        生成规则:
          - risk_explanations 逐人给出;
          - task_proposals 仅使用 TASK_PROPOSAL_VOCAB 中的动作:
            最高风险人员 → alert, 高严重度场景 → notify, 事件整体 → log。
        """
        scene, severity = "unknown", "medium"
        if isinstance(vlm_output, dict):
            scene = vlm_output.get("scene_type") or "unknown"
            severity = vlm_output.get("overall_severity") or "medium"

        persons = [
            p for p in (person_events or []) if isinstance(p, dict) and "person_id" in p
        ]

        risk_explanations = [
            {
                "pid": p["person_id"],
                "reason": f"状态 {p.get('state', 'unknown')}, "
                f"风险分 {p.get('risk_score', 'N/A')}",
            }
            for p in persons
        ]

        task_proposals: List[Dict[str, Any]] = []
        if persons:
            top = max(persons, key=lambda p: _risk_key(p))
            task_proposals.append(
                {
                    "action": "alert",
                    "target": top["person_id"],
                    "payload": {"severity": severity, "state": top.get("state")},
                }
            )
        if severity in ("high", "critical"):
            task_proposals.append(
                {
                    "action": "notify",
                    "target": None,
                    "payload": {"scene": scene, "severity": severity},
                }
            )
        task_proposals.append(
            {
                "action": "log",
                "target": None,
                "payload": {"scene": scene, "severity": severity, "n_persons": len(persons)},
            }
        )

        # 防御: 动作词必须来自词表, 不越界
        for proposal in task_proposals:
            if proposal["action"] not in TASK_PROPOSAL_VOCAB:
                raise RuntimeError(f"mock 动作越界: {proposal['action']!r} 不在 TASK_PROPOSAL_VOCAB")

        suggestions = (
            [f"优先处置风险分最高人员(person_id={p['person_id']})" for p in persons[:1]]
            if persons
            else ["无人员数据, 建议持续监测现场"]
        )

        payload = {
            "event_summary": f"Mock 推理: {scene} 场景, "
            f"整体严重度 {severity}, 共 {len(persons)} 名人员。",
            "risk_explanations": risk_explanations,
            "rescue_suggestions": suggestions,
            "task_proposals": task_proposals,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _call_openai_compatible(
        self,
        prompt: str,
        vlm_output: Dict[str, Any],
        person_events: List[Dict[str, Any]],
    ) -> str:
        """未来真实 LLM provider 接入点(OpenAI 兼容接口)。

        P0 Stage2 实现: 携带 SYSTEM_PROMPT/prompt 请求 → 返回文本。
        """
        raise NotImplementedError("真实 LLM provider 将在 P0 Stage2 接入")


def _risk_key(person: Dict[str, Any]) -> float:
    """提取人员风险分用于比较; 非数值按 0 处理。"""
    score = person.get("risk_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return 0.0
