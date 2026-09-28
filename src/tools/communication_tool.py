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
            "severity": severity,
            "title": title,
            "message": message,
            "person_id": kwargs.get("person_id"),
            "event_id": kwargs.get("event_id"),
            "task_id": kwargs.get("task_id"),
            "image_path": kwargs.get("image_path"),
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
        """钉钉 markdown 报警模板。

        首行必须含"灾害报警"(钉钉安全关键词, 否则 errcode 310000 拒绝)。
        取值优先读 alert 顶层, 再回退 extra(Agent/Executor 可能把
        state/risk_score/priority 放在 extra 内)。
        """
        ts = alert.get("timestamp") or datetime.now().isoformat(timespec="seconds")
        extra = alert.get("extra") or {}
        person = alert.get("person_id")
        state = alert.get("state") or extra.get("state") or "-"
        priority = alert.get("priority") or alert.get("severity") or extra.get("priority") or "-"
        risk = extra.get("risk_score") if extra.get("risk_score") is not None else (
            alert.get("risk_score") if alert.get("risk_score") is not None else "-"
        )
        lines = [
            "## 🚨 灾害报警",
            "",
            "人员ID:",
            str(person if person is not None else "-"),
            "",
            "状态:",
            str(state),
            "",
            "风险等级:",
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
        ]
        return "\n".join(lines)

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
            # 现场图片证据: 上传 snapshot 得 image_url, 再随文字报警发送图片
            img_path = alert.get("image_path")
            img_ok = None
            image_url = None
            image_error = None
            if img_path:
                try:
                    from src.storage import upload_snapshot
                    up = upload_snapshot(img_path)
                    if up.get("ok") and up.get("url"):
                        image_url = up["url"]
                        img_res = self._send_with_retry(
                            channel, "image", image_url,
                            {"alert": alert, "source": "snapshot"},
                        )
                        img_ok = bool(img_res and img_res.get("ok"))
                        if not img_ok:
                            image_error = (img_res or {}).get("error")
                    else:
                        img_ok = False
                        image_error = up.get("error", "upload failed")
                except Exception as exc:  # noqa: BLE001
                    img_ok = False
                    image_error = f"upload exception: {exc}"
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