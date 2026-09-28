"""
src/tools/channels.py — 通信渠道抽象 (P0-17)
============================================
统一 Channel 接口，供 CommunicationTool 选择不同发送后端：

    Channel
      ↑
      ├── MockChannel      (本地日志, 无网络, 单测/离线用)
      ├── WeComChannel     (企业微信群机器人 Webhook, 真实发送)
      └── DingTalkChannel  (钉钉机器人 Webhook, 真实发送)

设计约束:
  - webhook 地址绝不硬编码; 由构造参数 / 环境变量提供;
  - 发送结果统一 dict {ok, channel, msgid, ..., transient}:
        ok=True       发送成功
        ok=False      失败; transient=True 表示可重试(网络/HTTP/errcode!=0),
                       transient=False 表示配置/参数错误(不可重试)
  - 不引入 requests SDK 之外的依赖; transport 可注入(Fake 测试)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Channel(ABC):
    """通信渠道抽象基类。"""

    name: str = "channel"

    @abstractmethod
    def verify(self) -> bool:
        """渠道配置是否完备(可发送)。"""

    @abstractmethod
    def send_markdown(self, content: str, **meta: Any) -> Dict[str, Any]:
        """发送 markdown 消息。"""

    @abstractmethod
    def send_image(self, image_path: str, **meta: Any) -> Dict[str, Any]:
        """发送图片(base64)。"""


class MockChannel(Channel):
    """本地日志渠道: 不发送任何网络请求, 仅落盘记录, 供单测/离线回归。"""

    name = "LOCAL_MOCK"

    def __init__(self, log_path: str = "data/communication_log.jsonl"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def verify(self) -> bool:
        return True

    def _record(self, kind: str, data: Dict[str, Any]) -> None:
        rec = {
            "timestamp": _now(),
            "channel": self.name,
            "kind": kind,
            "status": "SENT",
            "data": data,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def send_markdown(self, content: str, **meta: Any) -> Dict[str, Any]:
        self._record("markdown", {"content": content, **meta})
        return {"ok": True, "channel": self.name,
                "msgid": uuid.uuid4().hex[:16], "transient": False}

    def send_image(self, image_path: str, **meta: Any) -> Dict[str, Any]:
        self._record("image", {"image_path": str(image_path), **meta})
        return {"ok": True, "channel": self.name,
                "msgid": uuid.uuid4().hex[:16], "transient": False}


class WeComChannel(Channel):
    """企业微信群机器人 Webhook 渠道 (真实发送)。

    webhook_url 由构造参数或环境 WECOM_WEBHOOK_URL 提供; 禁止硬编码。
    transport 可注入(签名同 requests.post): url, json=, headers=, timeout=。
    """

    name = "WECOM"
    SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        transport: Optional[Callable[..., Any]] = None,
        timeout: float = 10.0,
    ):
        self.webhook_url = webhook_url
        self._post = transport if transport is not None else self._default_post
        self.timeout = timeout

    @staticmethod
    def _default_post(url: str, **kwargs: Any) -> Any:
        import requests
        return requests.post(url, **kwargs)

    def verify(self) -> bool:
        return bool(self.webhook_url and str(self.webhook_url).strip())

    def _call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST Webhook 并校验 HTTP + errcode。"""
        if not self.verify():
            return {"ok": False, "transient": False,
                    "error": "WECOM_WEBHOOK_URL 未配置", "channel": self.name}
        try:
            resp = self._post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except Exception as exc:  # network/timeout 等 → 可重试
            return {"ok": False, "transient": True,
                    "error": f"network error: {exc}", "channel": self.name}

        http = getattr(resp, "status_code", None)
        try:
            body = resp.json()
        except Exception:
            body = {}

        if http == 200 and isinstance(body, dict) and body.get("errcode") == 0:
            msgid = body.get("msgid") or uuid.uuid4().hex[:16]
            return {"ok": True, "channel": self.name, "msgid": msgid,
                    "transient": False}

        errmsg = body.get("errmsg") if isinstance(body, dict) else "non-json response"
        return {"ok": False, "transient": True, "http": http,
                "errcode": body.get("errcode") if isinstance(body, dict) else None,
                "error": errmsg or f"http {http}", "channel": self.name}

    def send_markdown(self, content: str, **meta: Any) -> Dict[str, Any]:
        return self._call({"msgtype": "markdown", "markdown": {"content": content}})

    def send_image(self, image_path: str, **meta: Any) -> Dict[str, Any]:
        try:
            data = open(image_path, "rb").read()
        except Exception as exc:
            return {"ok": False, "transient": False, "channel": self.name,
                    "error": f"image read failed: {exc}"}
        b64 = base64.b64encode(data).decode("ascii")
        md5 = hashlib.md5(data).hexdigest()
        return self._call({"msgtype": "image", "image": {"base64": b64, "md5": md5}})


