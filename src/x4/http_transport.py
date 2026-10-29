"""X4/E6 HTTP 推理客户端 Transport（`src/x4/http_transport.py`）。

真实 POST transport（`requests`，已是项目声明依赖）。**不修改 `CloudYoloClient`**：
本模块提供 `make_http_infer(cfg)` / `new_http_client(cfg)` 供调用方把真实 HTTP 接进现有 `infer_func` 注入模式，
重试仍由 `CloudYoloClient`（唯一有界重试层）负责，本模块**单次调用只发一次请求**。

- 编码：`encode_jpeg(image_bgr, quality=95) -> bytes`（cv2.imencode；失败抛非瞬时 `CloudUnavailableError`）。
- 发送：`send_predict(endpoint, image_bgr, model_id, timeout) -> Tuple[Detection, ...]`
  `POST <endpoint>`；`multipart/form-data` `file`(image/jpeg) + `model_id`；timeout 传入 `requests.post`；空 endpoint/格式错误不发起请求。
- 响应：先按状态码走 `map_status_to_error`，再 `parse_predict_response`；非法 JSON/结构 → `CloudInvalidResponseError`。
- 异常：requests timeout→`CloudTimeoutError`、连接→`CloudConnectionError`、状态→既有映射、无效响应→非瞬时。
- 不读/不打印密钥；不把 ndarray 当 JSON；不自动拼重复 `/predict`。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional, Tuple

import cv2
import requests  # 项目依赖（requirements.txt）

from src.x4.cloud_yolo import (
    CloudConnectionError,
    CloudInvalidResponseError,
    CloudTimeoutError,
    CloudUnavailableError,
    CloudYoloClient,
)
from src.x4.config import X4Config
from src.x4.contracts import Detection
from src.x4.http_schema import (
    IMAGE_CONTENT_TYPE,
    endpoint_enabled,
    map_status_to_error,
    parse_predict_response,
    validate_endpoint,
)

# 便于测试替换传输（严格等价于 requests.post）
_post: Callable[..., Any] = requests.post


def encode_jpeg(image_bgr: Any, quality: int = 95) -> bytes:
    """BGR 帧 → JPEG bytes（不把 ndarray 当 JSON；不修改输入）。失败抛非瞬时 `CloudUnavailableError`。"""
    if not hasattr(image_bgr, "shape") or getattr(image_bgr, "ndim", 0) not in (2, 3):
        raise CloudUnavailableError("jpeg encode failed: 非法帧类型")
    ok, buf = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok or buf is None:
        raise CloudUnavailableError("jpeg encode failed")
    return bytes(buf)


def _resolve_token(token: Optional[str]) -> Optional[str]:
    """解析 X4 服务端 Bearer Token。

    - 显式 token 参数非空时以其为准；参数为 `None` 才从环境变量 `X4_API_TOKEN` 读取
      （不写进 YAML/源码/dataclass）。
    - None、空字符串、纯空白一律视为未配置 → 返回 None（不发送 Authorization 头），
      显式传入的空/空白不会回退到环境变量（显式禁用不被 env 意外覆盖）。
    - 返回去空格后的非空 token；绝不把 token 写入日志/异常/响应。
    """
    if token is not None:
        tok = token.strip()
        return tok if tok else None
    env = os.getenv("X4_API_TOKEN")
    if env is None:
        return None
    env = env.strip()
    return env if env else None


def send_predict(endpoint: str, image_bgr: Any, model_id: str, timeout: Optional[float] = None,
                 *, token: Optional[str] = None) -> Tuple[Detection, ...]:
    """单次 HTTP 推理请求（**不自带重试**；重试由 `CloudYoloClient` 统一负责）。

    成功返回经 `parse_predict_response` 校验的检测元组；`detections=[]` 视为成功。
    `token` 非空时以 `Authorization: Bearer <token>` 请求头发送；空/空白视为未配置不发送。
    **禁用重定向**（`allow_redirects=False`），避免认证头经响应重定向泄露到其他地址。
    """
    if not endpoint_enabled(endpoint):
        raise CloudUnavailableError("cloud endpoint disabled")   # 空 endpoint 不发请求
    endpoint = validate_endpoint(endpoint)                        # 格式非法 → ValueError（不发请求）
    jpeg = encode_jpeg(image_bgr)
    headers: dict[str, str] = {}
    tok = _resolve_token(token)
    if tok is not None:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        resp = _post(
            endpoint,
            files={"file": ("frame.jpg", jpeg, IMAGE_CONTENT_TYPE)},
            data={"model_id": model_id},
            headers=headers or None,
            allow_redirects=False,            # 认证请求不跟随重定向（避免 token 泄露到别处）
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        raise CloudTimeoutError() from None
    except requests.exceptions.RequestException:
        raise CloudConnectionError() from None
    if not (200 <= resp.status_code < 300):
        raise map_status_to_error(resp.status_code)
    try:
        data = resp.json()
        return parse_predict_response(data).detections
    except ValueError as exc:
        raise CloudInvalidResponseError(str(exc)) from exc


def make_http_infer(cfg: X4Config) -> Callable[[Any, Optional[float]], Tuple[Detection, ...]]:
    """基于真实 `X4Config` 构造 infer 函数（绑定 endpoint/model_id；timeout 由调用方传入）。

    Bearer Token 不存于 `X4Config`（frozen dataclass，repr 会泄露）；由 `send_predict` 在
    发送时经 `_resolve_token` 从环境变量 `X4_API_TOKEN` 读取。
    """
    def _infer(image_bgr: Any, timeout: Optional[float] = None) -> Tuple[Detection, ...]:
        return send_predict(cfg.cloud_endpoint, image_bgr, cfg.model_id, timeout, token=None)
    return _infer


def new_http_client(cfg: X4Config) -> CloudYoloClient:
    """按配置构造真实 HTTP 客户端（注入 `make_http_infer`，保留 CloudYoloClient 单层有界重试）。

    仅当 `cfg.cloud_endpoint` 非空时才会真正发送请求；空=禁用（transport 层直接抛非瞬时异常）。
    本模块不把该客户端自动接入 service/生产调用链（下一阶段接线）。
    """
    if not endpoint_enabled(cfg.cloud_endpoint):
        raise CloudUnavailableError("cloud endpoint not configured")
    return CloudYoloClient(
        infer_func=make_http_infer(cfg),
        model_id=cfg.model_id,
        max_attempts=cfg.max_attempts,
    )