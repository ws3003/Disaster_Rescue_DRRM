"""V4.4 Task3+Task4 阶段1 —— 方案A：C4 多记录批次原子性修复 专项测试。

背景：原实现中媒体首收 status=success 的 playable 守卫位于 _ingest 内、批次预校验之外，
导致"前序合法记录先提交、后置不可播放 success 媒体抛 400"时出现整批 400 但前序已落库
（违反批次级"任一条非法→整批400、不产生半写入"契约）。

本文件验证修复后：媒体首收 playable 语义校验移至批次预校验（_media_first_receive_guard），
在任何 _ingest 之前整批完成；并用 seen 保留同批重复 source_record_id 的既有顺序语义。

隔离：独立临时 SQLite（依赖覆盖 get_db），真实 TestClient 走 HTTP 层，不触碰真实库。
覆盖：A/B/C/D 核心、非 success 不误拒、UPDATE 不被误判 INSERT、idem 在但行缺不补建、
business 缺失/非dict、同批重复、其他业务类型、并发探针、business缺失前置的重复边界。
"""
from __future__ import annotations

import os
import tempfile
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import c4
from src.database.db import Database

TOKEN = "c4-batch-token"


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    monkeypatch.setenv("C4_API_TOKEN", TOKEN)


def _make_app(db: Database) -> TestClient:
    app = FastAPI()
    app.include_router(c4.router)
    app.dependency_overrides[c4.get_db] = lambda: db
    return TestClient(app)


@pytest.fixture()
def client() -> TestClient:
    tmp = tempfile.TemporaryDirectory()
    db = Database(db_path=os.path.join(tmp.name, "c4_batch.db"))
    yield _make_app(db)
    try:
        tmp.cleanup()
    except Exception:  # noqa: BLE001
        pass


def _get_db(client: TestClient) -> Database:
    return client.app.dependency_overrides[c4.get_db]()


def _ch(rt, business) -> str:
    h, _ = c4._hash_of_business(rt, business)
    assert h, f"business 不可哈希(应不会发生): {rt} {business}"
    return h


def _post(client: TestClient, records) -> TestClient:
    return client.post("/api/v1/c4/records",
                       json={"records": records},
                       headers={"Authorization": f"Bearer {TOKEN}"})


def _statuses(resp) -> list[str]:
    return [d["status"] for d in resp.json()["data"]]


def _counts(db: Database) -> dict:
    with db._connect() as conn:
        return {
            "readings": conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0],
            "media": conn.execute("SELECT COUNT(*) FROM media_evidence").fetchone()[0],
            "idem_media": conn.execute(
                "SELECT COUNT(*) FROM c4_idem WHERE record_type='media_evidence'").fetchone()[0],
            "idem_reading": conn.execute(
                "SELECT COUNT(*) FROM c4_idem WHERE record_type='reading'").fetchone()[0],
            "idem": conn.execute("SELECT COUNT(*) FROM c4_idem").fetchone()[0],
            "conflict": conn.execute("SELECT COUNT(*) FROM c4_content_conflict").fetchone()[0],
        }


def _media(sid="M1", *, status="pending", upload="pending", ok=None,
           ref="r1", mtype="image", hash_override=None, business_missing=False):
    if business_missing:
        return {"record_type": "media_evidence", "source_record_id": sid,
                "content_hash": hash_override or "legacy-hash", "edge_id": "e1"}
    b = {"media_id": sid, "ref_id": ref, "source": "sensor", "media_type": mtype,
         "status": status, "captured_at": "2026-10-24T12:00:00+08:00"}
    if upload is not None:
        b["upload_status"] = upload
    if ok is not None:
        b["object_key"] = ok
    h = _ch("media_evidence", b) if hash_override is None else hash_override
    return {"record_type": "media_evidence", "source_record_id": sid,
            "content_hash": h, "edge_id": "e1", "business": b}


def _reading(sid="R1"):
    b = {"sensor_id": "S1", "temperature": "25.5",
         "device_captured_at": "2026-10-24T12:00:00+08:00"}
    return {"record_type": "reading", "source_record_id": sid,
            "content_hash": _ch("reading", b), "edge_id": "e1", "business": b}


def _alert(sid="A1"):
    b = {"type": "temp-high", "occurred_at": "2026-10-24T12:00:00+08:00"}
    return {"record_type": "sensor_alert", "source_record_id": sid,
            "content_hash": _ch("sensor_alert", b), "edge_id": "e1", "business": b}


# ---- A/B/C/D：批次原子性核心 ----

def test_A_batch_valid_reading_then_unplayable_success_media(client):
    db = _get_db(client)
    unplayable = _media(status="success", upload="pending", ok=None)  # 不可播放
    r = _post(client, [_reading(), unplayable])
    assert r.status_code == 400  # 整批预校验拒绝
    c = _counts(db)
    assert c["readings"] == 0 and c["media"] == 0 and c["idem"] == 0 and c["conflict"] == 0


