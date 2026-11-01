"""V4.4 微信用户认证 Router（`server/auth.py`）。

接口:
- POST /api/v1/auth/login   : 微信 code2session → 内部用户 + Session（24h），返回一次明文 Token
- GET  /api/v1/auth/me      : Bearer Session Token → 当前用户非敏感信息
- POST /api/v1/auth/logout  : 撤销当前 Session（幂等）

Session 安全:
- Token 用 `secrets.token_urlsafe(32)`; 数据库只存 SHA-256 哈希
- 有效期 86400 秒; 撤销/过期/缺失 Token → 401 UNAUTHORIZED
- 与 S4 共享服务 Token（`_require_s4_token`）完全独立，不改变 S4 语义
- 日志/响应不含明文 Token、哈希、AppSecret、session_key

响应统一沿用项目风格 `{code, message, data, meta}`（code 字符串）。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse

from src.database.db import Database as _Db  # noqa: F401  (仅类型提示)

from server.s4 import get_db
from server.wechat import WechatAuthError, WechatClient

router = APIRouter(prefix="/api/v1")

SESSION_TTL_SECONDS = 86400   # 24 小时


# ----------------------------------------------------------------------
# 认证专属错误码（不改 S4._CODES；格式与项目统一 {code,message,data,meta}）
# ----------------------------------------------------------------------
_AUTH_CODES = {
    "INVALID_PARAMETER": "参数非法",
    "UNAUTHORIZED": "Unauthorized",
    "FORBIDDEN": "权限不足",
    "NOT_FOUND": "资源不存在",
    "WECHAT_ERROR": "微信服务异常",
    "INTERNAL_ERROR": "服务端内部错误",
}


class AuthError(Exception):
    """认证业务错误：函数内联转成四字段信封。"""

    def __init__(self, http_status: int, code: str) -> None:
        super().__init__(code)
        self.http_status = http_status
        self.code = code


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
            "message": _AUTH_CODES.get(code, _AUTH_CODES["INTERNAL_ERROR"]),
            "data": None,
            "meta": {},
        },
    )


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# 用户身份认证依赖（独立于 S4 服务 Token）
# ----------------------------------------------------------------------
def get_current_user(
    authorization: Optional[str] = Header(default=None),
    db: Any = Depends(get_db),
):
    """从 Bearer Session Token 解析当前用户；无效/缺/过期/撤销 → 401。

    不记录明文 Token；不改变 S4 `_require_s4_token` 语义。
    """
    _require_session(authorization)
    token = authorization.split(" ", 1)[1].strip()
    session = db.get_session_by_hash(_sha256(token))
    if session is None:
        raise AuthError(401, "UNAUTHORIZED")
    now_str = _now_iso()

    # 已撤销
    if session.get("revoked_at"):
        raise AuthError(401, "UNAUTHORIZED")
    # 过期
    if session.get("expires_at") and session["expires_at"] <= now_str:
        raise AuthError(401, "UNAUTHORIZED")

    user = db.get_user_by_id(session["user_id"])
    if user is None:
        raise AuthError(401, "UNAUTHORIZED")
    return user


def _require_session(authorization: Optional[str]) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError(401, "UNAUTHORIZED")


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------
@router.post("/auth/login")
def login(
    payload: Dict[str, Any] = Body(default={}),
    db: Any = Depends(get_db),
):
    """微信登录：code → openid → 内部用户 → 新建 Session(24h) → 返回一次明文 Token。"""
    try:
        code = str((payload or {}).get("code") or "").strip()
        if not code:
            raise AuthError(400, "INVALID_PARAMETER")

        wechat = WechatClient(http_get=_default_wechat_http())
        result = wechat.code2session(code)
        openid = result.openid

        user_id = db.create_user(openid)                    # 幂等: 一 openid 一内部用户
        token = secrets.token_urlsafe(32)                   # 明文，仅本次返回一次
        expires_dt = datetime.now().astimezone() + timedelta(seconds=SESSION_TTL_SECONDS)
        expires_at = expires_dt.isoformat(timespec="seconds")
        db.create_session(user_id, _sha256(token), expires_at=expires_at)

        return _ok_json(
            {
                "session_token": token,
                "expires_in": SESSION_TTL_SECONDS,
                "user_id": user_id,
            }
        )
    except AuthError as e:
        return _fail_json(e.http_status, e.code)
    except WechatAuthError as e:
        # 微信服务异常统一 502，不透传微信原始错误/密钥/完整响应
        return _fail_json(502, "WECHAT_ERROR")
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("x4.auth").warning("auth/login internal: %s", type(exc).__name__)
        return _fail_json(500, "INTERNAL_ERROR")


# ----------------------------------------------------------------------
# 当前用户
# ----------------------------------------------------------------------
@router.get("/auth/me")
def me(
    authorization: Optional[str] = Header(default=None),
    db: Any = Depends(get_db),
):
    """返回当前登录用户非敏感信息；无效/缺/过期/撤销 Token → 401 UNAUTHORIZED。"""
    try:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError(401, "UNAUTHORIZED")
        token = authorization.split(" ", 1)[1].strip()
        session = db.get_session_by_hash(_sha256(token))
        now_str = _now_iso()
        if session is None or session.get("revoked_at") \
                or (session.get("expires_at") and session["expires_at"] <= now_str):
            raise AuthError(401, "UNAUTHORIZED")
        user = db.get_user_by_id(session["user_id"])
        if user is None:
            raise AuthError(401, "UNAUTHORIZED")
        return _ok_json({"user_id": user["user_id"]})
    except AuthError as e:
        return _fail_json(e.http_status, e.code)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("x4.auth").warning("auth/me internal: %s", type(exc).__name__)
        return _fail_json(500, "INTERNAL_ERROR")


# ----------------------------------------------------------------------
# 登出（只撤销当前 Session，幂等，不影响同用户其他 Session）
# ----------------------------------------------------------------------
@router.post("/auth/logout")
def logout(
    authorization: Optional[str] = Header(default=None),
    db: Any = Depends(get_db),
):
    try:
        _require_session(authorization)
        token = authorization.split(" ", 1)[1].strip()
        session = db.get_session_by_hash(_sha256(token))
        if session is None:
            # 未知 Token：视为已登出（幂等，401 或无操作）
            return _fail_json(401, "UNAUTHORIZED")
        db.revoke_session(session["session_id"])
        return _ok_json({"revoked": session["session_id"]})
    except AuthError as e:
        return _fail_json(e.http_status, e.code)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger("x4.auth").warning("auth/logout internal: %s", type(exc).__name__)
        return _fail_json(500, "INTERNAL_ERROR")


# ----------------------------------------------------------------------
# 微信 HTTP 默认封装（供测试 monkeypatch；生产用 requests）
# ----------------------------------------------------------------------
def _default_wechat_http():
    from server.wechat import _default_http_get
    return _default_http_get