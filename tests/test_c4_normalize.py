"""V4.4 C4-01 阶段 2 测试: canonical 规范化 + 7 原因码 + c4_verify 待核验承载。

覆盖:
- events.payload 白名单 {state, risk_score, priority, timestamp}; benign(frame_idx/title/message) 不触发;
- trigger_type/severity 或真正未知键 -> unknown_keys 待核验;
- 数值(大整数/极小/0.1/1 vs 1.0/负零/Decimal/NaN/±Inf/字符串)契约;
- 时间(带时区 UTC 规范化/无时区保留/超微秒 unparsable);
- 版本(unversioned/version_mismatch) 与 legacy_ts;
- missing_required; 多原因并存;
- c4_verify 新增/合并(并发安全)/business_json 保留未知键与 benign 键;
- enqueue_sync 签名/语义兼容(由 test_c4_sync 定向回归在验证阶段执行)。

隔离: 全部 tempfile 临时库, 不触碰数据/drrm.db。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading

import pytest
from decimal import Decimal

from src.database import canonical
from src.database.db import Database


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "c4_normalize_test.db"))
    yield db


class TestPersonIdInEventHash:
    """C4 阶段 1.4: 顶层 person_id 纳入 person_event 规范化 hash. (type/severity 不入)"""

    _BASE = {"payload": {"state": "Fall", "risk_score": Decimal("0.5"), "priority": "HIGH",
                         "timestamp": "2026-10-28T10:00:00Z"}}

    def _h(self, person_present: bool, person_val=None):
        biz = dict(self._BASE)
        if person_present:
            biz["person_id"] = person_val
        return canonical.compute_content_hash("person_event", biz, version="v1")

    def test_different_person_id_hash_differs(self):
        a = self._h(True, 1)
        b = self._h(True, 2)
        assert a[1] == set() and b[1] == set() and a[0] != b[0]

    def test_same_person_id_stable(self):
        a = self._h(True, 1)
        b = self._h(True, 1)
        assert a[0] == b[0]

    def test_none_vs_missing_hash_differs(self):
        missing = self._h(False)
        none = self._h(True, None)
        assert missing[0] != none[0]

    def test_key_order_stable(self):
        a = self._h(True, 7)
        biz = {"person_id": 7, "payload": {"timestamp": "2026-10-28T10:00:00Z",
                                           "priority": "HIGH", "risk_score": Decimal("0.5"),
                                           "state": "Fall"}}
        b = canonical.compute_content_hash("person_event", biz, version="v1")
        assert a[0] == b[0]

    def test_existing_validations_unchanged(self):
        # 缺 timestamp -> missing_required; 未知键 -> unknown_keys; 非整 float risk -> unparsable
        r = canonical.compute_content_hash("person_event",
                                           {"person_id": 1,
                                            "payload": {"state": "F", "zzz": 1}},
                                           version="v1")
        assert {"missing_required", "unknown_keys"} <= r[1]
        r2 = canonical.compute_content_hash("person_event",
                                            {"person_id": 1,
                                             "payload": {"state": "F", "risk_score": 0.9,
                                                         "priority": "H",
                                                         "timestamp": "2026-10-28T10:00:00Z"}},
                                            version="v1")
        assert "unparsable" in r2[1]

    def test_other_record_types_unaffected(self):
        # readings 白名单不含 person_id; 其 hash 逻辑不变
        a = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": Decimal("30"),
                                                       "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert a[1] == set() and a[0] is not None


class TestCanonicalHash:
    def test_reading_stable_and_keyorder_independent(self):
        a = canonical.compute_content_hash("reading", {
            "sensor_id": "s1", "temperature": Decimal("30.5"),
            "humidity": Decimal("40"), "device_captured_at": "2026-10-28T10:00:00+08:00"}, version="v1")
        b = canonical.compute_content_hash("reading", {
            "device_captured_at": "2026-10-28T10:00:00+08:00", "humidity": Decimal("40"),
            "temperature": Decimal("30.5"), "sensor_id": "s1"}, version="v1")
        ha, ra = a; hb, rb = b
        assert ra == set() and rb == set() and ha == hb  # 键序无关, 稳定

    def test_1_vs_1_0_same(self):
        a = canonical.compute_content_hash("sensor_alert", {"type": "t", "temperature": 1, "threshold": 45,
                                                            "occurred_at": "2026-10-28T10:00:00Z"}, version="v1")
        b = canonical.compute_content_hash("sensor_alert", {"type": "t", "temperature": 1.0, "threshold": 45,
                                                            "occurred_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert a[1] == set() and b[1] == set() and a[0] == b[0]

    def test_0_1_float_unparsable_decimal_ok(self):
        # float 0.1 已丢精度 -> unparsable, 不宣称恢复
        r = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": 0.1,
                                                       "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert r[0] is None and "unparsable" in r[1]
        ok = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": Decimal("0.1"),
                                                        "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert ok[1] == set() and ok[0] is not None

    def test_big_int_and_tiny_nonzero_preserved(self):
        big = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": 2 ** 53 + 1,
                                                         "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert big[1] == set() and big[0] is not None
        tiny = canonical.compute_content_hash("reading", {"sensor_id": "s",
                                                          "temperature": Decimal("1e-400"),
                                                          "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert tiny[1] == set() and tiny[0] is not None  # 极小非零不归零

    def test_neg_zero_normalized(self):
        a = canonical.compute_content_hash("sensor_alert", {"type": "t", "temperature": 0, "threshold": 45,
                                                            "occurred_at": "2026-10-28T10:00:00Z"}, version="v1")
        b = canonical.compute_content_hash("sensor_alert", {"type": "t", "temperature": -0.0, "threshold": 45,
                                                            "occurred_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert a[1] == set() and b[1] == set() and a[0] == b[0]

    def test_nan_inf_unparsable(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            r = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": bad,
                                                           "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
            assert "unparsable" in r[1]

    def test_utc_offset_equivalent(self):
        a = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": Decimal("30"),
                                                       "device_captured_at": "2026-10-28T10:00:00+08:00"}, version="v1")
        b = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": Decimal("30"),
                                                       "device_captured_at": "2026-10-28T02:00:00Z"}, version="v1")
        assert a[0] == b[0]  # 同瞬时规范化一致

    def test_naive_preserved_not_z(self):
        r = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": Decimal("30"),
                                                       "device_captured_at": "2026-10-28T10:00:00"}, version="v1")
        assert r[1] == set() and r[0] is not None  # naive 保留字面, 不猜时区

    def test_sub_micro_unparsable(self):
        r = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": Decimal("30"),
                                                       "device_captured_at": "2026-10-28T10:00:00.123456789+08:00"}, version="v1")
        assert "unparsable" in r[1]  # 超微秒不截断判同

    def test_string_numeric_accepted(self):
        r = canonical.compute_content_hash("reading", {"sensor_id": "s", "temperature": "30.5",
                                                       "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert r[1] == set() and r[0] is not None


class TestReasons:
    def test_unversioned(self):
        r = canonical.classify("reading", {"sensor_id": "s", "temperature": Decimal("1"),
                                           "device_captured_at": "2026-10-28T10:00:00Z"})
        assert "unversioned" in r[1] and r[0] is None

    def test_version_mismatch(self):
        r = canonical.classify("reading", {"sensor_id": "s", "temperature": Decimal("1"),
                                           "device_captured_at": "2026-10-28T10:00:00Z"},
                               version="v1", other_version="v2")
        assert "version_mismatch" in r[1]

    def test_missing_required(self):
        r = canonical.classify("reading", {"temperature": Decimal("1"),
                                           "device_captured_at": "2026-10-28T10:00:00Z"}, version="v1")
        assert "missing_required" in r[1] and r[0] is None

    def test_unknown_keys_trigger_severity(self):
        # events payload 含 trigger_type/severity(白名单外) -> unknown_keys 待核验
        r = canonical.classify("person_event", {"payload": {"state": "Fall", "risk_score": 0.9,
                                                           "priority": "HIGH",
                                                           "timestamp": "2026-10-28T10:00:00Z",
                                                           "trigger_type": "HIGH_RISK"}}, version="v1")
        assert "unknown_keys" in r[1] and r[0] is None

    def test_benign_keys_no_negative(self):
        # frame_idx/title/message 为已知非业务: 不触发 unknown_keys
        r = canonical.classify("person_event", {"payload": {"state": "Fall", "risk_score": Decimal("0.9"),
                                                           "priority": "HIGH",
                                                           "timestamp": "2026-10-28T10:00:00Z",
                                                           "frame_idx": 1204, "message": "hi", "title": "t"}},
                               version="v1")
        assert r[1] == set() and r[0] is not None

    def test_truly_unknown_key(self):
        r = canonical.classify("person_event", {"payload": {"state": "Fall", "risk_score": 0.9,
                                                           "priority": "HIGH",
                                                           "timestamp": "2026-10-28T10:00:00Z",
                                                           "zzz_weird": 1}}, version="v1")
        assert "unknown_keys" in r[1]

    def test_legacy_ts_only(self):
        r = canonical.classify("person_event", {"payload": {"state": "Fall", "risk_score": 0.9,
                                                           "priority": "HIGH", "ts": "2026-10-28T10:00:00+08:00"}},
                               version="v1")
        assert "legacy_ts" in r[1]

    def test_multi_reasons_coexist(self):
        r = canonical.classify("person_event", {"payload": {"state": "Fall", "zzz": 1}}, version=None)
        assert {"unknown_keys", "missing_required", "unversioned"} <= r[1]

    def test_content_conflict_is_valid_code(self):
        assert "content_conflict" in canonical.REASONS


class TestVerifyStore:
    def test_add_and_get(self, db):
        db.add_verify("reading", "R-1", {"unparsable"}, business_json={"sensor_id": "s", "temperature": 0.1},
                      edge_id="e")
        got = db.get_verify("reading", "R-1")
        assert got is not None and got["reasons"] == '["unparsable"]'

    def test_merge_reasons_same_key(self, db):
        db.add_verify("reading", "R-1", {"unparsable"})
        db.add_verify("reading", "R-1", {"unknown_keys"})
        got = db.get_verify("reading", "R-1")
        assert set(canonical.parse_reasons_json(got["reasons"])) == {"unparsable", "unknown_keys"}
        # 同键不新增行
        assert len(db.list_verify()) == 1

    def test_business_json_preserves_unknown_and_benign_keys(self, db):
        biz = {"payload": {"state": "F", "risk_score": 0.9, "priority": "HIGH",
                           "timestamp": "2026-10-28T10:00:00Z",
                           "frame_idx": 1204, "trigger_type": "HIGH_RISK", "zzz": 1}}
        db.add_verify("person_event", "E-X", {"unknown_keys"}, business_json=biz)
        got = db.get_verify("person_event", "E-X")
        stored = json.loads(got["business_json"])
        assert stored["payload"]["frame_idx"] == 1204       # benign 保留
        assert stored["payload"]["trigger_type"] == "HIGH_RISK"  # 未知/非白保留
        assert stored["payload"]["zzz"] == 1

    def test_concurrent_merge_no_loss(self, db, tmp_path):
        # 用两个独立 Database 实例指向同一临时文件: with_transaction 的 _tx 嵌套守卫为
        # 单实例非线程安全, 真实并发模型应为多实例同库。
        db_file = os.path.join(tmp_path, "c4_conc.db")
        a = Database(db_path=db_file)
        b = Database(db_path=db_file)
        barrier = threading.Barrier(2)
        results = []

        def _add(inst, code):
            try:
                barrier.wait(timeout=5)
                inst.add_verify("sensor_alert", "E-C", {code})
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        t1 = threading.Thread(target=_add, args=(a, "unparsable"))
        t2 = threading.Thread(target=_add, args=(b, "unknown_keys"))
        t1.start(); t2.start(); t1.join(timeout=10); t2.join(timeout=10)
        assert not results
        got = a.get_verify("sensor_alert", "E-C")
        assert set(canonical.parse_reasons_json(got["reasons"])) == {"unparsable", "unknown_keys"}
        assert len(a.list_verify()) == 1

    def test_invalid_reason_rejected(self, db):
        with pytest.raises(ValueError):
            db.add_verify("reading", "R-9", {"bogus"})