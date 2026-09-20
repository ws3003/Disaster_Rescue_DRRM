"""
server/schemas.py — API Gateway Pydantic 请求/响应模型
======================================================
定义 REST 接口 /api/v1/analyze 的请求与响应契约。

注意: 这里是 API 层的 DTO (Pydantic), 不改变
schemas/vlm_output.json 与 schemas/llm_output.json 两个契约;
vlm_output / llm_output 字段仍由 src/vlm/parser 与 src/llm/parser
产出, 与契约层保持一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PersonEvent(BaseModel):
    """单名人员的边缘感知证据 (供 LLM 作人员风险输入)。"""

    person_id: int = Field(..., description="人员编号, 对应感知层 person_id")
    state: str = Field(..., description="人员状态, 如 Fall / Lying / Standing 等")
    risk_score: float = Field(..., description="DRRM 风险评分 [0,1]")
    priority: str = Field(..., description="优先级, 如 HIGH / MEDIUM / LOW")


class AnalyzeRequest(BaseModel):
    """POST /api/v1/analyze 请求体。"""

    image_path: str = Field(..., description="灾害现场图片路径 (Edge Layer 可访问)")
    person_events: List[PersonEvent] = Field(
        default_factory=list, description="人员事件列表, 可为空"
    )


class AnalyzeResponse(BaseModel):
    """POST /api/v1/analyze 响应体 (成功与失败统一结构)。"""

    success: bool = Field(..., description="是否成功")
    vlm_output: Dict[str, Any] = Field(
        default_factory=dict, description="标准化 vlm_output (失败为空字典)"
    )
    llm_output: Dict[str, Any] = Field(
        default_factory=dict, description="标准化 llm_output (失败为空字典)"
    )
    error: Optional[str] = Field(None, description="失败原因; 成功为 null")


# 便利构造器, 供 api.py 与测试复用
def ok(vlm_output: Dict[str, Any], llm_output: Dict[str, Any]) -> AnalyzeResponse:
    return AnalyzeResponse(success=True, vlm_output=vlm_output, llm_output=llm_output,
                           error=None)


def fail(message: str, status_code: Optional[int] = None) -> AnalyzeResponse:
    """失败响应; status_code 仅作占位, 实际 HTTP 状态码在 api.py 的 JSONResponse 设置。"""
    return AnalyzeResponse(success=False, vlm_output={}, llm_output={}, error=message)


# 成功/失败的 HTTP 状态码约定 (供文档与测试引用)
HTTP_OK = 200
HTTP_NOT_FOUND = 404        # 图片不存在/不可读
HTTP_UNPROCESSABLE = 422    # 请求体不符合 schema (FastAPI 自动)
HTTP_CONFIG_ERROR = 500     # 服务端缺 API Key 等配置
HTTP_UPSTREAM_ERROR = 502   # VLM/LLM 或 parser 失败