"""V4.4 媒体访问授权路由（D-0 契约）— 独立于 server/api.py 的媒体模块。

解耦目的：B 线干净部署不含微信登录模块(auth/wechat)时，server/api.py 仍可独立启动；
媒体路由集中在本模块，仅当 auth 依赖存在时由 api.py 保护式挂载。

- 认证：复用微信会话依赖 get_current_user（server.auth，不改登录模块）。
- AuthError：本模块提供处理函数，由 server/api.py 在挂载时经 app.add_exception_handler 注册。
- 接口 A: GET  /alerts/{alert_key}/media   （告警关联媒体元数据列表）
- 接口 B: POST /media/{media_id}/access    （签发短期 GET 签名 URL）
- 复用 server.s4 的 _parse_alert_key/_paging/_ok_json/get_db 及 S4Error 语义，不修改 s4.py/DAO/契约。

安全约束：媒体访问必须经 get_current_user 鉴权；绝不提供匿名/公开媒体接口。
微信登录 router 不在此自动挂载（由未来集成任务决定）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from server import s4 as s4_module
from server.auth import AuthError, get_current_user
from src.storage import StorageSignError, create_storage

router = APIRouter(prefix="/api/v1")

# 需补充 message 的错误码（不在 s4._CODES 内的媒体专属码）
_MEDIA_CODE_MESSAGE = {
    "CONFLICT": "媒体尚未上传完成",
    "SERVICE_UNAVAILABLE": "媒体存储服务暂不可用",
    "INTERNAL_ERROR": "服务端内部错误",
}


def _media_fail_json(http_status: int, code: str) -> JSONResponse:
    """媒体接口统一错误信封（四字段，不泄露内部细节）。"""
    message = _MEDIA_CODE_MESSAGE.get(code) or s4_module._CODES.get(code, "服务端内部错误")
    return JSONResponse(
        status_code=http_status,
        content={"code": code, "message": message, "data": None, "meta": {}},
    )


async def _on_auth_error(request: Request, exc: AuthError) -> JSONResponse:
    """未登录/会话过期/撤销 → 401（复用四字段信封；不改 auth.py）。"""
    return _media_fail_json(exc.http_status, exc.code)


@router.get("/alerts/{alert_key}/media")
def list_alert_media(
    alert_key: str,
    page: Optional[str] = Query(default=None),
    page_size: Optional[str] = Query(default=None),
    db: Any = Depends(s4_module.get_db),
    _user: dict = Depends(get_current_user),
) -> JSONResponse:
    """接口 A: 查询告警关联媒体元数据（D0-01：有效登录用户可查全部告警媒体，无归属隔离）。

    person 告警先验证事件存在（无媒体→空列表）；sensor 告警先验证告警存在再查媒体。
    只返回元数据，绝不返回 object_key/local_path/Bucket/URL。
    """
    try:
        source, raw = s4_module._parse_alert_key(alert_key)
        p, ps = s4_module._paging(page, page_size)
        if source == "person":
            if db.get_event(raw) is None:
                return _media_fail_json(404, "NOT_FOUND")
            records = []
        else:
            if db.get_sensor_alert(raw) is None:
                return _media_fail_json(404, "NOT_FOUND")
            records = db.list_media_evidence_by_ref("sensor", raw)
        total = len(records)
        start = (p - 1) * ps
        page_items = records[start:start + ps]
        data = [
            {
                "media_id": m["media_id"],
                "media_type": m["media_type"],
                "status": m["status"],
                "upload_status": m["upload_status"],
                "captured_at": m["captured_at"],
                "uploaded_at": m["uploaded_at"],
            }
            for m in page_items
        ]
        meta = {"page": p, "page_size": ps, "total": total, "has_more": p * ps < total}
        return s4_module._ok_json(data, meta)
    except s4_module.S4Error as e:
        return _media_fail_json(e.http_status, e.code)
    except HTTPException:
        raise
    except Exception:
        return _media_fail_json(500, "INTERNAL_ERROR")


@router.post("/media/{media_id}/access")
def get_media_access(
    media_id: str,
    db: Any = Depends(s4_module.get_db),
    _user: dict = Depends(get_current_user),
) -> JSONResponse:
    """接口 B: 为已上传成功的媒体签发短期 GET 签名 URL。

    仅当 status=='success' 且 upload_status=='success' 才签发；否则 409。
    服务端内部读 object_key，不接受客户端传 key/Bucket/Endpoint/URL；签后不回写数据库。
    StorageSignError→503；ValueError→500。
    """
    try:
        rec = db.get_media_evidence(media_id)
        if rec is None:
            return _media_fail_json(404, "NOT_FOUND")
        if rec.get("status") != "success" or rec.get("upload_status") != "success":
            return _media_fail_json(409, "CONFLICT")
        object_key = rec.get("object_key")
        if not object_key:
            # 数据不完整/内部状态错误：不调用 OSS
            return _media_fail_json(500, "INTERNAL_ERROR")
        ttl = 300
        storage = create_storage()
        access_url = storage.sign_get_url(object_key, ttl=ttl)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(
            timespec="seconds")
        return s4_module._ok_json({"access_url": access_url, "expires_at": expires_at}, {})
    except s4_module.S4Error as e:
        return _media_fail_json(e.http_status, e.code)
    except StorageSignError:
        # 含 EvidenceBucketNotConfiguredError（子类），不重复捕获
        return _media_fail_json(503, "SERVICE_UNAVAILABLE")
    except ValueError:
        return _media_fail_json(500, "INTERNAL_ERROR")
    except HTTPException:
        raise
    except Exception:
        return _media_fail_json(500, "INTERNAL_ERROR")


def install(app) -> None:
    """供 server/api.py 挂载时注册 AuthError 全局异常处理器（保持四字段 401 信封）。"""
    app.add_exception_handler(AuthError, _on_auth_error)