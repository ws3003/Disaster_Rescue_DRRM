"""V4.4 1.9-B 正式 FastAPI 应用隔离集成测试 (server.api:app)。

目标: 以真正的 `server.api:app`(非 c4-only 最小 app) 在本机隔离环境验证 C4:
  F1 隔离路径前置断言; F2 正式应用启动; F3 OpenAPI 路由发现; F4 缺 Token -> 401;
  F5 错 Token -> 401; F6 合法 C4 投递; F7 临时库登记正确; F8 正式库未受影响;
  F9 服务退出; F10 端口释放。

安全边界:
  - 仅 127.0.0.1:8317; 用随机测试 token/ID, 不打印 token。
  - 服务端仅以 DRRM_DB=临时库 + C4_API_TOKEN=随机 隔离; 数据库路径与 data/drrm.db 绝对不同。
  - 只调用 /openapi.json 一次(路由发现, 纯生成) 与 /api/v1/c4/records; 不调任何 V4.3 业务端点。
  - 正式库只读(URI mode=ro)核查本轮唯一键, 不创建/初始化/修改它。
  - 使用真实 python 解释器启动 uvicorn server.api:app(cwd=repo root)。
  - finally 终止自启子进程并确认退出 + 端口释放; 不清理临时产物。
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
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8317
BASE = f"http://{HOST}:{PORT}"
REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DB = REPO_ROOT / "data" / "drrm.db"

TOKEN = "formal-" + secrets.token_hex(16)
WORK_DIR = tempfile.mkdtemp(prefix="c4_formal_it_")
RECV_DB = os.path.join(WORK_DIR, "receiver_formal_it.db")
LOG_PATH = os.path.join(WORK_DIR, "server.log")

_PROC: subprocess.Popen | None = None
_results: list[str] = []
TEST_KEY: str = "FX-" + secrets.token_hex(8).upper()


def check(name: str, passed: bool, detail: str) -> None:
    _results.append("PASS" if passed else "FAIL")
    print(f"[{'PASS' if passed else 'FAIL'}] {name} | {detail}")


# ---------------- 服务进程 ----------------
def start_server() -> None:
    global _PROC
    env = dict(os.environ)
    env["DRRM_DB"] = RECV_DB
    env["C4_API_TOKEN"] = TOKEN
    log = open(LOG_PATH, "ab")
    _PROC = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server.api:app",
         "--host", HOST, "--port", str(PORT)],
        cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def stop_server() -> None:
    global _PROC
    if _PROC is not None:
        if _PROC.poll() is None:
            _PROC.terminate()
        try:
            _PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _PROC.kill()
            _PROC.wait(timeout=5)
    _PROC = None


def wait_ready(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port_closed(timeout: float = 8.0) -> bool:
    """有界等待监听端口释放(子进程 terminate 后 socket 释放可能稍滞后)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open():
            return True
        time.sleep(0.3)
    return not port_open()


# ---------------- HTTP ----------------
def request(method: str, path: str, *, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None), False
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw.strip() else None), False
    except urllib.error.URLError as e:
        return -1, {"_err": type(e).__name__}, True


