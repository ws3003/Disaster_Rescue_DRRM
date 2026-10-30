"""V4.4 C4 Outbox worker 测试 (任务 §6.2)。

覆盖 (12 项):
1. claim -> HTTP 成功 -> outbox success;
2. 网络连接失败 -> retry_wait;
3. 超时 / 可重试服务端错误 -> retry_wait;
4. 永久性请求错误 -> dead_letter;
5. 同 hash 重复回执 (already_synced) -> 幂等成功;
6. 内容冲突回执不被当作普通成功;
7. worker 重启后继续发送待处理任务;
8. 旧 gen / 过期租约回写被 fencing 拒绝;
9. 两个 worker 竞争领取同一记录不会错误确认;
10. HTTP 请求发生在数据库事务之外;
11. HTTP client 异常 / 无效响应不错误标记 success;
12. 单条失败后批次继续处理下一条。

隔离: 临时 SQLite; HTTP 用注入 mock client, 不发真实网络。
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal

import pytest

from src.database import canonical
from src.database.db import Database
from src.sync import bridge
from src.sync.worker import OutboxWorker

_TOKEN = "worker-token"


class _Resp:
    """极简 fake HTTP 响应: 持有 status_code + body(构造时可为 JSON dict 或抛错工厂)。"""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        if callable(self._body):
            return self._body()
        return self._body


def _resp_ok(sid, status="synced"):
    return _Resp(200, {"code": "OK", "message": "ok",
                       "data": [{"record_type": "x", "source_record_id": sid,
                                 "status": status, "error_code": None}],
                       "meta": {}})


def _not_json(sid):
    return _Resp(200, lambda: (_ for _ in ()).throw(ValueError("no json")))


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "c4_worker_test.db"))
    yield db


def _seed_sensor_alert(db):
    r = bridge.create_sync_record(
        db, record_type="sensor_alert",
        business={"type": "sensor_hightemp", "temperature": 52, "threshold": 45,
                  "occurred_at": "2026-10-28T10:00:00Z",
                  "device_captured_at": "2026-10-28T10:00:00Z"})
    return r["source_record_id"]


def _seed_person_event(db):
    r = bridge.create_sync_record(
        db, record_type="person_event",
        business={"type": "memory_alert", "severity": "HIGH", "person_id": 1,
                  "payload": {"state": "Fall", "priority": "HIGH",
                              "timestamp": "2026-10-28T10:00:00Z"}})
    return r["source_record_id"]


def _seed_fractional_reading(db):
    """含小数值读数: bridge 可入队(Decimal 可哈希), 但存库为 REAL 后 worker 无法重建哈希。"""
    r = bridge.create_sync_record(
        db, record_type="reading",
        business={"sensor_id": "s1", "temperature": Decimal("30.5"), "humidity": Decimal("40"),
                  "device_captured_at": "2026-10-28T10:00:00Z"})
    return r["source_record_id"]


def _worker(db, post, **kw):
    return OutboxWorker(db, base_url="http://server", token=_TOKEN, http_post=post, **kw)


def _outbox_of(db, sid):
    row = None
    for o in db.list_outbox():
        if o["source_record_id"] == sid:
            row = o
    return row


# ---- 1-4: 基础回执 -> 状态推进 ----
class TestOutcomes:
    def test_claim_http_ok_to_success(self, db):
        sid = _seed_sensor_alert(db)
        captured = {}

        def post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            captured["headers"] = headers
            return _resp_ok(sid, "synced")

        results = _worker(db, post).process_batch()
        assert results == ["success"]
        got = _outbox_of(db, sid)
        assert got["status"] == "success" and got["completed_at"] is not None
        # 请求应携带鉴权头与单条记录 +(同键同内容 hash)
        assert captured["headers"]["Authorization"] == f"Bearer {_TOKEN}"
        sent = captured["json"]["records"][0]
        assert sent["record_type"] == "sensor_alert" and sent["source_record_id"] == sid
        assert isinstance(sent["content_hash"], str) and sent["content_hash"]

    def test_network_failure_to_retry_wait(self, db):
        sid = _seed_sensor_alert(db)

        def post(url, json=None, headers=None, timeout=None):
            raise ConnectionError("boom")

        results = _worker(db, post).process_batch()
        assert results == ["retry"]
        got = _outbox_of(db, sid)
        assert got["status"] == "retry_wait"
        assert got["retry_count"] == 1
        assert got["next_attempt_at"] is not None
        assert "network" in (got["last_error"] or "")

    def test_timeout_and_retryable_5xx_to_retry_wait(self, db):
        # 超时(抛 TimeoutError)
        sid_a = _seed_sensor_alert(db)
        r1 = _worker(db, lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out"))).process_batch()
        assert r1 == ["retry"]
        assert _outbox_of(db, sid_a)["status"] == "retry_wait"
        # 5xx 可重试服务端错误
        sid_b = _seed_sensor_alert(db)
        r2 = _worker(db, lambda *a, **k: _Resp(503, {"code": "INTERNAL_ERROR"})).process_batch()
        assert r2 == ["retry"]
        assert _outbox_of(db, sid_b)["status"] == "retry_wait"

    def test_permanent_4xx_to_dead_letter(self, db):
        sid = _seed_sensor_alert(db)
        results = _worker(db, lambda *a, **k: _Resp(400, {"code": "INVALID_PARAMETER"})).process_batch()
        assert results == ["dead"]
        assert _outbox_of(db, sid)["status"] == "dead_letter"


# ---- 5-6: 幂等 / 冲突回执 ----
class TestReceiptClass:
    def test_already_synced_treated_as_success(self, db):
        sid = _seed_sensor_alert(db)
        results = _worker(db, lambda *a, **k: _resp_ok(sid, "already_synced")).process_batch()
        assert results == ["success"]
        assert _outbox_of(db, sid)["status"] == "success"

    def test_content_mismatch_not_success(self, db):
        sid = _seed_sensor_alert(db)
        results = _worker(db, lambda *a, **k: _resp_ok(sid, "content_mismatch")).process_batch()
        assert results == ["dead"]  # 冲突按既有策略进入死信, 不误判普通成功
        assert _outbox_of(db, sid)["status"] == "dead_letter"
        assert _outbox_of(db, sid)["last_error"] == "content_mismatch"


# ---- 7: 重启继续 ----
class TestRestart:
    def test_restart_continues_pending_and_due_retry(self, db):
        s1 = _seed_sensor_alert(db)
        s2 = _seed_sensor_alert(db)
        far_future = 999_999_999_999_999

        # 第一轮: 网络失败 -> 两条都进 retry_wait (next_attempt_at = now+delay)
        fail = _worker(db, lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")),
                       now_ms=lambda: 1_000)
        assert fail.process_batch() == ["retry", "retry"]

        # "重启": 全新 worker 实例, 时间推进到重试到期后, 服务端恢复 -> 全部成功
        ok = _worker(db, lambda *a, **k: _resp_ok(s1, "synced"),
                     now_ms=lambda: far_future)
        # 每条回执都带各自 sid 才可靠: 这里用按 sid 分派
        def post(url, json=None, headers=None, timeout=None):
            sid = json["records"][0]["source_record_id"]
            return _resp_ok(sid, "synced")

        ok2 = _worker(db, post, now_ms=lambda: far_future)
        results = ok2.process_batch()
        assert set(results) == {"success"}
        assert _outbox_of(db, s1)["status"] == "success"
        assert _outbox_of(db, s2)["status"] == "success"


# ---- 8-9: fencing 与并发 ----
class TestFencing:
    def test_old_gen_writeback_rejected(self, db):
        sid = _seed_sensor_alert(db)
        oid = db.outbox_claim("worker-A", now_ms=1_000)          # gen -> 1
        assert oid is not None
        # 租约过期后 worker-B 重新领取 -> gen -> 2
        oid2 = db.outbox_claim("worker-B", now_ms=100_000)
        assert oid2 == oid
        assert db.get_outbox(oid)["gen"] == 2
        # 旧 worker A 用旧 gen=1 回写 -> fencing 拒绝 -> lost, 不标记 success
        res = _worker(db, lambda *a, **k: _resp_ok(sid, "synced")).process_one(oid, gen=1)
        assert res == "lost"
        assert _outbox_of(db, sid)["status"] == "processing"  # 未被 A 覆盖为 success

    def test_expired_lease_reclaim_then_new_gen_ok(self, db):
        sid = _seed_sensor_alert(db)
        oid = db.outbox_claim("worker-A", now_ms=1_000)
        oid2 = db.outbox_claim("worker-B", now_ms=100_000)  # A 租约(31s)已过期 -> B 领取
        assert oid2 == oid
        got = db.get_outbox(oid)
        assert got["gen"] == 2 and got["worker_id"] == "worker-B"
        # 新 worker 用当前 gen 回写成功
        res = _worker(db, lambda *a, **k: _resp_ok(sid, "synced"), worker_id="worker-B").process_one(oid, gen=got["gen"])
        assert res == "success" and _outbox_of(db, sid)["status"] == "success"

    def test_two_workers_one_claim_no_wrong_confirm(self, db):
        sid = _seed_sensor_alert(db)
        oidA = db.outbox_claim("worker-A", now_ms=5_000)
        oidB = db.outbox_claim("worker-B", now_ms=5_000)   # 未过期 -> B 拿不到
        assert oidA is not None and oidB is None
        got = db.get_outbox(oidA)
        assert got["worker_id"] == "worker-A"
        res = _worker(db, lambda *a, **k: _resp_ok(sid, "synced"), worker_id="worker-A").process_one(oidA, gen=got["gen"])
        assert res == "success" and _outbox_of(db, sid)["status"] == "success"


# ---- 10-12: 事务边界 / 脏回执 / 批次韧性 ----
class TestRobustness:
    def test_http_request_outside_db_transaction(self, db):
        sid = _seed_sensor_alert(db)
        seen = {}

        def post(url, json=None, headers=None, timeout=None):
            # 在 HTTP 执行时, 领取事务必须已提交: 记录呈 processing 状态,
            # 且此时读取不抛嵌套事务错误(证明无活跃写事务包裹 HTTP)。
            rows = db.list_outbox()
            seen["claimed_processing"] = {o["status"] for o in rows if o["source_record_id"] == sid}
            return _resp_ok(sid, "synced")

        results = _worker(db, post).process_batch()
        assert results == ["success"]
        assert seen["claimed_processing"] == {"processing"}  # HTTP 期间记录为 processing(已领取提交)

    def test_invalid_or_malformed_response_not_success(self, db):
        sid = _seed_sensor_alert(db)
        # 200 但响应体非 JSON
        r1 = _worker(db, lambda *a, **k: _not_json(sid)).process_batch()
        assert r1 == ["retry"] and _outbox_of(db, sid)["status"] == "retry_wait"
        # 200 但 data 缺失本记录 -> 状态未知 -> 重试, 不标成功
        sid2 = _seed_sensor_alert(db)
        r2 = _worker(db, lambda *a, **k: _Resp(200, {"code": "OK", "data": []})).process_batch()
        assert r2 == ["retry"] and _outbox_of(db, sid2)["status"] == "retry_wait"
        # client 抛异常 -> 重试, 不标成功
        sid3 = _seed_sensor_alert(db)
        r3 = _worker(db, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("client crash"))).process_batch()
        assert r3 == ["retry"] and _outbox_of(db, sid3)["status"] == "retry_wait"

    def test_batch_continues_after_single_failure(self, db):
        s1, s2 = _seed_sensor_alert(db), _seed_sensor_alert(db)
        state = {"fail_sid": s1, "calls": 0}

        def post(url, json=None, headers=None, timeout=None):
            sid = json["records"][0]["source_record_id"]
            state["calls"] += 1
            if sid == state["fail_sid"]:
                raise ConnectionError("down")
            return _resp_ok(sid, "synced")

        results = _worker(db, post).process_batch()
        # 第一条网络失败 -> retry; 第二条成功 -> success; 不因单条失败终止批次
        assert set(results) == {"success", "retry"}
        assert _outbox_of(db, s1)["status"] == "retry_wait"
        assert _outbox_of(db, s2)["status"] == "success"


class TestHashReuse:
    def test_person_event_with_exact_values_hashable(self, db):
        sid = _seed_person_event(db)
        results = _worker(db, lambda *a, **k: _resp_ok(sid, "synced")).process_batch()
        assert results == ["success"] and _outbox_of(db, sid)["status"] == "success"


# ---- E7→C4 数值契约衔接后：合法小数值 reading 现可正常同步（worker 重读按 exact 重哈希，
#     不再因 Decimal 入队 → SQLite float → 重读 float 而误判 unbuildable）----
class TestFractionalNowSyncs:
    def test_fractional_syncs_when_server_ok(self, db):
        sid = _seed_fractional_reading(db)
        results = _worker(db, lambda *a, **k: _resp_ok(sid, "synced")).process_batch()
        out = _outbox_of(db, sid)
        assert out["status"] == "success" and out["completed_at"] is not None
        assert db.get_verify("reading", sid) is None          # 不再误入 c4_verify
        assert len(results) == 1

    def test_fractional_network_error_retry_wait(self, db):
        sid = _seed_fractional_reading(db)
        _worker(db, lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))).process_batch()
        got = _outbox_of(db, sid)
        # 现可哈希 → 网络错误走重试，而非被当成不可构建的 dead_letter
        assert got["status"] == "retry_wait" and got["status"] != "dead_letter"
        assert "network" in (got["last_error"] or "").lower()

    def test_fractional_no_verify_written(self, db):
        sid = _seed_fractional_reading(db)
        _worker(db, lambda *a, **k: _resp_ok(sid, "synced")).process_batch()
        assert db.get_verify("reading", sid) is None
        assert _outbox_of(db, sid)["status"] == "success"

    def test_fractional_hash_stable_preserves_value(self, db):
        sid = _seed_fractional_reading(db)
        with db._connect() as c:
            stored = c.execute("SELECT temperature FROM readings WHERE reading_id=?",
                               (sid,)).fetchone()[0]
        assert float(stored) == 30.5                          # 精度保留
        biz = {"sensor_id": "s1", "temperature": bridge.exact_number(float(stored)),
               "humidity": bridge.exact_number(40.0), "device_captured_at": "2026-10-28T10:00:00Z"}
        h, reasons = canonical.compute_content_hash("reading", biz,
                                                    version=bridge.SYNC_NORM_VERSION)
        assert sorted(reasons) == [] and h

    def test_integer_record_still_success(self, db):
        sid = _seed_sensor_alert(db)
        results = _worker(db, lambda *a, **k: _resp_ok(sid, "synced")).process_batch()
        assert results == ["success"] and _outbox_of(db, sid)["status"] == "success"
        assert db.get_verify("sensor_alert", sid) is None


class TestFencing:
    """A 失去认领后陈旧 gen 回写被拒（fencing），与数值无绑定。"""

    def test_stale_gen_writeback_rejected(self, db):
        sid = _seed_sensor_alert(db)
        oid = db.outbox_claim("A", now_ms=1_000)
        assert oid is not None
        genA = db.get_outbox(oid)["gen"]
        assert db.outbox_claim("B", now_ms=100_000) == oid
        genB = db.get_outbox(oid)["gen"]
        assert genB == genA + 1
        # A 用陈旧 gen 回写 dead_letter 被 fencing 拒绝；B 的状态保留
        assert db.outbox_mark_dead_letter(oid, "A", genA, last_error="stale") is False
        assert db.get_outbox(oid)["status"] == "processing" and db.get_outbox(oid)["worker_id"] == "B"
