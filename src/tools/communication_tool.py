"""
src/tools/communication_tool.py — 通信工具 (P0-17 真实化)
=========================================================
统一发送救援报警到所选 Channel:

    Planner → Executor → CommunicationTool.run(send_alert) → Channel
          ├── MockChannel      (本地日志, 无网络, 单测/离线)
          ├── WeComChannel     (企业微信 Webhook, 真实发送)
          └── DingTalkChannel  (钉钉机器人 Webhook, 真实发送)

P0-17 新增能力:
  - Channel 抽象 + Mock/WeCom 两种后端;
  - 重试(首次 + 2 次 = 最多 3 次尝试), 指数退避 1s / 4s, 可注入 sleep 供测试;
  - 持久化 pending 队列(data/pending_communication.jsonl), 失败/配置缺失入库;
  - 恢复补发 flush_pending(成功移除, 失败保留);
  - 严禁: 无 webhook 时静默回落 Mock —— 显式 channel=wecom/dingtalk 缺 URL 报错并入队。

渠道显式选择(环境变量, 不硬编码):
  COMMUNICATION_CHANNEL = mock | wecom | dingtalk   (默认 mock, 兼容既有测试)
  WECOM_WEBHOOK_URL     = 企业微信 webhook 地址
  DINGTALK_WEBHOOK      = 钉钉机器人 webhook 地址 (https://oapi.dingtalk.com/robot/send?access_token=...)

run() 契约保持不变: send_alert / get_recent_alerts 继续可用; 新增 flush_pending。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tools.base import Tool  # noqa: E402
from src.tools.channels import (  # noqa: E402
    Channel,
    DingTalkChannel,
    MockChannel,
    WeComChannel,
)


class CommunicationTool(Tool):
    """
    Communication tool for the Disaster Rescue DRRM Agent.

    通过 Channel 发送结构化救援报警, 支持 Mock / WeCom 两种后端,
    带重试(指数退避)与持久化待发队列。run() 契约与既有 Agent 兼容。
    """

    def __init__(
        self,
        log_path: str = "data/communication_log.jsonl",
        pending_path: str = "data/pending_communication.jsonl",
        channel: str = "auto",
        webhook_url: Optional[str] = None,
        transport: Optional[Callable[..., Any]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        max_retries: int = 2,
        backoff: tuple = (1.0, 4.0),
        timeout: float = 10.0,
    ):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path = Path(pending_path)
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)

        if channel == "auto":
            channel = os.getenv("COMMUNICATION_CHANNEL", "mock")
        channel = str(channel or "mock").lower()
        if channel not in ("mock", "wecom", "dingtalk"):
            raise ValueError(
                f"COMMUNICATION_CHANNEL 非法: {channel!r}(mock|wecom|dingtalk)"
            )
        self._channel_name = channel

        # webhook 地址仅来自显式参数或环境变量, 禁止硬编码
        env_key = "WECOM_WEBHOOK_URL" if channel == "wecom" else "DINGTALK_WEBHOOK"
        if webhook_url is not None:
            self._webhook_url = webhook_url or None
        elif channel == "dingtalk":
            self._webhook_url = os.getenv("DINGTALK_WEBHOOK") or None
        else:
            self._webhook_url = os.getenv(env_key) or None

        self._transport = transport
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep
        self.max_retries = int(max_retries)
        self.backoff = tuple(float(b) for b in backoff)
        self.timeout = float(timeout)

    # ------------------------------------------------------------------
    # Channel 选择
    # ------------------------------------------------------------------
    def _channel(self) -> Channel:
        if self._channel_name == "wecom":
            return WeComChannel(webhook_url=self._webhook_url,
                                transport=self._transport,
                                timeout=self.timeout)
        if self._channel_name == "dingtalk":
            return DingTalkChannel(webhook_url=self._webhook_url,
                                   transport=self._transport,
                                   timeout=self.timeout)
        return MockChannel(log_path=str(self.log_path))

    # ------------------------------------------------------------------
    # Tool 契约
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "communication_tool"

    @property
    def description(self) -> str:
        return ("Create and send structured disaster rescue alerts. "
                "Channels: MockChannel/WeComChannel/DingTalkChannel. "
                "Actions: send_alert / flush_pending / get_recent_alerts.")

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send_alert", "flush_pending", "get_recent_alerts"],
                },
                "severity": {"type": "string"},
                "title": {"type": "string"},
                "message": {"type": "string"},
                "person_id": {"type": ["integer", "string"]},
                "event_id": {"type": "string"},
                "task_id": {"type": "string"},
                "image_path": {"type": "string"},
                "extra": {"type": "object"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["action"],
            "additionalProperties": True,
        }

    def run(self, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        if not action:
            return {"ok": False, "error": "Missing required parameter: action."}
        try:
            if action == "send_alert":
                return self._send_alert(kwargs)
            if action == "flush_pending":
                return self.flush_pending()
            if action == "get_recent_alerts":
                return self._get_recent_alerts(kwargs)
            return {"ok": False, "error": f"Unsupported communication action: {action}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "action": action}

    # ------------------------------------------------------------------
    # 报警消息组装
    # ------------------------------------------------------------------
    def _build_alert(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        severity = str(kwargs.get("severity", "HIGH")).upper()
        title = kwargs.get("title")
        message = kwargs.get("message")
        if not title:
            raise ValueError("Missing required parameter: title.")
        if not message:
            raise ValueError("Missing required parameter: message.")
        extra = kwargs.get("extra", {})
        if not isinstance(extra, dict):
            raise ValueError("Parameter 'extra' must be a dictionary.")
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "alert_type": kwargs.get("alert_type"),
            "environment_severity": kwargs.get("environment_severity"),
            "severity": severity,
            "title": title,
            "message": message,
            "person_id": kwargs.get("person_id"),
            "event_id": kwargs.get("event_id"),
            "task_id": kwargs.get("task_id"),
            "image_path": kwargs.get("image_path"),
            "priority": kwargs.get("priority"),
            "person_priority": kwargs.get("person_priority"),
            "risk_score": kwargs.get("risk_score"),
            "state": kwargs.get("state"),
            "extra": extra,
        }

    @staticmethod
    def _build_markdown(alert: dict[str, Any]) -> str:
        lines = [f"## {alert['title']}"]
        if alert.get("person_id") is not None:
            lines.append(f"**人员**: {alert['person_id']}")
        lines.append(f"**消息**: {alert['message']}")
        if alert.get("event_id"):
            lines.append(f"**事件**: {alert['event_id']}")
        if alert.get("task_id"):
            lines.append(f"**task**: {alert['task_id']}")
        return "\n".join(lines)

    @staticmethod
    def _build_dingtalk_markdown(alert: dict[str, Any]) -> str:
        """钉钉 markdown 报警模板(按 alert_type 分流)。

        首行必须含"灾害报警/环境报警"(钉钉安全关键词, 否则 errcode 310000 拒绝)。
        Person 报警: 仅人员信息(人员ID/状态/人员风险等级/风险分数/时间/来源), 禁环境字段。
        Environment 报警: 仅环境信息(环境危险等级/message/时间/来源), 禁人员字段。
        """
        ts = alert.get("timestamp") or datetime.now().isoformat(timespec="seconds")
        alert_type = str(alert.get("alert_type") or "").lower()

        if alert_type == "environment":
            env_sev = str(alert.get("environment_severity")
                          or alert.get("severity") or "HIGH")
            return "\n".join([
                "## 🚨 环境高风险报警",
                "",
                "环境危险等级:",
                env_sev,
                "",
                str(alert.get("message") or ""),
                "",
                "时间:",
                ts,
                "",
                "来源:",
                "Raspberry Pi Edge AI",
            ])

        # person 报警(默认走人员信息)
        extra = alert.get("extra") or {}
        person = alert.get("person_id")
        state = alert.get("state") or extra.get("state") or "-"
        priority = alert.get("person_priority") or alert.get("priority") \
            or extra.get("priority") or "-"
        risk = extra.get("risk_score") if extra.get("risk_score") is not None else (
            alert.get("risk_score") if alert.get("risk_score") is not None else "-"
        )
        return "\n".join([
            "## 🚨 灾害报警",
            "",
            "人员ID:",
            str(person if person is not None else "-"),
            "",
            "状态:",
            str(state),
            "",
            "人员风险等级:",
            str(priority),
            "",
            "风险分数:",
            str(risk),
            "",
            "时间:",
            ts,
            "",
            "来源:",
            "Raspberry Pi Edge AI",
        ])

    # ------------------------------------------------------------------
    # 发送(带重试 + 指数退避)
    # ------------------------------------------------------------------
    def _send_with_retry(self, channel: Channel, kind: str,
                         payload: Any, meta: dict) -> dict[str, Any]:
        """首次发送 + max_retries 次重试(指数退避); 仅 transient 错误重试。"""
        attempts = self.max_retries + 1
        last: dict[str, Any] = {"error": "unknown"}
        for attempt in range(attempts):
            last = channel.send_markdown(payload, **meta) if kind == "markdown" \
                else channel.send_image(payload, **meta)
            if last.get("ok"):
                return last
            # 配置/参数错误 → 不重试; 网络/HTTP/errcode!=0 → 重试
            if not last.get("transient", True):
                break
            if attempt < attempts - 1:
                delay = self.backoff[attempt] if attempt < len(self.backoff) \
                    else self.backoff[-1]
                self._sleep(delay)
        last = dict(last)
        last.setdefault("ok", False)
        return last

    # ------------------------------------------------------------------
    # Pending 队列(持久化)
    # ------------------------------------------------------------------
    def _write_pending(self, kind: str, payload: Any, meta: dict,
                       channel_name: str, error: str) -> dict[str, Any]:
        entry = {
            "msg_id": uuid.uuid4().hex,
            "type": kind,
            "payload": payload,
            "meta": meta,
            "channel": channel_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": "pending",
            "last_error": str(error),
        }
        with self.pending_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _read_pending(self) -> List[dict]:
        if not self.pending_path.exists():
            return []
        out = []
        for line in self.pending_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("status") == "pending":
                out.append(entry)
        return out

    def _rewrite_pending(self, entries: List[dict]) -> None:
        with self.pending_path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def flush_pending(self) -> dict[str, Any]:
        """补发待发队列: 成功移除, 失败保留 pending。"""
        entries = self._read_pending()
        kept: List[dict] = []
        flushed = 0
        for e in entries:
            if e.get("channel") == "WECOM":
                channel: Channel = WeComChannel(webhook_url=self._webhook_url,
                                                transport=self._transport)
            elif e.get("channel") == "DINGTALK":
                channel = DingTalkChannel(webhook_url=self._webhook_url,
                                          transport=self._transport)
            else:
                channel = MockChannel(log_path=str(self.log_path))
            res = self._send_with_retry(channel, e.get("type", "markdown"),
                                        e.get("payload"),
                                        e.get("meta", {}) or {})
            if res.get("ok"):
                flushed += 1
            else:
                e["last_error"] = res.get("error")
                kept.append(e)
        self._rewrite_pending(kept)
        return {"ok": True, "action": "flush_pending",
                "flushed": flushed, "remaining": len(kept)}

    # ------------------------------------------------------------------
    # send_alert
    # ------------------------------------------------------------------
    def _resolve_public_image(self, img_path: str) -> str:
        """把 image_path 解析为「公网 HTTPS OSS URL」(钉钉可访问)。

        - 本地文件(data/snapshots/xxx.jpg): 上传 OSS → 公开 URL
        - 私网/非HTTPS URL(localhost/192.168.x/127.0.0.1): 映射到本地 snap 文件再上传 OSS
        - 已是公网 HTTPS(如 OSS): 原样返回
        禁止把私网 URL 发给钉钉。返回 "" 表示无法得到公网 URL。
        """
        if not img_path:
            return ""
        s = str(img_path).strip()
        if s.startswith(("http://", "https://")):
            if self._is_public_https(s):
                return s
            local = self._url_to_local_snap(s)
            if not local:
                # 私网 URL 对应本地文件不存在 → 尝试下载到本地再传 OSS(V4.7)
                local = self._download_snap_image(s)
            if not local:
                print("[IMAGE] 私网 URL 无法映射/下载到本地, path=", s)
                return ""
            return self._upload_snap_image(local)
        # 本地文件路径
        if os.path.exists(s):
            return self._upload_snap_image(s)
        # 相对路径 → 相对仓库根
        cand = str(PROJECT_ROOT / s)
        if os.path.exists(cand):
            return self._upload_snap_image(cand)
        print("[IMAGE] 本地文件不存在, path=", s)
        return ""

    @staticmethod
    def _is_public_https(url: str) -> bool:
        """是否为公网 HTTPS 且非私网主机(含 OSS URL)。"""
        if not url.lower().startswith("https://"):
            return False
        host = url.lower().split("://", 1)[1].split("/", 1)[0]
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False
        if host.startswith("192.168.") or host.startswith("10.") \
                or host.startswith("172."):
            return False
        return True

    @classmethod
    def _url_to_local_snap(cls, url: str) -> str:
        """把 server /snapshots/<file> URL 映射到本地 snap 文件路径; 无法映射返回 "". """
        if "/snapshots/" not in url:
            return ""
        filename = url.rsplit("/snapshots/", 1)[1].split("?", 1)[0]
        if not filename:
            return ""
        local = Path(PROJECT_ROOT) / "data" / "snapshots" / filename
        return str(local) if local.is_file() else ""

    @classmethod
    def _download_snap_image(cls, url: str) -> str:
        """下载私网 /snapshots/ 图片到本地 data/snapshots/(仅供随后 OSS 上传); 失败返回 ""。

        filename 取 basename 防路径穿越; 下载失败/坏图/空一律回退 ""。
        """
        if "/snapshots/" not in url:
            return ""
        filename = Path(url.rsplit("/snapshots/", 1)[1].split("?", 1)[0]).name
        if not filename:
            return ""
        snap_dir = Path(PROJECT_ROOT) / "data" / "snapshots"
        try:
            import requests  # 惰性加载

            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            snap_dir.mkdir(parents=True, exist_ok=True)
            dest = snap_dir / filename
            dest.write_bytes(resp.content)
            print(f"[IMAGE] downloaded private url -> {dest}")
            if dest.is_file() and dest.stat().st_size > 0:
                return str(dest)
            return ""
        except Exception as exc:  # noqa: BLE001
            print(f"[IMAGE] download failed ({url}): {exc}")
            return ""

    @staticmethod
    def _upload_snap_image(local_path: str) -> str:
        """上传本地图片到 OSS, 返回公网 HTTPS URL; 失败返回 "". """
        from src.storage import upload_snapshot
        print("[IMAGE] local_path=", local_path)
        up = upload_snapshot(local_path)
        if up.get("ok") and up.get("url"):
            print("[OSS] uploaded_url=", up["url"])
            return up["url"]
        print("[OSS] upload failed:", up.get("error"))
        return ""

    def _send_alert(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        alert = self._build_alert(kwargs)
        channel = self._channel()
        is_dingtalk = self._channel_name == "dingtalk"
        content = (self._build_dingtalk_markdown(alert) if is_dingtalk
                   else self._build_markdown(alert))

        # 显式 wecom/dingtalk 但缺 URL → 明确失败并入队(绝不伪装成功/静默降级 Mock)
        if self._channel_name in ("wecom", "dingtalk") and not channel.verify():
            err_key = ("DINGTALK_WEBHOOK" if is_dingtalk else "WECOM_WEBHOOK_URL")
            entry = self._write_pending(
                "markdown", content, {"alert": alert},
                channel.name, f"{err_key} missing")
            return {"ok": False, "action": "send_alert", "queued": True,
                    "channel": channel.name, "msg_id": entry["msg_id"],
                    "error": f"{err_key} missing"}

        res = self._send_with_retry(channel, "markdown", content, {"alert": alert})

        if res.get("ok"):
            # 现场图片证据: 统一解析为「公网 HTTPS OSS URL」(钉钉可访问), 不走私网 URL
            img_path = alert.get("image_path")
            img_ok = None
            image_url = None
            image_error = None
            if img_path:
                try:
                    image_url = self._resolve_public_image(img_path)
                    if image_url:
                        print("[DINGTALK] send image url=", image_url)
                        img_res = self._send_with_retry(
                            channel, "image", image_url,
                            {"alert": alert, "source": "oss"},
                        )
                        img_ok = bool(img_res and img_res.get("ok"))
                        if not img_ok:
                            image_error = (img_res or {}).get("error")
                    else:
                        img_ok = False
                        image_error = "no public image url (OSS upload failed or path missing)"
                except Exception as exc:  # noqa: BLE001
                    img_ok = False
                    image_error = f"image resolve exception: {exc}"
                if not img_ok:
                    self._append_log({
                        **alert, "status": "IMAGE_FAILED", "channel": channel.name,
                        "image_path": img_path,
                        "error": image_error,
                    })
            self._append_log({
                **alert, "status": "SENT", "channel": channel.name,
                "msgid": res.get("msgid"),
            })
            return {"ok": True, "action": "send_alert", "channel": channel.name,
                    "msgid": res.get("msgid"), "status": "SENT",
                    "image_sent": img_ok, "image_url": image_url,
                    "image_error": image_error}

        # transient/队列化
        entry = self._write_pending("markdown", content, {"alert": alert},
                                    channel.name, res.get("error"))
        return {"ok": False, "action": "send_alert", "queued": True,
                "channel": channel.name, "msg_id": entry["msg_id"],
                "error": res.get("error"), "attempts": self.max_retries + 1}

    # ------------------------------------------------------------------
    # get_recent_alerts
    # ------------------------------------------------------------------
    def _get_recent_alerts(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        limit = kwargs.get("limit", 10)
        if not isinstance(limit, int) or limit <= 0:
            return {"ok": False, "error": "Parameter 'limit' must be a positive integer."}
        if not self.log_path.exists():
            return {"ok": True, "action": "get_recent_alerts", "count": 0, "alerts": []}
        alerts = []
        for line in reversed(self.log_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(alerts) >= limit:
                break
        return {"ok": True, "action": "get_recent_alerts",
                "count": len(alerts), "alerts": alerts}

    # ------------------------------------------------------------------
    # Local log
    # ------------------------------------------------------------------
    def _append_log(self, alert: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(alert, ensure_ascii=False) + "\n")


# ==============================================================
# Standalone test
# ==============================================================
if __name__ == "__main__":
    print("Testing CommunicationTool...")
    ct = CommunicationTool()  # 默认 mock 渠道(无网络)
    print("channel:", ct._channel_name)
    r1 = ct.run(action="send_alert", severity="HIGH", title="High-Risk Person",
                message="A fallen civilian detected.",
                person_id=1, event_id="E-TEST-001", task_id="T-TEST-001")
    print("send_alert:", r1)
    r2 = ct.run(action="get_recent_alerts", limit=10)
    print("recent:", r2)
    r3 = ct.run(action="invalid")
    print("invalid:", r3)
    print("CommunicationTool test completed successfully.")