# ---------------- DB ----------------
def db_query(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------- 场景 ----------------
def main() -> int:
    try:
        mtime_before = os.path.getmtime(REAL_DB) if REAL_DB.exists() else None

        # F1 隔离路径前置
        recv_abs = os.path.abspath(RECV_DB)
        real_abs = os.path.abspath(REAL_DB)
        check("F1: 临时库与 data/drrm.db 绝对路径不同", recv_abs != real_abs,
              f"recv={recv_abs} real={real_abs}")
        check("F1: 本机回环 127.0.0.1", HOST == "127.0.0.1", HOST)

        # F2 启动正式应用
        start_server()
        if not wait_ready():
            tail = ""
            if Path(LOG_PATH).exists():
                tail = Path(LOG_PATH).read_text(encoding="utf-8", errors="replace")[-1500:]
            print("FATAL: 正式应用启动失败/超时; 日志尾部:\n" + tail)
            check("F2: 正式应用启动", False, "未就绪")
            return 1
        check("F2: 正式应用启动(server.api:app)", True, f"listen 127.0.0.1:{PORT}")

        # F3 OpenAPI 路由发现
        st, body, err = request("GET", "/openapi.json")
        paths = (body or {}).get("paths", {}) if isinstance(body, dict) else {}
        c4 = paths.get("/api/v1/c4/records")
        ok3 = (not err) and st == 200 and c4 is not None and "post" in c4
        check("F3: OpenAPI 存在 POST /api/v1/c4/records", ok3,
              f"status={st} present={c4 is not None} post={'post' in c4 if c4 else None}")

        # F4 缺少 Token
        st4, b4, e4 = request("POST", "/api/v1/c4/records",
                              body={"records": [{"record_type": "sensor_alert",
                                                 "source_record_id": TEST_KEY, "content_hash": "h"}]})
        check("F4: 缺 Token -> 401 UNAUTHORIZED",
              (not e4) and st4 == 401 and (b4 or {}).get("code") == "UNAUTHORIZED",
              f"status={st4} code={(b4 or {}).get('code') if b4 else None}")

        # F5 错误 Token
        st5, b5, e5 = request("POST", "/api/v1/c4/records",
                              headers={"Authorization": "Bearer WRONG-x"},
                              body={"records": [{"record_type": "sensor_alert",
                                                 "source_record_id": TEST_KEY, "content_hash": "h"}]})
        check("F5: 错 Token -> 401 UNAUTHORIZED",
              (not e5) and st5 == 401 and (b5 or {}).get("code") == "UNAUTHORIZED",
              f"status={st5} code={(b5 or {}).get('code') if b5 else None}")

        # F6 合法投递
        good_hash = hashlib.sha256(("formal-ok-" + TEST_KEY).encode()).hexdigest()
        st6, b6, e6 = request("POST", "/api/v1/c4/records",
                              headers={"Authorization": f"Bearer {TOKEN}"},
                              body={"records": [{"record_type": "sensor_alert",
                                                 "source_record_id": TEST_KEY, "content_hash": good_hash}]})
        ok6 = ((not e6) and st6 == 200 and (b6 or {}).get("code") == "OK"
               and b6["data"][0]["status"] == "synced" and b6["meta"]["succeeded"] == 1)
        check("F6: 合法投递 -> 200 OK synced succeeded=1", ok6,
              f"status={st6} code={(b6 or {}).get('code') if b6 else None} "
              f"data={(b6 or {}).get('data') if b6 else None} meta={(b6 or {}).get('meta') if b6 else None}")

        # F7 临时库登记
        rows = db_query(RECV_DB,
                        "SELECT record_type, source_record_id, content_hash FROM c4_idem WHERE source_record_id=?",
                        (TEST_KEY,))
        ok7 = len(rows) == 1 and rows[0][0] == "sensor_alert" and rows[0][1] == TEST_KEY \
            and rows[0][2] == good_hash
        check("F7: 临时库 c4_idem 单条 + hash 匹配", ok7,
              f"count={len(rows)} hash_match={bool(rows and rows[0][2] == good_hash)}")

        # 再次投递同键同内容 -> already_synced(幂等) 且临时库仍 1 行
        st7b, b7b, e7b = request("POST", "/api/v1/c4/records",
                                 headers={"Authorization": f"Bearer {TOKEN}"},
                                 body={"records": [{"record_type": "sensor_alert",
                                                    "source_record_id": TEST_KEY, "content_hash": good_hash}]})
        rows2 = db_query(RECV_DB, "SELECT COUNT(*) FROM c4_idem WHERE source_record_id=?", (TEST_KEY,))
        ok7b = (not e7b) and st7b == 200 and b7b["data"][0]["status"] == "already_synced" and rows2[0][0] == 1
        check("F7b: 重复投递 already_synced, 临时库仍 1 行", ok7b,
              f"status={st7b} data_status={(b7b or {}).get('data') and b7b['data'][0]['status']} count={rows2[0][0]}")

        # F8 正式库未受影响(只读)
        real_rows_msg = "正式库无 C4 表, 本轮记录不可能写入"
        real_ok = True
        try:
            conn = sqlite3.connect(f"file:{real_abs}?mode=ro", uri=True)
            try:
                has = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('c4_idem','c4_content_conflict')")]
                if has:
                    n = conn.execute("SELECT COUNT(*) FROM c4_idem WHERE source_record_id=?",
                                     (TEST_KEY,)).fetchone()[0]
                    real_rows_msg = f"正式库存在 C4 表, 本轮键 count={n}"
                    real_ok = n == 0
                else:
                    real_ok = True
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            real_rows_msg = f"正式库只读核查出错(保留现场): {exc}"
            real_ok = False
        check("F8: 正式 data/drrm.db 无本轮记录", real_ok, real_rows_msg)

        mtime_after = os.path.getmtime(REAL_DB) if REAL_DB.exists() else None
        check("F8: 正式库 mtime 测试前后一致", mtime_before is not None and mtime_before == mtime_after,
              f"before={mtime_before} after={mtime_after}")

        # F9 服务退出
        stop_server()
        check("F9: 服务进程已退出", _PROC is None, "terminate+wait 完成")

        # F10 端口释放(有界等待, 避免 terminate 后 socket 释放瞬时滞后误报)
        check("F10: 端口 127.0.0.1:8317 已释放", wait_port_closed(), "无监听")

        print(f"\nwork dir: {WORK_DIR}")
        print(f"receiver db: {RECV_DB}")
        print(f"log: {LOG_PATH}")
        failed = _results.count("FAIL")
        print(f"\n== {len(_results) - failed} PASS / {failed} FAIL (共 {len(_results)} 断言组) ==")
        return 0 if failed == 0 else 2
    finally:
        stop_server()


if __name__ == "__main__":
    sys.exit(main())