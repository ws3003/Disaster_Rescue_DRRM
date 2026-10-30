"""src/sync/worker.py — C4 Outbox worker: 出队 → HTTP 发送 → 按回执确认/重试/死信。

复用现有 DAO(&db.outbox_claim/outbox_mark_success/mark_retry_wait/mark_dead_letter) 与 CAS fencing,
不在本模块重写 Outbox SQL。网络请求在数据库事务外执行。
HTTP client / 时间 / 退避 可注入(test 使用 mock transport)。
不接入守护进程/服务钩子;提供可独立调用、可测试的轮询入口。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from src.database import canonical
from src.database.db import Database
from src.sync import bridge

# 业务数据同步：reading/sensor_alert 在 HTTP 载荷携带完整业务本体（服务端写只读副本）
_BUSINESS_CARRIERS = frozenset({"reading", "sensor_alert"})


class OutboxWorker:
    """发送 Outbox 记录到服务端接收接口, 并据回执推进状态。"""

    def __init__(
        self,
        db: Database,
        base_url: str,
        token: str,
        *,
        http_post: Optional[Callable[..., Any]] = None,
        worker_id: str = "worker-1",
        retry_base_ms: int = 5000,
        retry_max_ms: int = 300000,
        now_ms: Optional[Callable[[], int]] = None,
        request_timeout: float = 10.0,
    ):
        self.db = db
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._post = http_post or _default_post
        self.worker_id = worker_id
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.timeout = request_timeout

    # ---- 权限读取权威业务并计算 canonical hash ----
    def _read_business(self, record_type: str, source_record_id: str) -> Optional[dict]:
        if record_type == "person_event":
            row = self.db.get_event(source_record_id)
            if not row:
                return None
            # get_event 已把 payload JSON 反序列化为 dict
            return {"type": row["type"], "severity": row.get("severity"),
                    "person_id": row.get("person_id"), "payload": row.get("payload")}
        if record_type == "reading":
            row = self.db.get_reading(source_record_id)
            if not row:
                return None
            return {"sensor_id": row["sensor_id"],
                    "temperature": bridge.exact_number(row.get("temperature")),
                    "humidity": bridge.exact_number(row.get("humidity")),
                    "device_captured_at": row.get("device_captured_at")}
        if record_type == "sensor_alert":
            row = self.db.get_sensor_alert(source_record_id)
            if not row:
                return None
            return {"type": row["type"],
                    "temperature": bridge.exact_number(row.get("temperature")),
                    "humidity": bridge.exact_number(row.get("humidity")),
                    "threshold": bridge.exact_number(row.get("threshold")),
                    "severity": row.get("severity"), "device_captured_at": row.get("device_captured_at"),
                    "reading_id": row.get("reading_id"), "occurred_at": row.get("occurred_at")}
        return None

    def _hash_for(self, record_type: str, source_record_id: str, biz: Optional[dict] = None) -> tuple[Optional[str], set]:
        if biz is None:
            biz = self._read_business(record_type, source_record_id)
        if not biz:
            return None, set()
        view = bridge._canonical_view(record_type, biz)
        h, reasons = canonical.classify(record_type, view, version=bridge.SYNC_NORM_VERSION)
        return h, reasons

    def _to_verify(self, oid: int, gen: int, record_type: str, sid: str, reasons: set, biz: dict) -> str:
        """无法重建稳定 content_hash 的记录: 原子幂等记入 c4_verify 后, 再以 fencing 条件将 outbox 推向终态。

        - verify 先行(idempotent upsert, 持 business 快照): 即使后续 outbox 更新因故失败,
          记录也压在 c4_verify, 不会"先死信后写 verify"导致永久丢失。
        - 之后才用 (id + processing + worker + gen) 的 fencing 条件把 outbox 置 dead_letter;
          rowcount==0(已输掉认领)则返回 lost, 不覆盖其他 worker 的结果。
        - 确定性 unparsable 不再走 retry_wait(避免掩盖永久错误), 也绝不当普通成功发送。
        """
        rsn = reasons if reasons else {"unparsable"}
        self.db.add_verify(record_type, sid, rsn, business_json=biz)
        err = "unbuildable:" + ",".join(sorted(rsn))
        ok = self.db.outbox_mark_dead_letter(oid, self.worker_id, gen, last_error=err)
        return "dead" if ok else "lost"

    def _retry(self, oid: int, gen: int, err: str) -> str:
        row = self.db.get_outbox(oid)
        rc = row["retry_count"] if row else 0
        delay = min(self.retry_base_ms * (2 ** min(rc, 10)), self.retry_max_ms)
        ok = self.db.outbox_mark_retry_wait(oid, self.worker_id, gen,
                                            next_attempt_at=self._now() + delay, last_error=err)
        return "retry" if ok else "lost"

    def _parse(self, resp, sid: str) -> str:
        """按服务端回执分类. 返回 synced/already_synced/content_mismatch/invalid/retryable."""
        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                return "retryable"
            per = (body.get("data") or []) if isinstance(body, dict) else []
            for p in per:
                if p.get("source_record_id") == sid:
                    s = p.get("status")
                    if s in ("synced", "already_synced"):
                        return s
                    if s == "content_mismatch":
                        return "content_mismatch"
                    return "retryable"
            return "retryable"  # 缺本条结果 -> 状态未知, 重试
        if resp.status_code in (400, 403, 413, 422):
            return "invalid"
        if 500 <= resp.status_code < 600:
            return "retryable"
        return "retryable"

    def process_one(self, oid: int, gen: int) -> str:
        row = self.db.get_outbox(oid)
        if not row:
            return "lost"
        rt, sid = row["record_type"], row["source_record_id"]
        biz = self._read_business(rt, sid)
        if not biz:
            self.db.outbox_mark_dead_letter(oid, self.worker_id, gen, last_error="unbuildable:missing_business")
            return "dead"
        h, reasons = self._hash_for(rt, sid, biz)
        if not h:
            return self._to_verify(oid, gen, rt, sid, reasons, biz)
        rec = {"record_type": rt, "source_record_id": sid, "content_hash": h}
        # 业务数据同步：reading/sensor_alert 携带完整业务本体（精确表示），供服务端写只读副本。
        # person_event 继续沿用旧协议(无 business)；media_evidence 永不入 Outbox/载荷。
        if rt in _BUSINESS_CARRIERS and isinstance(biz, dict):
            rec["business"] = biz
        payload = {"records": [rec]}
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = self._post(self.base_url + "/api/v1/c4/records", json=payload,
                              headers=headers, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            return self._retry(oid, gen, f"network:{exc}")

        status = self._parse(resp, sid)
        if status in ("synced", "already_synced"):
            ok = self.db.outbox_mark_success(oid, self.worker_id, gen)
            return "success" if ok else "lost"
        if status in ("content_mismatch", "invalid"):
            ok = self.db.outbox_mark_dead_letter(oid, self.worker_id, gen, last_error=status)
            return "dead" if ok else "lost"
        return self._retry(oid, gen, "retryable:" + status)

    def process_batch(self, max_records: int = 10) -> list[str]:
        results: list[str] = []
        for _ in range(int(max_records)):
            oid = self.db.outbox_claim(self.worker_id, now_ms=self._now())
            if oid is None:
                break
            row = self.db.get_outbox(oid)
            gen = row["gen"] if row else 0
            results.append(self.process_one(oid, gen))
        return results


def _default_post(url, json=None, headers=None, timeout=10.0):
    import requests
    return requests.post(url, json=json, headers=headers, timeout=timeout)