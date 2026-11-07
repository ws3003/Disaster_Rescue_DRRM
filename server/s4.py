"""server/s4.py — V4.4 S4 只读查询接口（契约 schemas/s4_contract.json v0.1.0）。

本模块提供独立的 FastAPI APIRouter，由 server/api.py 挂载。

关键约定
--------
- 统一响应信封 ``{code, message, data, meta}``：
  成功 ``code="OK", message="ok"``；错误 ``data=null, meta={}``。
- 业务判断以 ``code`` 为准；``message`` 仅展示（文案契约未固定，用短中文）。
- Bearer 鉴权仅作用于本 router 内五个 S4 路由（环境变量 ``S4_API_TOKEN``）：
  服务端未配置 -> HTTP 500 INTERNAL_ERROR；客户端缺失/无效 -> HTTP 401 UNAUTHORIZED。
  鉴权在路由内联执行，不使用全局依赖/异常处理器，避免影响 V4.3 既有路由。
- 错误处理为 S4 内联（S4Error -> 四字段信封），参数校验手动解析（契约：类型/取值错误->400）。
- 数据来源：src.database.db.Database（默认 data/drrm.db；测试经 get_db 依赖覆盖注入临时库）。
- 不改动 events/persons/tasks/agent_logs 表结构与既有 response 模型。
"""

from __future__ import annotations

import hmac
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse

from src.database.db import Database

router = APIRouter(prefix="/api/v1")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
PERSON_EVENT_TYPE = "high_risk_alert"
# 兼容 V4.3 既有告警和 Edge/C4 新同步的标准人员告警；两者都应展示给小程序。
PERSON_EVENT_TYPES = (PERSON_EVENT_TYPE, "person_alert")

_CODES = {
    "INVALID_PARAMETER": "参数非法",
    "UNAUTHORIZED": "认证缺失或无效",
    "FORBIDDEN": "权限不足",
    "NOT_FOUND": "资源不存在",
    "VALIDATION_ERROR": "校验失败",
    "INTERNAL_ERROR": "服务端内部错误",
}


class S4Error(Exception):
    """S4 业务错误：由路由内联转成四字段错误信封。"""

    def __init__(self, http_status: int, code: str) -> None:
        super().__init__(code)
        self.http_status = http_status
        self.code = code


# ----------------------------------------------------------------------
# 响应信封 与 数据访问依赖
# ----------------------------------------------------------------------