class DingTalkChannel(Channel):
    """钉钉机器人 Webhook 渠道 (真实发送)。

    webhook_url 由构造参数或环境 DINGTALK_WEBHOOK 提供; 禁止硬编码。
    transport 可注入(签名同 requests.post): url, json=, headers=, timeout=。

    钉钉安全须知: markdown 内容首行需包含安全关键词(本项目 "灾害报警"),
    否则钉钉拒绝发送(返回 errcode 310000)。本渠道不自动注入关键词,
    由调用方(CommunicationTool)构造含关键词的 markdown; 仅在此保有兜底:
    检测首行无关键词时自动前缀一行, 提高真实发送成功概率。
    """

    name = "DINGTALK"
    _KEYWORD = "灾害报警"

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        transport: Optional[Callable[..., Any]] = None,
        timeout: float = 10.0,
    ):
        self.webhook_url = webhook_url
        self._post = transport if transport is not None else self._default_post
        self.timeout = timeout

    @staticmethod
    def _default_post(url: str, **kwargs: Any) -> Any:
        import requests
        return requests.post(url, **kwargs)

    def verify(self) -> bool:
        return bool(self.webhook_url and str(self.webhook_url).strip())

    def _call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST 钉钉 robot webhook 并校验 HTTP + errcode。"""
        if not self.verify():
            return {"ok": False, "transient": False,
                    "error": "DINGTALK_WEBHOOK 未配置", "channel": self.name}
        try:
            resp = self._post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except Exception as exc:  # network/timeout 等 → 可重试
            return {"ok": False, "transient": True,
                    "error": f"network error: {exc}", "channel": self.name}

        http = getattr(resp, "status_code", None)
        try:
            body = resp.json()
        except Exception:
            body = {}
        ok = http == 200 and isinstance(body, dict) and body.get("errcode") == 0
        if ok:
            return {"ok": True, "channel": self.name, "transient": False}

        # 钉钉失败体: {"errcode": 310000, "errmsg": "..."}; transient=True 由调用方重试
        errcode = body.get("errcode") if isinstance(body, dict) else None
        errmsg = body.get("errmsg") if isinstance(body, dict) else "non-json response"
        return {"ok": False, "transient": True, "http": http,
                "errcode": errcode, "error": errmsg or f"http {http}",
                "channel": self.name}

    def send_markdown(self, content: str, **meta: Any) -> Dict[str, Any]:
        # 钉钉安全: 内容首行需含安全关键词, 缺则兜底前缀(保证真实发送不因关键词被拒)
        if self._KEYWORD not in content.splitlines()[0] if content else False:
            content = f"## {self._KEYWORD}\n\n{content}"
        return self._call({"msgtype": "markdown",
                           "markdown": {"title": self._KEYWORD, "text": content}})

    def send_image(self, image_url: str, **meta: Any) -> Dict[str, Any]:
        """发送图片 (钉钉 image 消息要求 picURL)。

        参数: image_url (须为可公开访问的 http(s) URL, 通常由
              StorageProvider.upload(snapshot_path) 产生)。
        不再使用 base64(钉钉 webhook 图片消息仅支持 picURL)。
        """
        if not image_url or not str(image_url).startswith(("http://", "https://")):
            return {"ok": False, "transient": False, "channel": self.name,
                    "error": f"invalid image_url: {image_url}"}
        try:
            payload = {
                "msgtype": "image",
                "image": {
                    "picURL": str(image_url),
                },
            }
            return self._call(payload)
        except Exception as exc:
            return {"ok": False, "transient": False, "channel": self.name,
                    "error": f"send image failed: {exc}"}