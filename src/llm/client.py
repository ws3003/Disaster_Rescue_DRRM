"""
LLM client — 大模型推理客户端 (决策解释与建议生成, P0 → Stage4)
================================================================
职责: 提供 LLM 调用抽象层, provider 可切换:
  - "mock": 本地依据输入证据生成, 零网络请求 (P0 Stage1)
  - "qwen": Qwen, OpenAI 兼容 HTTP API, requests 直连 (Stage4)

配置(环境变量, 模板见 .env.example; 不自动加载 .env 文件):
  LLM_PROVIDER  默认 provider (仅当构造参数 provider=None 时生效, 缺省 mock)
  LLM_API_KEY   qwen provider 必需
  LLM_BASE_URL  OpenAI 兼容端点 (缺省 DashScope compatible-mode)
  LLM_MODEL     模型名 (缺省 qwen-plus)
  LLM_TIMEOUT   请求超时秒数 (缺省 30)

调用边界:
  - Client 组织 prompt(prompts.py 的 SYSTEM_PROMPT + build_advice_prompt)
    并按 provider 分发; 返回模型**原始输出**;
  - 解析/校验/兜底由 parser.py 负责, Client 不复制 Parser 逻辑
    (模型返回的非 JSON 文本由 parser 的 LLMParseError 处理);
  - API 层错误(网络/超时/HTTP/协议/空内容)在 Client 层抛 LLMProviderError。
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional

import requests

from .prompts import SYSTEM_PROMPT, TASK_PROPOSAL_VOCAB, build_advice_prompt


class UnsupportedProviderError(ValueError):
    """provider 不在支持列表时抛出; 不做静默降级。"""


class LLMConfigError(RuntimeError):
    """provider 配置缺失(如未设置 LLM_API_KEY)时抛出。"""


class LLMProviderError(RuntimeError):
    """真实 API 调用失败(网络/超时/HTTP错误/响应协议异常)时抛出。"""


# qwen provider 默认配置 (可被环境变量覆盖)
_QWEN_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_QWEN_DEFAULT_MODEL = "qwen-plus"
_DEFAULT_TIMEOUT = 30.0


def _load_llm_config() -> Dict[str, Any]:
    """从环境变量读取 qwen provider 配置, 缺失项用默认值。"""
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    return {
        "api_key": os.environ.get("LLM_API_KEY", "").strip(),
        "base_url": (os.environ.get("LLM_BASE_URL", "").strip().rstrip("/")
                     or _QWEN_DEFAULT_BASE_URL),
        "model": os.environ.get("LLM_MODEL", "").strip() or _QWEN_DEFAULT_MODEL,
        "timeout": timeout,
    }


class LLMClient:
    """LLM 调用抽象层。

    用法:
        client = LLMClient(provider="mock")        # 本地零网络
        client = LLMClient(provider="qwen")        # 真实 Qwen
        raw = client.infer(vlm_output, person_events)
        result = llm_parser.parse(raw)             # 解析在 Parser 侧完成

    参数:
        provider: "mock" / "qwen"; 缺省读 LLM_PROVIDER 环境变量, 再缺省 "mock"。
        transport: 可注入的 HTTP 传输函数(签名同 requests.post), 供测试
                   注入 fake transport; 缺省使用 requests.post。
    """

    SUPPORTED_PROVIDERS = ("mock", "qwen")

    def __init__(
        self,
        provider: Optional[str] = None,
        transport: Optional[Callable[..., Any]] = None,
    ):
        if provider is None:
            provider = os.environ.get("LLM_PROVIDER", "mock")
        if provider not in self.SUPPORTED_PROVIDERS:
            raise UnsupportedProviderError(
                f"未支持的 LLM provider: {provider!r}, "
                f"当前支持: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider
        self._transport = transport if transport is not None else requests.post
        if provider == "qwen":
            self._llm_config = _load_llm_config()

    def infer(
        self, vlm_output: Dict[str, Any], person_events: List[Dict[str, Any]]
    ) -> Any:
        """基于环境证据与人员风险证据推理, 返回 LLM 原始输出。

        参数:
            vlm_output: VLM Parser 产出的标准 dict (schemas/vlm_output.json)。
            person_events: 人员事件列表, 每项含 person_id/state/risk_score 等。

        返回:
            模型原始输出(mock/qwen 均为 JSON 文本), 不做任何 schema 校验。
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
        if self.provider == "qwen":
            return self._call_qwen(prompt, vlm_output, person_events)
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

    def _call_qwen(
        self,
        prompt: str,
        vlm_output: Dict[str, Any],
        person_events: List[Dict[str, Any]],
    ) -> str:
        """Qwen provider: OpenAI 兼容 /chat/completions, requests 直连。

        消息结构: system=SYSTEM_PROMPT, user=build_advice_prompt 生成的文本
        (其中已注入 vlm_output 与 person_events 证据)。
        所有 API 层错误在此抛出(LLMConfigError/LLMProviderError), 不静默降级;
        模型返回的非 JSON 文本交给 parser 处理。
        """
        cfg = self._llm_config
        if not cfg["api_key"]:
            raise LLMConfigError(
                "缺少 LLM_API_KEY 环境变量 (qwen provider 必需, 见 .env.example)"
            )

        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        url = f"{cfg['base_url']}/chat/completions"

        try:
            resp = self._transport(url, json=payload, headers=headers, timeout=cfg["timeout"])
        except requests.Timeout as exc:
            raise LLMProviderError(
                f"LLM 请求超时({cfg['timeout']}s): {exc}"
            ) from exc
        except requests.RequestException as exc:
            raise LLMProviderError(f"LLM 网络请求失败: {exc}") from exc

        status = getattr(resp, "status_code", None)
        if status != 200:
            snippet = str(getattr(resp, "text", ""))[:200]
            raise LLMProviderError(f"LLM API 返回 HTTP {status}: {snippet}")

        try:
            body = resp.json()
        except (ValueError, TypeError) as exc:
            raise LLMProviderError(f"LLM API 响应体不是合法 JSON: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                f"LLM API 响应缺少 choices/message/content: {str(body)[:200]}"
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError("LLM 模型返回内容为空")
        return content


def _risk_key(person: Dict[str, Any]) -> float:
    """提取人员风险分用于比较; 非数值按 0 处理。"""
    score = person.get("risk_score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return 0.0