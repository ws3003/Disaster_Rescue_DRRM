"""C4 本机 HTTP 联调冒烟脚本 (只针对服务端接收端 POST /api/v1/c4/records)。

设计:
- 只在 127.0.0.1:8317 上启动仅挂载 server.c4.router 的最小 FastAPI 应用子进程
  (临时目录运行时代码), 不修改 server/api.py, 不拉 VLM/LLM/assistant 等重模块。
- 服务端数据库通过 DRRM_DB 指向本轮新建临时 SQLite 文件, 完全隔离, 不触 data/drrm.db。
- token 使用随机生成的测试值, 经 C4_API_TOKEN 传入; 全程不打印 token。
- 仅用 stdlib (urllib/sqlite3/subprocess), 不安装依赖, 不访问外部网络。
- 两个服务阶段: 阶段1 无 token(测 A); 阶段2 带 token(测 B-I)。
- 每项打印 PASS/FAIL; finally 终止自启服务并确认退出。
- 任何启动失败/端口占用/隔离失败 -> 立即中止, 不回退真实库。
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

# 最小服务端 app (仅 c4 router); 写入临时目录运行时代码
APP_SRC = (
    "from fastapi import FastAPI\n"
    "from server import c4\n"
    "app = FastAPI()\n"
    "app.include_router(c4.router)\n"
)

_TOKEN = "c4-smoke-" + secrets.token_hex(16)
WORK_DIR = tempfile.mkdtemp(prefix="c4_http_it_")
APP_DIR = WORK_DIR
APP_FILE = os.path.join(APP_DIR, "_c4_app.py")
DB_PATH = os.path.join(WORK_DIR, "c4_it.db")
LOG_PATH = os.path.join(WORK_DIR, "server.log")

_PROCS: list[subprocess.Popen] = []
_results: list[str] = []


# ----------------------------------------------------------------------
# 服务进程管理
# ----------------------------------------------------------------------
def start_server(token: str | None) -> subprocess.Popen:
    env = dict(os.environ)
    env["DRRM_DB"] = DB_PATH
    if token is None:
        env.pop("C4_API_TOKEN", None)
    else:
        env["C4_API_TOKEN"] = token
    log = open(LOG_PATH, "ab")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "_c4_app:app",
         "--host", HOST, "--port", str(PORT), "--app-dir", APP_DIR],
        cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    _PROCS.append(p)
    return p


def stop_all_servers() -> None:
    for p in _PROCS:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
    deadline = time.time() + 10
    for p in _PROCS:
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.1)


def wait_ready(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ----------------------------------------------------------------------
# HTTP 与 DB 检查
# ----------------------------------------------------------------------
def http(method: str, path: str, *, headers: dict | None = None,
         body: dict | None = None) -> tuple[int, dict | None, bool]:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None), False
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        return e.code, (json.loads(raw) if raw.strip() else None), False
    except urllib.error.URLError as e:
        return -1, {"_err": type(e).__name__}, True


def db_count(table: str, where: str, params: tuple) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def db_first_hash(key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type='sensor_alert' AND source_record_id=?",
            (key,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def check(name: str, passed: bool, detail: str) -> None:
    tag = "PASS" if passed else "FAIL"
    _results.append(tag)
    print(f"[{tag}] {name} | {detail}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


H1, H2, H3 = sha("c4-1001-v1"), sha("c4-1001-v2"), sha("it-g1")


# ----------------------------------------------------------------------
# 测试用例
# ----------------------------------------------------------------------
def run_token_unset_case() -> None:
    """阶段1 (无 C4_API_TOKEN): 请求被拒, HTTP 500 INTERNAL_ERROR。"""
    st, body, err = http("POST", "/api/v1/c4/records",
                         headers={"Authorization": "Bearer whatever"},
                         body={"records": [{"record_type": "sensor_alert",
                                            "source_record_id": "IT-A", "content_hash": "h"}]})
    ok = (not err) and st == 500 and body and body.get("code") == "INTERNAL_ERROR"
    check("A: token 未配置 -> 500", ok,
          f"POST /api/v1/c4/records status={st} code={body.get('code') if body else None}")


def run_token_configured_cases() -> None:
    AUTH = {"Authorization": f"Bearer {_TOKEN}"}

    # B: 未提供 Authorization -> 401
    st, body, err = http("POST", "/api/v1/c4/records",
                         body={"records": [{"record_type": "sensor_alert",
                                            "source_record_id": "IT-B", "content_hash": sha("b")}]})
    check("B: 未提供 token -> 401", (not err) and st == 401 and body.get("code") == "UNAUTHORIZED",
          f"status={st} code={body.get('code') if body else None}")

    # C: 错误 token -> 401
    st, body, err = http("POST", "/api/v1/c4/records", headers={"Authorization": "Bearer WRONG"},
                         body={"records": [{"record_type": "sensor_alert",
                                            "source_record_id": "IT-C", "content_hash": sha("c")}]})
    check("C: 错误 token -> 401", (not err) and st == 401 and body.get("code") == "UNAUTHORIZED",
          f"status={st} code={body.get('code') if body else None}")

    # D: 合法单条首次 -> 200 synced
    st, body, err = http("POST", "/api/v1/c4/records", headers=AUTH,
                         body={"records": [{"record_type": "sensor_alert",
                                            "source_record_id": "IT-C4-1001", "content_hash": H1}]})
    ok_d = ((not err) and st == 200 and body.get("code") == "OK"
            and body["data"][0]["status"] == "synced" and body["meta"]["succeeded"] == 1)
    check("D: 合法单条首次 -> synced", ok_d, f"status={st} data_status={body['data'][0]['status'] if body and body.get('data') else None}")
    d_cnt = db_count("c4_idem", "record_type='sensor_alert' AND source_record_id=?", ("IT-C4-1001",))
    check("D: c4_idem 登记 1 行", d_cnt == 1, f"c4_idem count={d_cnt}")

    # H: envelope 字段
    ok_env = (body is not None and {"code", "message", "data", "meta"} <= set(body.keys()))
    check("H: envelope 含 code/message/data/meta", ok_env, f"keys={sorted(body.keys()) if body else None}")

    # E: 同键同内容重提 -> already_synced, 不重复登记
    st2, body2, err2 = http("POST", "/api/v1/c4/records", headers=AUTH,
                            body={"records": [{"record_type": "sensor_alert",
                                               "source_record_id": "IT-C4-1001", "content_hash": H1}]})
    e_cnt = db_count("c4_idem", "record_type='sensor_alert' AND source_record_id=?", ("IT-C4-1001",))
    ok_e = ((not err2) and st2 == 200 and body2["data"][0]["status"] == "already_synced" and e_cnt == 1)
    check("E: 同键同内容 -> already_synced 不重复", ok_e,
          f"status={st2} data_status={body2['data'][0]['status'] if body2.get('data') else None} idem_count={e_cnt}")

    # F: 同键异内容 -> content_mismatch + 冲突审计, 首哈希保留
    st3, body3, err3 = http("POST", "/api/v1/c4/records", headers=AUTH,
                            body={"records": [{"record_type": "sensor_alert",
                                               "source_record_id": "IT-C4-1001", "content_hash": H2}]})
    c_cnt = db_count("c4_content_conflict", "record_type='sensor_alert' AND source_record_id=?", ("IT-C4-1001",))
    kept = db_first_hash("IT-C4-1001")
    ok_f = ((not err3) and st3 == 200 and body3["data"][0]["status"] == "content_mismatch"
            and body3["meta"]["conflicted"] == 1 and body3["meta"]["succeeded"] == 0
            and c_cnt == 1 and kept == H1)
    check("F: 同键异内容 -> content_mismatch 首哈希保留", ok_f,
          f"status={st3} data_status={body3['data'][0]['status'] if body3.get('data') else None} conflict_count={c_cnt} kept_first={kept[:16]}")

    # I: 计数一致 (以 F 响应为准)
    ok_i = (body3["meta"]["total"] == 1 and body3["meta"]["succeeded"] == 0
            and body3["meta"]["conflicted"] == 1 and body3["meta"]["failed"] == 1)
    check("I: succeeded/conflicted/failed 计数一致", ok_i,
          f"meta={body3['meta'] if body3 else None}")

    # G: 批次含非法记录 -> 400, 无部分写入
    st4, body4, err4 = http("POST", "/api/v1/c4/records", headers=AUTH,
                            body={"records": [
                                {"record_type": "sensor_alert", "source_record_id": "IT-G-1", "content_hash": H3},
                                {"record_type": "sensor_alert", "source_record_id": "IT-G-2", "content_hash": ""},
                            ]})
    g_cnt = db_count("c4_idem", "record_type='sensor_alert' AND source_record_id IN (?,?)", ("IT-G-1", "IT-G-2"))
    ok_g = ((not err4) and st4 == 400 and body4.get("code") == "INVALID_PARAMETER" and g_cnt == 0)
    check("G: 批次含非法 -> 400 无部分写入", ok_g,
          f"status={st4} code={body4.get('code') if body4 else None} it-g idem_count={g_cnt}")


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    try:
        Path(APP_FILE).write_text(APP_SRC, encoding="utf-8")
        os.makedirs(WORK_DIR, exist_ok=True)

        # 阶段1: 无 token -> 测 A
        p_none = start_server(None)
        if not wait_ready():
            print("FATAL: 阶段1(无token)服务未就绪, 中止")
            return 1
        run_token_unset_case()
        stop_all_servers()
        if any(p.poll() is None for p in _PROCS):
            print("FATAL: 阶段1服务未退出, 端口可能仍占用, 中止")
            return 1
        _PROCS.clear()

        # 阶段2: 带 token -> 测 B-I
        p_tok = start_server(_TOKEN)
        if not wait_ready():
            print("FATAL: 阶段2(带token)服务未就绪或端口占用, 中止")
            return 1
        run_token_configured_cases()

        print(f"\ntemp work dir: {WORK_DIR}")
        print(f"db: {DB_PATH}")
        print(f"log: {LOG_PATH}")
        failed = _results.count("FAIL")
        print(f"\n== {len(_results) - failed} PASS / {failed} FAIL (共 {len(_results)} 断言组) ==")
        return 0 if failed == 0 else 2
    finally:
        stop_all_servers()


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for p in _PROCS:
            pass