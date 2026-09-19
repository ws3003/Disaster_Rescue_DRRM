"""
VLM client — 视觉语言模型客户端 (环境理解, P0)
================================================
职责: 提供 VLM 调用抽象层, provider 可切换(P0 仅内置 mock, 无任何网络请求)。

调用边界:
  - Client 负责组织 prompt(src/vlm/prompts.py)并按 provider 分发调用;
  - 返回值为模型**原始输出**(raw, 本实现为 JSON 文本);
  - 解析/校验/兜底一律由 src/vlm/parser.py 完成, Client 不复制 Parser 逻辑;
  - 最终数据契约由 schemas/vlm_output.json 约束。

未来接入真实 VLM(OpenAI 兼容: DashScope/Kimi/DeepSeek 等)时,
在 SUPPORTED_PROVIDERS 注册新 provider 并实现对应 _call_* 方法即可,
Parser 与 Prompt 均无需改动。
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from .prompts import build_scene_prompt


class UnsupportedProviderError(ValueError):
    """provider 不在支持列表时抛出; 不做静默降级。"""


# Mock provider 的固定返回(风格与 data/mock/vlm_output.json 同源,
# 可被 parser 解析为合法 vlm_output)
_MOCK_SCENE = {
    "scene_type": "collapse",
    "overall_severity": "critical",
    "hazards": [
        {"type": "debris", "brief": "主体结构垮塌, 大量瓦砾覆盖救援通道"},
        {"type": "smoke", "brief": "西北角可见浓烟, 疑似次生火情"},
    ],
    "environment_note": "Mock 场景: 多层建筑局部垮塌, 通道受阻, 存在二次垮塌风险。",
}


class VLMClient:
    """VLM 调用抽象层。

    用法:
        client = VLMClient(provider="mock")
        raw = client.analyze(image_input)
        scene = vlm_parser.parse(raw)   # 解析在 Parser 侧完成
    """

    SUPPORTED_PROVIDERS = ("mock",)

    def __init__(self, provider: str = "mock"):
        if provider not in self.SUPPORTED_PROVIDERS:
            raise UnsupportedProviderError(
                f"未支持的 VLM provider: {provider!r}, "
                f"当前支持: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider

    def analyze(
        self,
        image_input: Union[str, bytes, None],
        image_description: Optional[str] = None,
    ) -> Any:
        """分析灾害现场图片, 返回 VLM 原始输出。

        参数:
            image_input: 图片路径/字节; mock 模式允许任意占位输入(含 None)。
            image_description: 可选的图片文字提示, 注入 user prompt。

        返回:
            模型原始输出(mock 下为 JSON 文本), 不做任何 schema 校验。
        """
        prompt = build_scene_prompt(image_description)
        return self._dispatch(prompt, image_input)

    # ------------------------------------------------------------------
    # provider 分发
    # ------------------------------------------------------------------
    def _dispatch(self, prompt: str, image_input: Union[str, bytes, None]) -> Any:
        if self.provider == "mock":
            return self._call_mock(prompt, image_input)
        # __init__ 已挡住未知 provider, 此处仅防御
        raise UnsupportedProviderError(f"未支持的 VLM provider: {self.provider!r}")

    def _call_mock(
        self, prompt: str, image_input: Union[str, bytes, None]
    ) -> str:
        """Mock provider: 不发任何网络请求, 直接返回固定 JSON 文本。

        prompt 已在 analyze() 中生成完毕(保留真实调用边界), mock 忽略其内容。
        """
        return json.dumps(_MOCK_SCENE, ensure_ascii=False)

    def _call_openai_compatible(
        self, prompt: str, image_input: Union[str, bytes, None]
    ) -> str:
        """未来真实 VLM provider 接入点(OpenAI 兼容接口)。

        P0 Stage2 实现: 编码图片 → 携带 SYSTEM_PROMPT/prompt 请求 → 返回文本。
        """
        raise NotImplementedError("真实 VLM provider 将在 P0 Stage2 接入")