def _ok_json(data: Any, meta: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(
        content={
            "code": "OK",
            "message": "ok",
            "data": data,
            "meta": meta if meta is not None else {},
        }
    )


def _fail_json(http_status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={
            "code": code,
            "message": _CODES.get(code, _CODES["INTERNAL_ERROR"]),
            "data": None,
            "meta": {},
        },
    )


def get_db() -> Database:
    """注入真实数据库（运行时 data/drrm.db；测试用 dependency_overrides 覆盖）。"""
    return Database(db_path=os.environ.get("DRRM_DB", "data/drrm.db"))


def _require_s4_token(authorization: Optional[str]) -> None:
    token = (os.environ.get("S4_API_TOKEN") or "").strip()
    if not token:
        # 服务端未配置 Token：已确认语义 -> 500 INTERNAL_ERROR
        raise S4Error(500, "INTERNAL_ERROR")
    # 等时比较, 不泄露凭据; 缺失/空值/格式错误/不匹配一律 401。
    expected = f"Bearer {token}".encode("utf-8")
    provided = (authorization or "").encode("utf-8")
    if not authorization or not hmac.compare_digest(provided, expected):
        raise S4Error(401, "UNAUTHORIZED")


# ----------------------------------------------------------------------
# 校验（手动解析；契约：参数缺失/类型/格式/取值范围错误 -> 400）
# ----------------------------------------------------------------------

def _int_or_400(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise S4Error(400, "INVALID_PARAMETER")


def _paging(
    page: Optional[str],
    page_size: Optional[str],
) -> Tuple[int, int]:
    p = 1 if page is None else _int_or_400(page)
    ps = DEFAULT_PAGE_SIZE if page_size is None else _int_or_400(page_size)
    if p < 1 or ps < 1 or ps > MAX_PAGE_SIZE:
        raise S4Error(400, "INVALID_PARAMETER")
    return p, ps


def _validate_category(category: Optional[str]) -> None:
    if category is not None and category not in ("person", "sensor_environment"):
        raise S4Error(400, "INVALID_PARAMETER")


def _parse_alert_key(alert_key: str) -> Tuple[str, str]:
    """返回 ("person"|"sensor", 原始ID)；格式非法 -> 400。"""
    if isinstance(alert_key, str):
        prefix, _, raw = alert_key.partition(":")
        if prefix == "person" and raw:
            return "person", raw
        if prefix == "sensor" and raw:
            return "sensor", raw
    raise S4Error(400, "INVALID_PARAMETER")


# ----------------------------------------------------------------------
# DTO 组装（字段与契约 dto_rules 一致）
# ----------------------------------------------------------------------

def _optional_finite_number(value: Any) -> Optional[float]:
    """将 SQLite/C4 可能保存成文本的数值恢复为 API 契约中的 number。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _person_item(ev: Dict[str, Any]) -> Dict[str, Any]:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    return {
        "alert_key": f"person:{ev['event_id']}",
        "source": "person",
        "category": "person",
        "type": ev.get("type"),
        "severity": ev.get("severity"),
        "ts": ev.get("ts"),
        "person_id": ev.get("person_id"),
        "title": payload.get("title"),
        "state": payload.get("state"),
        "priority": payload.get("priority"),
        "risk_score": _optional_finite_number(payload.get("risk_score")),
    }


def _sensor_item(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "alert_key": f"sensor:{a['alert_id']}",
        "source": "sensor",
        "category": "sensor_environment",
        "type": a.get("type"),
        "severity": a.get("severity"),
        "status": a.get("status"),
        "temperature": a.get("temperature"),
        "humidity": a.get("humidity"),
        "threshold": a.get("threshold"),
        "occurred_at": a.get("occurred_at"),
        "resolved_at": a.get("resolved_at"),
        "device_captured_at": a.get("device_captured_at"),
        "reading_id": a.get("reading_id"),
        "received_at": a.get("received_at"),
    }


def _reading_item(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "reading_id": r["reading_id"],
        "sensor_id": r["sensor_id"],
        "temperature": r["temperature"],
        "humidity": r.get("humidity"),
        "device_captured_at": r["device_captured_at"],
        "edge_received_at": r.get("edge_received_at"),
        "received_at": r.get("received_at"),
    }


# ----------------------------------------------------------------------
# 分页取数（组序：先人员组、后传感器组；不做跨表时间排序）
# ----------------------------------------------------------------------

def _person_page(
    db: Database, page: int, page_size: int, person_total: int
) -> List[Dict[str, Any]]:
    offset = (page - 1) * page_size
    if person_total <= 0 or offset >= person_total:
        return []
    limit = min(page_size, person_total - offset)
    return [
        _person_item(e)
        for e in db.query_person_alerts(
            PERSON_EVENT_TYPES, limit=limit, offset=offset
        )
    ]


def _sensor_block(db: Database, offset: int, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    return [
        _sensor_item(a)
        for a in db.query_sensor_alerts(limit=limit, offset=offset)
    ]


# ----------------------------------------------------------------------
# 共享查询业务函数（无 FastAPI Depends / 无认证；供 S4 Token 路由与
# 微信 Session 用户态路由共同调用，避免复制第二套查询实现）。
# 输入：db + 业务参数；输出：(data, meta)。错误经 S4Error 抛出由上层信封处理。
# ----------------------------------------------------------------------
def query_system_status(
    db: Database,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """S4-01 占位：device.status=unknown / modules={}（真实健康聚合由 WP-3 补充）。"""
    return {"device": {"status": "unknown"}, "modules": {}}, {}


def query_alerts(
    db: Database,
    category: Optional[str],
    page: Optional[str],
    page_size: Optional[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """告警列表查询（person / sensor_environment / 合并 + 分页）。"""
    _validate_category(category)
    page, page_size = _paging(page, page_size)
    person_total = db.count_person_alerts(PERSON_EVENT_TYPES)
    sensor_total = db.count_sensor_alerts()

    if category == "person":
        items = _person_page(db, page, page_size, person_total)
        total = person_total
    elif category == "sensor_environment":
        items = _sensor_block(db, (page - 1) * page_size, page_size)
        total = sensor_total
    else:
        # 合并：先人员组、后传感器组；全局分页
        total = person_total + sensor_total
        items: List[Dict[str, Any]] = []
        offset = (page - 1) * page_size
        taken = 0
        if person_total > 0 and offset < person_total:
            items = _person_page(db, page, page_size, person_total)
            taken = len(items)
        if taken < page_size:
            s_offset = max(0, offset - person_total)
            items += _sensor_block(db, s_offset, page_size - taken)

    has_more = page * page_size < total
    meta = {
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": has_more,
    }
    return items, meta


def query_alert_detail(
    db: Database, alert_key: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """告警详情（person / sensor）。"""
    prefix, raw = _parse_alert_key(alert_key)
    if prefix == "person":
        ev = db.get_event(raw)
        if ev is None:
            raise S4Error(404, "NOT_FOUND")
        return _person_item(ev), {}
    alert = db.get_sensor_alert(raw)
    if alert is None:
        raise S4Error(404, "NOT_FOUND")
    return _sensor_item(alert), {}


def query_alert_related(
    db: Database, alert_key: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """告警关联信息（关联读数；media 现为占位，WP-4 补充）。"""
    prefix, raw = _parse_alert_key(alert_key)
    readings: List[Dict[str, Any]] = []
    if prefix == "person":
        if db.get_event(raw) is None:
            raise S4Error(404, "NOT_FOUND")
        # 人员告警不关联环境读数
    else:
        alert = db.get_sensor_alert(raw)
        if alert is None:
            raise S4Error(404, "NOT_FOUND")
        rid = alert.get("reading_id")
        if rid:
            reading = db.get_reading(rid)
            if reading is not None:
                readings = [_reading_item(reading)]
    return {"readings": readings, "media": [], "media_supported": False}, {}


def query_readings(
    db: Database,
    sensor_id: Optional[str],
    start: Optional[str],
    end: Optional[str],
    page: Optional[str],
    page_size: Optional[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """环境读数查询（sensor_id / start / end 过滤 + 分页）。"""
    page, page_size = _paging(page, page_size)
    if start is not None and end is not None and end < start:
        raise S4Error(422, "VALIDATION_ERROR")

    total = db.count_readings(
        sensor_id=sensor_id,
        start_device_captured_at=start,
        end_device_captured_at=end,
    )
    rows = db.query_readings(
        sensor_id=sensor_id,
        start_device_captured_at=start,
        end_device_captured_at=end,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    items = [_reading_item(r) for r in rows]
    has_more = page * page_size < total
    meta = {
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": has_more,
    }
    return items, meta


# ----------------------------------------------------------------------
# 五个只读接口（请求方法/路径/参数/字段/错误码 遵循契约）
# ----------------------------------------------------------------------

@router.get("/system/status")
def system_status(
    authorization: Optional[str] = Header(default=None),
) -> JSONResponse:
    try:
        _require_s4_token(authorization)
        data, meta = query_system_status(None)
        return _ok_json(data, meta)
    except S4Error as e:
        return _fail_json(e.http_status, e.code)


@router.get("/alerts")
def alerts(
    category: Optional[str] = Query(default=None),
    page: Optional[str] = Query(default=None),
    page_size: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(get_db),
) -> JSONResponse:
    try:
        _require_s4_token(authorization)
        data, meta = query_alerts(db, category, page, page_size)
        return _ok_json(data, meta)
    except S4Error as e:
        return _fail_json(e.http_status, e.code)


@router.get("/alerts/{alert_key}")
def alert_detail(
    alert_key: str,
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(get_db),
) -> JSONResponse:
    try:
        _require_s4_token(authorization)
        data, meta = query_alert_detail(db, alert_key)
        return _ok_json(data, meta)
    except S4Error as e:
        return _fail_json(e.http_status, e.code)


@router.get("/alerts/{alert_key}/related")
def alert_related(
    alert_key: str,
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(get_db),
) -> JSONResponse:
    try:
        _require_s4_token(authorization)
        data, meta = query_alert_related(db, alert_key)
        return _ok_json(data, meta)
    except S4Error as e:
        return _fail_json(e.http_status, e.code)


@router.get("/environment/readings")
def environment_readings(
    sensor_id: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    page: Optional[str] = Query(default=None),
    page_size: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(get_db),
) -> JSONResponse:
    try:
        _require_s4_token(authorization)
        data, meta = query_readings(db, sensor_id, start, end, page, page_size)
        return _ok_json(data, meta)
    except S4Error as e:
        return _fail_json(e.http_status, e.code)
