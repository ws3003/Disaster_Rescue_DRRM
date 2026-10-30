"""C4 worker -> 本机 HTTP -> 接收端 端到端联调脚本 (验收 1.8-B)。

目标: 用真实 bridge + OutboxWorker + 真实 requests HTTP, 把业务记录写入 sender.db 的 Outbox,
经 http://127.0.0.1:8317/api/v1/c4/records 发送到接收端 (临时 receiver.db), 并核对两端 SQLite。

场景:
  R2  整数 sensor_alert -> 完整发送成功 (sender outbox=success, receiver c4_idem 登记)
  R2b 成功后再次运行 -> 无待发送任务, 不重复发送 (本地 worker 幂等)
  R2c 同键异内容 -> receiver content_mismatch + 冲突审计, 首哈希保留, sender outbox=dead_letter
  R3  服务端停止(连接拒绝) -> sender outbox=retry_wait, 注入时间核验退避
  R4  小数 reading(Decimal 30.5) -> worker 不发送 -> sender dead_letter(unbuildable) + c4_verify(unparsable),
      receiver 无对应该键登记

安全边界:
  - 仅 127.0.0.1:8317; 不走外部网络; 用随机测试 ID/token, 不打印 token。
  - sender.db / receiver.db 各自独立, 断言路径不同, 且不等于 data/drrm.db。
  - 使用真实 worker 默认 requests 传输(核心发送/解析/状态转换不 mock); 仅在 R2 用一个记录器包裹
    真实 requests 以捕获响应计数(非 mock 逻辑)。
  - 不创建/覆盖 data/drrm.db; 临时 DB/日志保留, 不清理; 不改 schema/canonical/decimalize/FIELD_PRECISION。
  - finally 停止服务并确认退出。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # 使 `src`/`server` 从脚本目录运行时也可导入

HOST = "127.0.0.1"
PORT = 8317
BASE = f"http://{HOST}:{PORT}"
REAL_DB = (REPO_ROOT / "data" / "drrm.db")

from src.database.db import Database
from src.sync import bridge
from src.sync.worker import OutboxWorker

# 最小服务端 app (仅 c4 router), 与 smoke 相同的启动方案
APP_SRC = (
    "from fastapi import FastAPI\n"
    "from server import c4\n"
    "app = FastAPI()\n"
    "app.include_router(c4.router)\n"
)

TOKEN = "e2e-" + secrets.token_hex(16)
WORK_DIR = tempfile.mkdtemp(prefix="c4_e2e_")
APP_DIR = WORK_DIR
APP_FILE = os.path.join(APP_DIR, "_c4_app.py")
RECEIVER_DB = os.path.join(WORK_DIR, "receiver.db")
LOG_PATH = os.path.join(WORK_DIR, "server.log")

_PROC: subprocess.Popen | None = None
_results: list[str] = []


# ----------------------------------------------------------------------
# 服务进程 (复用 smoke 骨架, 不 import smoke)
# ----------------------------------------------------------------------
def start_server() -> subprocess.Popen:
    global _PROC
    env = dict(os.environ)
    env["DRRM_DB"] = RECEIVER_DB
    env["C4_API_TOKEN"] = TOKEN
    log = open(LOG_PATH, "ab")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "_c4_app:app",
         "--host", HOST, "--port", str(PORT), "--app-dir", APP_DIR],
        cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    _PROC = p
    return p


def stop_server() -> None:
    global _PROC
    if _PROC is not None:
        if _PROC.poll() is None:
            try:
                _PROC.terminate()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + 10
        while _PROC.poll() is None and time.time() < deadline:
            time.sleep(0.1)
    _PROC = None


def wait_ready(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
def http_post_urllib(payload: dict, auth: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        BASE + "/api/v1/c4/records",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": auth},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw.strip() else None)
    except urllib.error.URLError as e:
        return -1, {"_err": type(e).__name__}


def db_query(db_path: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def db_count(db_path: str, sql: str, params: tuple = ()) -> int:
    rows = db_query(db_path, sql, params)
    return int(rows[0][0]) if rows else 0


def tziso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check(name: str, passed: bool, detail: str) -> None:
    _results.append("PASS" if passed else "FAIL")
    print(f"[{'PASS' if passed else 'FAIL'}] {name} | {detail}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ----------------------------------------------------------------------
# 发送端准备 (真实 bridge / DAO)
# ----------------------------------------------------------------------
def make_sender(tag: str) -> str:
    return os.path.join(WORK_DIR, f"sender_{tag}.db")


def seed_sensor_alert(db, tag: str) -> str:
    sid = f"E2E-{tag}-" + secrets.token_hex(4).upper()
    r = bridge.create_sync_record(
        db, record_type="sensor_alert",
        business={"type": "sensor_hightemp", "temperature": Decimal(52), "threshold": Decimal(45),
                  "occurred_at": tziso(), "device_captured_at": tziso()})
    if r["status"] != "queued":
        raise RuntimeError(f"sensor_alert 未入 Outbox: {r}")
    return r["source_record_id"]


def seed_fractional_reading(db, tag: str) -> str:
    sid = f"E2E-{tag}-" + secrets.token_hex(4).upper()
    r = bridge.create_sync_record(
        db, record_type="reading",
        business={"sensor_id": "s1", "temperature": Decimal("30.5"), "humidity": Decimal("40"),
                  "device_captured_at": tziso()})
    if r["status"] != "queued":
        raise RuntimeError(f"reading 未入 Outbox: {r}")
    return r["source_record_id"]


def outbox_status(db_path: str, sid: str):
    rows = db_query(db_path, "SELECT * FROM sync_outbox WHERE source_record_id=? ORDER BY id", (sid,))
    return dict(rows[0]) if rows else None


# ----------------------------------------------------------------------
# 场景
# ----------------------------------------------------------------------
def run_r2(token: str) -> str:
    """R2: 整数 sensor_alert 完整发送成功。返回 sid。"""
    db_path = make_sender("a")
    db = Database(db_path)
    sid = seed_sensor_alert(db, "R2")
    ob0 = outbox_status(db_path, sid)
    check("R2: sender outbox 初始 pending", ob0 and ob0["status"] == "pending", f"status={ob0['status'] if ob0 else None}")

    from src.sync.worker import _default_post
    captured: list[tuple[int, dict]] = []

    def _record_post(*a, **k):
        resp = _default_post(*a, **k)  # 真实 requests HTTP
        captured.append((resp.status_code, resp.json()))
        return resp

    worker = OutboxWorker(db, base_url=BASE, token=token, http_post=_record_post, worker_id="e2e-r2")
    expected_hash = worker._hash_for("sensor_alert", sid)[0]
    results = worker.process_batch()
    check("R2: worker 结果 success", results == ["success"], f"results={results}")

    # 真实 HTTP 响应与成功计数
    ok_resp = captured and captured[0][0] == 200 and captured[0][1].get("code") == "OK"
    ok_cnt = captured and captured[0][1]["meta"]["succeeded"] == 1
    ok_item = captured and captured[0][1]["data"][0]["status"] == "synced"
    check("R2: 本机 HTTP 200 + code=OK", ok_resp, f"status={captured[0][0] if captured else None}")
    check("R2: 成功计数 == 1 且 status=synced", ok_cnt and ok_item,
          f"meta={captured[0][1]['meta'] if captured else None} data[0]={captured[0][1]['data'][0] if captured else None}")

    # 接收端登记
    rc = db_count(RECEIVER_DB, "SELECT COUNT(*) FROM c4_idem WHERE record_type='sensor_alert' AND source_record_id=?", (sid,))
    row = db_query(RECEIVER_DB, "SELECT record_type, source_record_id, content_hash FROM c4_idem WHERE source_record_id=?", (sid,))
    ok_reg = rc == 1 and row and row[0][0] == "sensor_alert" and row[0][1] == sid and row[0][2] == expected_hash
    check("R2: receiver c4_idem 登记(类型/sid/hash 匹配)", ok_reg,
          f"count={rc} hash_match={bool(row and row[0][2] == expected_hash)}")

    # 发送端终态
    ob = outbox_status(db_path, sid)
    check("R2: sender outbox -> success", ob and ob["status"] == "success" and ob["completed_at"] is not None,
          f"status={ob['status'] if ob else None}")
    return sid


def run_r2b(token: str, sid: str) -> None:
    """R2b: 成功后再次运行 -> 无待发送任务, 不重复发送。"""
    db_path = make_sender("a")
    db = Database(db_path)
    worker = OutboxWorker(db, base_url=BASE, token=token, worker_id="e2e-r2b")
    results = worker.process_batch()
    check("R2b: 无待发送任务(worker 本地幂等) -> []", results == [], f"results={results}")
    rc = db_count(RECEIVER_DB, "SELECT COUNT(*) FROM c4_idem WHERE source_record_id=?", (sid,))
    check("R2b: receiver c4_idem 仍只 1 行(不重复登记)", rc == 1, f"count={rc}")


def run_r2c(token: str) -> None:
    """R2c: 同键异内容 -> content_mismatch + 冲突审计, 首哈希保留, sender outbox=dead_letter。"""
    db_path = make_sender("b")
    db = Database(db_path)
    sid = seed_sensor_alert(db, "R2C")
    worker = OutboxWorker(db, base_url=BASE, token=token, worker_id="e2e-r2c")
    row_hash = worker._hash_for("sensor_alert", sid)[0]
    first_hash = sha("e2e-r2c-first-" + sid)  # 与 row_hash 不同的合法首哈希

    # 先经合法 HTTP 在接收端登记首个 hash
    st, body = http_post_urllib(
        {"records": [{"record_type": "sensor_alert", "source_record_id": sid, "content_hash": first_hash}]},
        f"Bearer {token}")
    check("R2c: receiver 首哈希登记 -> synced", st == 200 and body["data"][0]["status"] == "synced",
          f"status={st} data_status={body['data'][0]['status'] if body else None}")

    # 再经真实 worker 发送 row_hash(不同内容) -> 触发冲突
    results = worker.process_batch()
    ob = outbox_status(db_path, sid)
    check("R2c: worker 结果 dead(冲突)", results == ["dead"] and ob["last_error"] == "content_mismatch",
          f"results={results} last_error={ob['last_error'] if ob else None}")

    conf = db_count(RECEIVER_DB,
                    "SELECT COUNT(*) FROM c4_content_conflict WHERE record_type='sensor_alert' AND source_record_id=?", (sid,))
    kept = db_query(RECEIVER_DB, "SELECT content_hash FROM c4_idem WHERE source_record_id=?", (sid,))
    check("R2c: receiver 冲突审计 1 行", conf == 1, f"conflict_count={conf}")
    check("R2c: receiver 首哈希保留 + sender dead_letter",
          kept and kept[0][0] == first_hash and ob["status"] == "dead_letter",
          f"kept_first={kept[0][0][:8] if kept else None} sender_status={ob['status'] if ob else None}")


def run_r3() -> None:
    """R3: 服务端已停止(连接拒绝) -> retry_wait; 注入时间核验退避。"""
    db_path = make_sender("c")
    db = Database(db_path)
    sid = seed_sensor_alert(db, "R3")
    FIXED = 5_000_000_000
    worker = OutboxWorker(db, base_url=BASE, token=TOKEN, worker_id="e2e-r3", now_ms=lambda: FIXED)
    results = worker.process_batch()
    ob = outbox_status(db_path, sid)
    ok = results == ["retry"] and ob and ob["status"] == "retry_wait" and ob["retry_count"] == 1 \
        and ob["next_attempt_at"] == FIXED + 5000 and (ob["last_error"] or "").startswith("network")
    check("R3: 连接拒绝 -> retry_wait + 退避(next=now+5000)", ok,
          f"results={results} status={ob['status'] if ob else None} retry={ob['retry_count'] if ob else None} next={ob['next_attempt_at'] if ob else None}")


def run_r4() -> None:
    """R4: 小数 reading(Decimal 30.5) -> 不发送 -> dead_letter(unbuildable) + verify(unparsable), receiver 无登记。"""
    db_path = make_sender("d")
    db = Database(db_path)
    sid = seed_fractional_reading(db, "R4")
    worker = OutboxWorker(db, base_url=BASE, token=TOKEN, worker_id="e2e-r4")
    # worker 从存库 REAL 重算需失败: 确保确实 unbuildable
    h, reasons = worker._hash_for("reading", sid)
    check("R4: worker 无法重建 hash(unparsable)", h is None and "unparsable" in reasons, f"h={h} reasons={sorted(reasons)}")

    results = worker.process_batch()
    ob = outbox_status(db_path, sid)
    check("R4: worker 不发送 -> dead(unbuildable)", results == ["dead"]
          and ob and ob["status"] == "dead_letter" and "unbuildable" in (ob["last_error"] or ""),
          f"results={results} status={ob['status'] if ob else None} last_error={ob['last_error'] if ob else None}")

    vt = db_count(db_path, "SELECT COUNT(*) FROM c4_verify WHERE record_type='reading' AND source_record_id=?", (sid,))
    vreason = db_query(db_path, "SELECT reasons FROM c4_verify WHERE source_record_id=?", (sid,))
    import json as _json
    ok_verify = vt == 1 and vreason and "unparsable" in _json.loads(vreason[0][0])
    check("R4: sender c4_verify 记 unparsable", ok_verify, f"verify_count={vt} reasons={vreason[0][0] if vreason else None}")

    rc = db_count(RECEIVER_DB, "SELECT COUNT(*) FROM c4_idem WHERE source_record_id=?", (sid,)) if os.path.exists(RECEIVER_DB) else 0
    check("R4: receiver 无该键登记(未发送)", rc == 0, f"receiver idem count={rc}")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    try:
        # 隔离前提: 两端库路径不同且非真实库
        check("前置: sender/receiver 路径不同", make_sender("a") != RECEIVER_DB, "互不共享文件")
        check("前置: 不含 data/drrm.db", str(REAL_DB) not in (make_sender("a"), RECEIVER_DB),
              f"real_db={REAL_DB}")

        Path(APP_FILE).write_text(APP_SRC, encoding="utf-8")

        start_server()
        if not wait_ready():
            print("FATAL: C4-only 服务未就绪, 中止")
            return 1

        r2_sid = run_r2(TOKEN)
        run_r2b(TOKEN, r2_sid)
        run_r2c(TOKEN)

        stop_server()
        check("服务已停止(端口不再监听)", not port_open(), "127.0.0.1:8317 无监听")

        run_r3()   # 服务端已停 -> 连接拒绝
        run_r4()   # 不发送

        stop_server()
        print(f"\nwork dir: {WORK_DIR}")
        print(f"sender dbs: {WORK_DIR}/sender_{'{a,b,c,d}'}.db")
        print(f"receiver db: {RECEIVER_DB}")
        print(f"log: {LOG_PATH}")
        failed = _results.count("FAIL")
        print(f"\n== {len(_results) - failed} PASS / {failed} FAIL (共 {len(_results)} 断言组) ==")
        return 0 if failed == 0 else 2
    finally:
        stop_server()


if __name__ == "__main__":
    sys.exit(main())