def test_B_batch_unplayable_success_media_then_valid_reading(client):
    db = _get_db(client)
    unplayable = _media(status="success", upload="pending", ok=None)
    r = _post(client, [unplayable, _reading()])
    assert r.status_code == 400
    c = _counts(db)
    assert c["readings"] == 0 and c["media"] == 0 and c["idem"] == 0 and c["conflict"] == 0


def test_C_single_unplayable_success_media(client):
    db = _get_db(client)
    r = _post(client, [_media(status="success", upload="pending", ok=None)])
    assert r.status_code == 400
    c = _counts(db)
    assert c["media"] == 0 and c["idem_media"] == 0 and c["conflict"] == 0


def test_D_legal_success_media_ok(client):
    db = _get_db(client)
    r = _post(client, [_media(status="success", upload="success", ok="obj/M1.mp4")])
    assert r.status_code == 200
    assert _statuses(r) == ["synced"]
    c = _counts(db)
    assert c["media"] == 1 and c["idem_media"] == 1 and c["conflict"] == 0


# ---- 非 success 状态不误拒 ----

@pytest.mark.parametrize("status,upload", [
    ("pending", "pending"),
    ("processing", "uploading"),
    ("failed-awaiting-retry", "failed"),
])
def test_non_success_states_not_rejected(client, status, upload):
    db = _get_db(client)
    r = _post(client, [_media(status=status, upload=upload)])
    assert r.status_code == 200, f"非 success 状态 {status} 被误拒"
    assert _statuses(r) == ["synced"]
    c = _counts(db)
    assert c["media"] == 1 and c["idem_media"] == 1


# ---- UPDATE 不被误判为 INSERT ----

def test_update_not_misjudged_insert(client):
    db = _get_db(client)
    # 先建合法 success 媒体 + idem
    r1 = _post(client, [_media(status="success", upload="success", ok="obj/M1.mp4")])
    assert r1.status_code == 200
    with db._connect() as conn:
        h0 = conn.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type='media_evidence' AND source_record_id='M1'"
        ).fetchone()[0]
    # 提交非法回退 UPDATE：status=success, upload=pending（idem 已存在 → 绝不能被守卫 400）
    r2 = _post(client, [_media(status="success", upload="pending", ok=None)])
    assert r2.status_code == 200, "UPDATE 被误判为 INSERT 触发 400"
    assert _statuses(r2) == ["content_mismatch"]
    c = _counts(db)
    assert c["media"] == 1          # 原媒体行不变
    with db._connect() as conn:
        row = conn.execute("SELECT status, upload_status, object_key FROM media_evidence WHERE media_id='M1'").fetchone()
        h1 = conn.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type='media_evidence' AND source_record_id='M1'"
        ).fetchone()[0]
    assert (row["status"], row["upload_status"], row["object_key"]) == ("success", "success", "obj/M1.mp4")
    assert h1 == h0                 # idem hash 未推进
    assert c["conflict"] == 1       # 仍按原逻辑记冲突审计


# ---- idem 存在但业务行缺失：不自动补建 ----

def test_idem_exists_media_row_missing_no_autocreate(client):
    db = _get_db(client)
    with db.with_transaction() as tx:
        tx.execute(
            "INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at, edge_id) "
            "VALUES ('media_evidence', 'Mx', 'h0', 0, 'e')")
    r = _post(client, [_media("Mx", status="success", upload="success", ok="obj/Mx.mp4")])
    assert r.status_code == 200, "idem 已存在但行缺失不应触发首收 400"
    assert _statuses(r) == ["content_mismatch"]
    c = _counts(db)
    assert c["media"] == 0                      # 不自动补建业务行
    with db._connect() as conn:
        h1 = conn.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type='media_evidence' AND source_record_id='Mx'"
        ).fetchone()[0]
    assert h1 == "h0"                           # 原 hash 不变
    assert c["conflict"] == 1


# ---- business 缺失 / 非 dict ----

def test_business_missing_old_client(client):
    db = _get_db(client)
    rec = _media("M1", business_missing=True)
    r = _post(client, [rec])
    assert r.status_code == 200, "business 缺失(旧客户端)应保持兼容，不得被新守卫 400"
    assert _statuses(r) == ["synced"]
    c = _counts(db)
    assert c["idem_media"] == 1 and c["media"] == 0   # 仅登记, 不写业务副本


def test_business_not_dict_rejected_by_structure(client):
    db = _get_db(client)
    rec = _media("M1", status="pending")
    rec["business"] = "not-a-dict"
    r = _post(client, [rec])
    assert r.status_code == 400              # 既有结构校验(_validate_business)拒绝, 非新守卫
    c = _counts(db)
    assert c["media"] == 0 and c["idem_media"] == 0 and c["conflict"] == 0


# ---- 同批重复 source_record_id ----

