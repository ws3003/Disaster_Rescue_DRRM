"""V4.4 微信 Session 用户态查询接口（拟议 `/api/v1/user/*`，WP-2.2）。

- 认证：复用 `server.auth.get_current_user`（微信 Session），绝不匿名为作服务。
- 业务：复用 `server.s4` 的共享查询函数 `query_*`（WP-2.1 抽取），不复制第二套查询/分页/组装逻辑。
- 与原 S4 接口共用同一 `get_db`；响应信封/错误码与原 S4 一致（`_ok_json`/`_fail_json`/`S4Error`）。
- 本模块不持 S4_API_TOKEN / C4_TOKEN / OSS 密钥；小程序仅持自己的微信 Session Token。
- 单租户共享：各用户共享业务数据，不作 user_id 数据隔离。

设计说明：本模块不做认证后又复用 S4 业务函数；`get_current_user` 作为 FastAPI 依赖注入，
`query_*` 为纯业务函数（输入 db+参数，输出 (data, meta)），二者在 handler 中组合。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse

from server import s4 as s4_module
from server.auth import get_current_user
from server.s4 import get_db

router = APIRouter(prefix="/api/v1/user")


def _user_alerts_handler(
    _user: Dict[str, Any],
    db: Any,
    category: Optional[str],
    page: Optional[str],
    page_size: Optional[str],
) -> JSONResponse:
    try:
        data, meta = s4_module.query_alerts(db, category, page, page_size)
        return s4_module._ok_json(data, meta)
    except s4_module.S4Error as e:
        return s4_module._fail_json(e.http_status, e.code)


@router.get("/system/status")
def user_system_status(
    _user: dict = Depends(get_current_user),
    db: Any = Depends(get_db),
) -> JSONResponse:
    try:
        data, meta = s4_module.query_system_status(db)
        return s4_module._ok_json(data, meta)
    except s4_module.S4Error as e:
        return s4_module._fail_json(e.http_status, e.code)


@router.get("/alerts")
def user_alerts(
    category: Optional[str] = Query(default=None),
    page: Optional[str] = Query(default=None),
    page_size: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),
    db: Any = Depends(get_db),
) -> JSONResponse:
    return _user_alerts_handler(_user, db, category, page, page_size)


@router.get("/alerts/{alert_key}")
def user_alert_detail(
    alert_key: str,
    _user: dict = Depends(get_current_user),
    db: Any = Depends(get_db),
) -> JSONResponse:
    try:
        data, meta = s4_module.query_alert_detail(db, alert_key)
        return s4_module._ok_json(data, meta)
    except s4_module.S4Error as e:
        return s4_module._fail_json(e.http_status, e.code)


@router.get("/alerts/{alert_key}/related")
def user_alert_related(
    alert_key: str,
    _user: dict = Depends(get_current_user),
    db: Any = Depends(get_db),
) -> JSONResponse:
    try:
        data, meta = s4_module.query_alert_related(db, alert_key)
        return s4_module._ok_json(data, meta)
    except s4_module.S4Error as e:
        return s4_module._fail_json(e.http_status, e.code)


@router.get("/environment/readings")
def user_environment_readings(
    sensor_id: Optional[str] = Query(default=None),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    page: Optional[str] = Query(default=None),
    page_size: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),
    db: Any = Depends(get_db),
) -> JSONResponse:
    try:
        data, meta = s4_module.query_readings(
            db, sensor_id, start, end, page, page_size)
        return s4_module._ok_json(data, meta)
    except s4_module.S4Error as e:
        return s4_module._fail_json(e.http_status, e.code)