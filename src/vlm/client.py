"""
VLM client — 视觉语言模型客户端 (环境理解, P0 → Stage3)
========================================================
职责: 提供 VLM 调用抽象层, provider 可切换:
  - "mock": 本地固定 JSON, 零网络请求 (P0 Stage1)
  - "qwen": Qwen3-VL-Plus, OpenAI 兼容 HTTP API, requests 直连 (Stage3)

配置(环境变量, 模板见 .env.example; 不自动加载 .env 文件):
  VLM_PROVIDER  默认 provider (仅当构造参数 provider=None 时生效, 缺省 mock)
  VLM_API_KEY   qwen provider 必需
  VLM_BASE_URL  OpenAI 兼容端点 (缺省 DashScope compatible-mode)
  VLM_MODEL     模型名 (缺省 qwen3-vl-plus)
  VLM_TIMEOUT   请求超时秒数 (缺省 30)

调用边界:
  - Client 组织 prompt(prompts.py 的 SYSTEM_PROMPT + build_scene_prompt)
    并按 provider 分发; 返回模型**原始输出**;
  - 解析/校验/兜底由 parser.py 负责, Client 不复制 Parser 逻辑
    (模型返回的非 JSON 文本由 parser 的 SceneParseError 处理);
  - API 层错误(网络/超时/HTTP/协议/空内容)在 Client 层抛 VLMProviderError。
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Callable, Dict, Optional, Union

import requests

from .prompts import SYSTEM_PROMPT, build_scene_prompt


class UnsupportedProviderError(ValueError):
    """provider 不在支持列表时抛出; 不做静默降级。"""


class VLMConfigError(RuntimeError):
    """provider 配置缺失(如未设置 VLM_API_KEY)时抛出。"""


class VLMInputError(ValueError):
    """图片输入非法(类型不支持/文件不可读/内容为空)时抛出。"""


class VLMProviderError(RuntimeError):
    """真实 API 调用失败(网络/超时/HTTP错误/响应协议异常)时抛出。"""


# qwen provider 默认配置 (可被环境变量覆盖)
_QWEN_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_QWEN_DEFAULT_MODEL = "qwen3-vl-plus"
_DEFAULT_TIMEOUT = 30.0


def _load_qwen_config() -> Dict[str, Any]:
    """从环境变量读取 qwen provider 配置, 缺失项用默认值。"""
    try:
        timeout = float(os.environ.get("VLM_TIMEOUT", _DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    return {
        "api_key": os.environ.get("VLM_API_KEY", "").strip(),
        "base_url": (os.environ.get("VLM_BASE_URL", "").strip().rstrip("/")
                     or _QWEN_DEFAULT_BASE_URL),
        "model": os.environ.get("VLM_MODEL", "").strip() or _QWEN_DEFAULT_MODEL,
        "timeout": timeout,
    }


def _guess_mime(data: bytes) -> str:
    """按魔数识别图片 MIME 类型, 识别失败按 jpeg 处理。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


def _encode_image_data_url(image_input: Union[str, bytes, "os.PathLike[str]"]) -> str:
    """将图片路径/字节编码为 base64 data URL (OpenAI 兼容格式)。"""
    if isinstance(image_input, (bytes, bytearray)):
        data = bytes(image_input)
    elif isinstance(image_input, (str, os.PathLike)):
        try:
            with open(os.fspath(image_input), "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise VLMInputError(f"无法读取图片文件: {exc}") from exc
    else:
        raise VLMInputError(
            f"qwen provider 需要图片路径或图片字节, "
            f"收到: {type(image_input).__name__}"
        )
    if not data:
        raise VLMInputError("图片内容为空")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{_guess_mime(data)};base64,{encoded}"


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
        client = VLMClient(provider="mock")        # 本地零网络
        client = VLMClient(provider="qwen")        # 真实 Qwen3-VL-Plus
        raw = client.analyze(image_input)
        scene = vlm_parser.parse(raw)              # 解析在 Parser 侧完成

    参数:
        provider: "mock" / "qwen"; 缺省读 VLM_PROVIDER 环境变量, 再缺省 "mock"。
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
            provider = os.environ.get("VLM_PROVIDER", "mock")
        if provider not in self.SUPPORTED_PROVIDERS:
            raise UnsupportedProviderError(
                f"未支持的 VLM provider: {provider!r}, "
                f"当前支持: {self.SUPPORTED_PROVIDERS}"
            )
        self.provider = provider
        self._transport = transport if transport is not None else requests.post
        if provider == "qwen":
            self._qwen_config = _load_qwen_config()

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
            模型原始输出(mock/qwen 均为 JSON 文本), 不做任何 schema 校验。
        """
        prompt = build_scene_prompt(image_description)
        return self._dispatch(prompt, image_input)

    # ------------------------------------------------------------------
    # provider 分发
    # ------------------------------------------------------------------
    def _dispatch(self, prompt: str, image_input: Union[str, bytes, None]) -> Any:
        if self.provider == "mock":
            return self._call_mock(prompt, image_input)
        if self.provider == "qwen":
            return self._call_qwen(prompt, image_input)
        # __init__ 已挡住未知 provider, 此处仅防御
        raise UnsupportedProviderError(f"未支持的 VLM provider: {self.provider!r}")

    def _call_mock(
        self, prompt: str, image_input: Union[str, bytes, None]
    ) -> str:
        """Mock provider: 不发任何网络请求, 直接返回固定 JSON 文本。

        prompt 已在 analyze() 中生成完毕(保留真实调用边界), mock 忽略其内容。
        """
        return json.dumps(_MOCK_SCENE, ensure_ascii=False)

    def _call_qwen(self, prompt: str, image_input: Union[str, bytes, None]) -> str:
        """Qwen provider: OpenAI 兼容 /chat/completions, requests 直连。

        消息结构: system=SYSTEM_PROMPT, user=[图片(data URL), prompt 文本]。
        所有 API 层错误在此抛出(VLMConfigError/VLMInputError/VLMProviderError),
        不静默降级; 模型返回的非 JSON 文本交给 parser 处理。
        """
        cfg = self._qwen_config
        if not cfg["api_key"]:
            raise VLMConfigError(
                "缺少 VLM_API_KEY 环境变量 (qwen provider 必需, 见 .env.example)"
            )
        data_url = _encode_image_data_url(image_input)

        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": prompt},
                    ],
                },
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
            raise VLMProviderError(
                f"VLM 请求超时({cfg['timeout']}s): {exc}"
            ) from exc
        except requests.RequestException as exc:
            raise VLMProviderError(f"VLM 网络请求失败: {exc}") from exc

        status = getattr(resp, "status_code", None)
        if status != 200:
            snippet = str(getattr(resp, "text", ""))[:200]
            raise VLMProviderError(f"VLM API 返回 HTTP {status}: {snippet}")

        try:
            body = resp.json()
        except (ValueError, TypeError) as exc:
            raise VLMProviderError(f"VLM API 响应体不是合法 JSON: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VLMProviderError(
                f"VLM API 响应缺少 choices/message/content: {str(body)[:200]}"
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise VLMProviderError("VLM 模型返回内容为空")
        return content