def test_same_batch_duplicate_sid_order_preserved(client):
    db = _get_db(client)
    # 第一条 pending 合法, 第二条 success-playable（同 M1, 异 hash → 走 UPDATE 状态机）
    r = _post(client, [_media(status="pending", upload="pending"),
                       _media(status="success", upload="success", ok="obj/M1.mp4")])
    assert r.status_code == 200, "同批同键重复不应被新守卫 400"
    assert _statuses(r) == ["synced", "synced"]   # 首 synced, 次按状态机 pending→success
    c = _counts(db)
    assert c["media"] == 1 and c["idem_media"] == 1
    with db._connect() as conn:
        st = conn.execute("SELECT status FROM media_evidence WHERE media_id='M1'").fetchone()[0]
    assert st == "success"
    assert c["conflict"] == 0


def test_same_batch_duplicate_idempotent(client):
    db = _get_db(client)
    rec = _media(status="success", upload="success", ok="obj/M1.mp4")
    r = _post(client, [rec, rec.copy()])            # 完全同键同内容
    assert r.status_code == 200
    assert _statuses(r) == ["synced", "already_synced"]  # 幂等消解保持
    c = _counts(db)
    assert c["media"] == 1 and c["idem_media"] == 1 and c["conflict"] == 0


def test_same_batch_duplicate_missing_business_then_unplayable_success(client):
    """边界：批次内第一条 business 缺失(旧兼容)→登记 idem；第二条同键 success 不可播放。
    因 seen.add 先于 business 检查，第二条不得被误判为"首次"而触发整批 400；逐条结果与
    既有 _ingest 顺序行为一致：首 synced(仅登记)，次走 media UPDATE→行缺失→content_mismatch。
    """
    db = _get_db(client)
    first = _media("M1", business_missing=True)                 # content_hash legacy 未校验
    second = _media("M1", status="success", upload="pending", ok=None)  # 同键 success 不可播放
    r = _post(client, [first, second])
    assert r.status_code == 200, "同批同键重复不应被新守卫 400"
    assert _statuses(r) == ["synced", "content_mismatch"]
    c = _counts(db)
    assert c["media"] == 0                 # 首条仅登记(idem), 不写业务行; 次条不补建
    assert c["idem_media"] == 1            # 仅首条登记 idem
    with db._connect() as conn:
        h = conn.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type='media_evidence' AND source_record_id='M1'"
        ).fetchone()[0]
    assert h == "legacy-hash"              # 透传哈希未被次条覆盖
    assert c["conflict"] == 1              # 次条按原逻辑记冲突审计


# ---- 其他业务类型不回归 ----

def test_other_business_types_unchanged(client):
    db = _get_db(client)
    # 多类型合法同批 → 全部 synced
    r = _post(client, [_reading(), _alert()])
    assert r.status_code == 200
    assert _statuses(r) == ["synced", "synced"]
    c = _counts(db)
    assert c["readings"] == 1 and c["idem_reading"] == 1
    # 非法结构仍整批 400、零写入
    bad = _reading("R2"); bad["business"] = {}
    r2 = _post(client, [bad])
    assert r2.status_code == 400


# ---- 并发探针（best-effort，不夸大） ----

def test_concurrent_same_media_id_probe():
    """隔离并发探针：两独立 Database(各自连接)对同一临时 SQLite 并发写同一 media ID。

    - 两实例在主线程序序构建（避免两线程同时跑 schema DDL 在同一新文件上的锁竞争，
      那是 get_db 每请求建库的既有特性，非本次改动范围）。
    - 并发仅发生在 _ingest 写入：库层(c4_idem UNIQUE + media_evidence PK)保证最终
      恰好一行、一登记——只断言这个不变量。
    - 不断言“谁赢/谁 500/锁时序”；若某线程因 SQLite 锁退回 err，如实计入 outcomes。
    """
    import gc
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "conc.db")
    rec = _media("M1", status="success", upload="success", ok="obj/M1.mp4")
    db_a = Database(db_path=path)
    db_b = Database(db_path=path)   # 顺序构建：schema 各自初始化完成，再并发写
    outcomes: list = []
    start = threading.Event()

    def worker(dbi):
        try:
            start.wait(timeout=5)
            outcomes.append(c4._ingest(dbi, dict(rec)))
        except Exception as exc:  # noqa: BLE001 —— 锁竞争等使该线程回落，据实记录
            outcomes.append(f"err:{type(exc).__name__}")

    t1 = threading.Thread(target=worker, args=(db_a,))
    t2 = threading.Thread(target=worker, args=(db_b,))
    t1.start(); t2.start()
    start.set()
    t1.join(timeout=10); t2.join(timeout=10)

    probe = Database(db_path=path)
    with probe._connect() as conn:
        media = conn.execute("SELECT COUNT(*) FROM media_evidence").fetchone()[0]
        idem = conn.execute("SELECT COUNT(*) FROM c4_idem").fetchone()[0]
    # 显式释放连接/触发 GC，避免 Windows 下临时库文件句柄未释放阻塞清理
    gc.collect()
    try:
        tmp.cleanup()
    except Exception:  # noqa: BLE001 —— 清理尽力而为
        pass
    assert media == 1 and idem == 1, f"并发后不变量被破坏: media={media} idem={idem} outcomes={outcomes}"
    assert any(o == "synced" for o in outcomes), f"至少一个请求应成功 synced: {outcomes}"