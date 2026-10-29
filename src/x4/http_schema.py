"""X4/E6 HTTP 推理接口契约（`src/x4/http_schema.py`）——**契约与校验层，不实现 HTTP 调用/客户端/服务端/真实推理**。

定义了"云端 YOLOv26l 推理"的最小请求/成功响应/错误映射规则，供后续真实 HTTP 客户端与服务端共用。
本模块只依赖标准库 + 既有 X4 契约（`Detection`/`validate_detections`/`CloudError` 子类），不新增依赖，不写 Pydantic。

约定：
- 方法/端点：`POST /predict`；`cloud_endpoint` 为**含 `/predict` 的完整预测 URL**；空=未配置（禁用）。
- 请求：`multipart/form-data`，字段 `file`(JPEG) + `model_id`(非空模型别名，如 `yolov26l`，非本地文件路径)。
  不发送 `max_attempts`；不把 NumPy ndarray 直接当 JSON。
- 成功响应：JSON `{model_id, detections[], latency_ms}`；检测项复用现有 `Detection` 契约；空 `detections=[]`=成功无目标（非失败）。
- 错误映射：仅把 HTTP 状态映射到**既有** `CloudError` 子类（不新增异常类）；消息不含密钥/敏感内容。

**注意**：本模块不执行网络；不把 ndarray 序列化；不以任何方式发起请求。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit

from src.x4.cloud_yolo import (
    CloudConnectionError,
    CloudError,
    CloudInvalidResponseError,
    CloudUnavailableError,
)
from src.x4.contracts import Detection, validate_detections

HTTP_METHOD = "POST"
PREDICT_PATH = "/predict"          # cloud_endpoint 应为含该段的完整预测 URL
FORM_FIELD_FILE = "file"
FORM_FIELD_MODEL = "model_id"
IMAGE_CONTENT_TYPE = "image/jpeg"

# multipart/form-data 请求字段（后续 HTML HTTP 客户端据此构造；本模块不实现网络）
REQUEST_FIELDS = (FORM_FIELD_FILE, FORM_FIELD_MODEL)


# ----------------------------------------------------------------------
# endpoint 规则
# ----------------------------------------------------------------------
def endpoint_enabled(endpoint: str | None) -> bool:
    """空串/None 表示未配置端点为禁用（不可据此发起请求）。"""
    return bool(endpoint is not None and str(endpoint).strip())


def validate_endpoint(endpoint: str) -> str:
    """非空 endpoint 必须是合法 HTTP/HTTPS 且含主机名；否则抛 ValueError。

    空串不视为可转换 URL（调用方应先 `endpoint_enabled` 判断）。返回去空格后的 endpoint。
    """
    e = str(endpoint).strip()
    if not e:
        return ""
    parsed = urlsplit(e)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"endpoint 必须为 http/https, 得到 {parsed.scheme!r}")
    if not (parsed.netloc and parsed.hostname):
        raise ValueError(f"endpoint 缺少主机名: {e!r}")
    return e


class PredictRequest:
    """`multipart/form-data` 请求的文档化结构。仅表达字段规则，不承载图片字节/不发起请求。"""

    __slots__ = ("model_id",)

    def __init__(self, model_id: str):
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id 必须为非空字符串")
        self.model_id = model_id.strip()

    @classmethod
    def form_fields(cls, model_id: str) -> Dict[str, str]:
        """返回合法的 multipart 字段（file 为图像文件本体，由真实客户端在发送时附带；此处仅声明 model_id）。"""
        return {FORM_FIELD_MODEL: cls(model_id).model_id}


@dataclass(frozen=True)
class PredictResponse:
    """成功推理响应（JSON 解析后的结构）。"""

    model_id: str
    detections: Tuple[Detection, ...]
    latency_ms: float


def _strict_detections(raw: Any) -> None:
    """契约层**严格**结构校验（区别于 `validate_detections` 的宽松 `_coerce`）。

    `_coerce` 对 dict 会把缺失字段填充(-1/"")并把类型强转；接口契约要求：
    缺失必填字段、class_id 非 int、class_name 非 str、confidence 越界、bbox 非法都必须拒绝。
    """
    if raw is None or not isinstance(raw, (list, tuple)):
        raise ValueError("detections 缺失或非列表")
    for it in raw:
        if not isinstance(it, dict):
            continue  # 非 dict（Detection 对象/不可规整输入）交由 validate_detections 处理
        for k in ("class_id", "class_name", "confidence", "bbox"):
            if k not in it:
                raise ValueError(f"detection 缺少字段: {k}")
        cid = it["class_id"]
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise ValueError(f"class_id 必须为 int, 得到 {cid!r}")
        name = it["class_name"]
        if not isinstance(name, str) or not name:
            raise ValueError("class_name 必须为非空 str")
        conf = it["confidence"]
        if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
                or not math.isfinite(float(conf)) or not (0.0 <= float(conf) <= 1.0):
            raise ValueError(f"confidence 必须为 [0,1] 数值, 得到 {conf!r}")
        bb = it["bbox"]
        if not isinstance(bb, (list, tuple)) or len(bb) != 4:
            raise ValueError("bbox 必须为长度为 4 的序列")
        for v in bb:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise ValueError(f"bbox 含非法/非有限值: {v!r}")


def parse_predict_response(data: Any) -> PredictResponse:
    """把服务端 JSON 响应 dict 解析为 `PredictResponse`；非法抛 ValueError（调用方归类）。"""
    if not isinstance(data, dict):
        raise ValueError("predict response must be a dict")

    model_id = data.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id 缺失/为空/类型错误")

    _strict_detections(data.get("detections"))               # 契约层严格校验（拒缺失/类型错/越界/Inf）
    detections = validate_detections(data.get("detections")) # 复用现有校验；[]=成功无目标

    latency = data.get("latency_ms")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        raise ValueError(f"latency_ms 必须为数值, 得到 {latency!r}")
    latency_f = float(latency)
    if not math.isfinite(latency_f) or latency_f < 0:
        raise ValueError(f"latency_ms 必须为有限非负数, 得到 {latency!r}")

    return PredictResponse(model_id=model_id.strip(), detections=detections, latency_ms=latency_f)


# ----------------------------------------------------------------------
# HTTP 状态 -> 既有 CloudError 子类 映射（仅定义规则，不实现 HTTP/重试）
# ----------------------------------------------------------------------
def map_status_to_error(status: int) -> CloudError:
    """把 HTTP 状态码映射为既有 `CloudError` 子类实例（复用现有异常，不新增类）。"""
    if status == 400:
        return CloudInvalidResponseError("http 400: bad request")
    if status in (401, 403):
        return CloudUnavailableError("http auth error")            # 无专属 auth 类，复用于不可用
    if status == 404:
        return CloudUnavailableError("http 404: endpoint not found")
    if 500 <= int(status) <= 599:
        return CloudConnectionError("http 5xx: server error")       # 归连接/服务端错误类，瞬时→客户端可重试
    return CloudUnavailableError(f"http {status}: protocol/unavailable")  # 未知状态安全默认