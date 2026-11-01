"""微信小程序 code2session 服务（`server/wechat.py`）。

- 封装微信官方 `jscode2session`：https://api.weixin.qq.com/sns/jscode2session
- AppID / AppSecret 仅从服务端环境变量读取（`WX_APPID` / `WX_APP_SECRET`），
  禁止硬编码、禁止进入小程序、日志、数据库或接口响应。
- HTTP 调用可注入（`http_get`），测试用 mock，不访问真实微信。
- 不记录/不透传 AppSecret、session_key、完整原始响应或完整 OpenID。
- 网络失败、超时、微信 errcode、缺失 openid 均转为受控异常 (WechatAuthError)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

import requests

CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
DEFAULT_TIMEOUT_SECONDS = 5.0


class WechatAuthError(Exception):
    """微信 code2session 业务/网络异常（不含敏感信息）。"""

    def __init__(self, reason: str, *, errcode: Optional[int] = None):
        super().__init__(reason)
        self.reason = reason
        self.errcode = errcode


def _default_http_get(url: str, params: dict, timeout: float):
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return {"status": resp.status_code, "json": resp.json()}
    except requests.exceptions.Timeout as exc:
        raise WechatAuthError("wechat request timeout") from exc
    except (requests.exceptions.RequestException, ValueError) as exc:
        # ValueError: JSON 解析失败; RequestException: 网络/HTTP 错误
        raise WechatAuthError("wechat network error") from exc


@dataclass(frozen=True)
class WechatCode2SessionResult:
    """code2session 的受控结果（仅保留 OpenID 与 session_key 的脱敏使用）。"""

    openid: str
    session_key: str  # 本地使用，禁止写入日志/数据库/响应


class WechatClient:
    """微信 code2session 客户端。

    依赖注入 `http_get`（可测试替换），默认用 requests。
    """

    def __init__(
        self,
        appid: Optional[str] = None,
        appsecret: Optional[str] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_get: Optional[Callable[[str, dict, float], dict]] = None,
    ):
        self.appid = appid if appid is not None else os.environ.get("WX_APPID", "")
        self.appsecret = appsecret if appsecret is not None else os.environ.get("WX_APP_SECRET", "")
        self.timeout = timeout
        self._http_get = http_get or _default_http_get

    def code2session(self, code: str) -> WechatCode2SessionResult:
        """用临时登录凭证 code 换取 OpenID / session_key。

        Raises:
            WechatAuthError: 配置缺失 / code 非法 / 微信 errcode / 网络异常 / 缺 openid。
        """
        if not self.appid or not self.appsecret:
            raise WechatAuthError("wechat auth not configured", errcode=-1)
        if not code or not code.strip():
            raise WechatAuthError("invalid code", errcode=-2)

        params = {
            "appid": self.appid,
            "secret": self.appsecret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
        res = self._http_get(CODE2SESSION_URL, params, self.timeout)
        data = res.get("json") if isinstance(res, dict) else None
        if not isinstance(data, dict):
            raise WechatAuthError("wechat invalid response")

        if data.get("errcode"):
            raise WechatAuthError("wechat errcode", errcode=int(data["errcode"]))

        openid = str(data.get("openid") or "").strip()
        session_key = str(data.get("session_key") or "").strip()
        if not openid:
            raise WechatAuthError("wechat missing openid")
        if not session_key:
            raise WechatAuthError("wechat missing session_key")

        return WechatCode2SessionResult(openid=openid, session_key=session_key)