"""server/c4.py — C4 服务端接收接口（POST /api/v1/c4/records）。

Pi 侧 Outbox worker 将待同步记录投递至此; 服务端做幂等接收(c4_idem)与同键内容核验。
- 认证: 环境变量 C4_API_TOKEN(Bearer); 缺 token/错误 token → 401; 未配置 → 500(配置错误), 不静默放行。
- 返回沿用四字段信封 {code, message, data, meta}; data 为逐条 (record_type, source_record_id, status, error_code?)。
- 逐条 status: synced / already_synced / content_mismatch。
- 复用现有数据库与 C4 表(c4_idem / c4_content_conflict), 不改 schema; 单一事务内登记与核验。
- 本仓库为单库模型("服务端读取的只读副本 == 权威库"); 真实双主机部署需在接收侧另存只读副本(超出本仓库范围, 记录为限制)。
"""

from __future__ import annotations

import hmac
import json as _json
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from src.database import canonical as _canonical
from src.database.db import Database
from src.sync import bridge as _bridge

router = APIRouter(prefix="/api/v1")

_CODES = {
    "INVALID_PARAMETER": "参数非法",
    "UNAUTHORIZED": "认证缺失或无效",
    "INTERNAL_ERROR": "服务端内部错误",
}

_C4_RECORD_TYPES = frozenset({"person_event", "reading", "sensor_alert", "media_evidence"})

# 需携带业务本体的记录类型（writing 业务只读副本到服务端库 readings/sensor_alerts）
_BUSINESS_CARRIERS = frozenset({"reading", "sensor_alert", "person_event", "media_evidence"})

# 各类型的必填业务字段（缺失 → 整批 400，不产生半写入）
_BUSINESS_REQUIRED = {
    "reading": frozenset({"sensor_id", "temperature", "device_captured_at"}),
    "sensor_alert": frozenset({"type", "occurred_at"}),
    "person_event": frozenset({"type", "ts"}),
    "media_evidence": frozenset({"status", "ref_id", "media_type", "source"}),
}


def get_db() -> Database:
    return Database(db_path=os.environ.get("DRRM_DB", "data/drrm.db"))


def _require_c4_token(authorization: Optional[str]) -> None:
    token = (os.environ.get("C4_API_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="service misconfigured: C4_API_TOKEN unset")
    expected = f"Bearer {token}".encode("utf-8")
    provided = (authorization or "").encode("utf-8")
    if not authorization or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _ok_json(data: Any, meta: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(content={"code": "OK", "message": "ok",
                                 "data": data, "meta": meta if meta is not None else {}})


def _fail_json(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(status_code=http_status,
                        content={"code": code, "message": message, "data": None, "meta": {}})


def _is_finite_number(value: Any) -> bool:
    """数值必须为 int/float/Decimal/十进制字符串、非 bool、有限（不允许 NaN/±Inf）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        try:
            import decimal as _dec
            d = _dec.Decimal(value.strip())
            d_float = float(d)
        except Exception:  # noqa: BLE001
            return False
        return math.isfinite(d_float)
    return False


def _validate_business(rt: str, business: Any) -> None:
    """业务本体校验：
    - person_event 不要求 business（延续旧协议）。
    - reading/sensor_alert：business 缺失视为"旧客户端"（只登记 c4_idem，不写业务副本，向后兼容）；
      若提供则必须为 dict 且含必填字段、数值合法（否则 400，不产生写入）。
    """
    if rt not in _BUSINESS_CARRIERS:
        return
    if business is None:
        return  # 旧客户端：无业务本体 → 仅幂等登记，兼容
    if not isinstance(business, dict):
        raise HTTPException(status_code=400, detail=f"{rt} business 必须为对象")
    missing = [k for k in _BUSINESS_REQUIRED[rt] if k not in business]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"{rt} business 缺少字段: {sorted(missing)}")
    # 数值/时间合法性
    num_fields = {"temperature"} | ({"humidity", "threshold"} if rt == "sensor_alert" else set())
    for k in num_fields:
        v = business.get(k)
        if v is None:
            continue
        if not _is_finite_number(v):
            raise HTTPException(status_code=400, detail=f"{rt}.business.{k} 非法数值")
    for k in ("device_captured_at", "occurred_at"):
        v = business.get(k)
        if isinstance(v, str) and not v.strip():
            raise HTTPException(status_code=400, detail=f"{rt}.business.{k} 为空")


def _validate_record(rec: Dict[str, Any]) -> None:
    """单条记录字段级校验; 非法抛 HTTP 400, 不产生任何写入。

    整批接收在 ingest 前先对全部记录执行本校验, 使非法请求整体被拒绝,
    不留下"已同步若干条 + 失败若干条"的半写入成功路径。
    """
    rt = rec.get("record_type")
    sid = rec.get("source_record_id")
    ch = rec.get("content_hash")
    if rt not in _C4_RECORD_TYPES or not isinstance(sid, str) or not sid or not isinstance(ch, str) or not ch:
        raise HTTPException(status_code=400, detail="invalid record fields")
    _validate_business(rt, rec.get("business"))
    if rt == "media_evidence" and isinstance(rec.get("business"), dict):
        # media：INSERT/UPDATE 共用字段校验（含 media_id==sid、source、media_type、枚举、ref_id）
        _media_validate(rec["business"], sid)


def _hash_of_business(rt: str, business: Dict[str, Any]):
    """用与 Pi 相同的 canonical 规则，从业务本体重算 hash（校验与 content_hash 一致）。"""
    if not isinstance(business, dict):
        return None, {"invalid"}
    view = _bridge._canonical_view(rt, business)
    return _canonical.classify(rt, view, version=_bridge.SYNC_NORM_VERSION)


MEDIA_STATUS_MP = {
    "pending": {"pending", "processing", "success", "failed-awaiting-retry"},
    "processing": {"processing", "success", "failed-awaiting-retry"},
    "success": {"success"},
    "failed-awaiting-retry": {"failed-awaiting-retry", "success"},
}
MEDIA_UPLOAD_MP = {
    None: {None, "pending", "uploading", "success", "failed"},   # NULL 初始：可保持/前进
    "pending": {"pending", "uploading", "success", "failed"},
    "uploading": {"uploading", "success", "failed"},
    "success": {"success"},
    "failed": {"failed", "success"},
}


def _media_playable(status, upload_status, object_key) -> bool:
    """唯一可播放条件：status==success ∧ upload_status==success ∧ object_key 非空。"""
    return bool(status == "success" and upload_status == "success" and object_key)


def _media_validate(business: Any, sid: str) -> None:
    """media 业务校验（INSERT/UPDATE 共用）。business 缺失=旧客户端兼容（应已由 _validate_business 处理）。"""
    if not isinstance(business, dict):
        raise HTTPException(400, "media business 必须为对象")
    if business.get("media_id") != sid:
        raise HTTPException(400, "media business.media_id 与 source_record_id 不一致")
    if business.get("source") not in ("sensor", "person"):
        raise HTTPException(400, "media source 必须为 sensor/person")
    if business.get("media_type") not in ("image", "video"):
        raise HTTPException(400, "media media_type 必须为 image/video")
    st = business.get("status")
    if st not in MEDIA_STATUS_MP:
        raise HTTPException(400, f"非法 media status: {st!r}")
    up = business.get("upload_status")
    if up is not None and up not in MEDIA_UPLOAD_MP:
        raise HTTPException(400, f"非法 media upload_status: {up!r}")
    if not business.get("ref_id"):
        raise HTTPException(400, "media ref_id 必填")


def _media_conflict(tx, rt: str, sid: str, old_hash: str, new_hash: str,
                    now: int, edge: Any) -> str:
    """媒体 illegal/revert 冲突：记审计、不推进 idem hash。返回 content_mismatch。"""
    tx.execute(
        """
        INSERT INTO c4_content_conflict
            (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (rt, sid, old_hash, new_hash, now, edge),
    )
    return "content_mismatch"


def _media_first_receive_guard(db, rec: Dict[str, Any], seen: set) -> None:
    """批次预校验：媒体首收 status=success 需可播放（INSERT 期强制，整批400、零写入）。

    在 receive 批次预校验阶段、任何 _ingest 之前对所有 media 记录执行，保证违反
    "首收 success 需可播放" 的批次整体 400、不产生半写入（修复 batch 原子性契约）。

    分层语义（与 _ingest 首收 playable 守卫条件逐条一致，_ingest 守卫保留为纵深防御）：
    1. 非 media_evidence 直接返回。
    2. key=(record_type, source_record_id)；批次内已出现同键 → 放行交还 _ingest
       （幂等/UPDATE/content_mismatch 由原逻辑决定），不去重/排序/合并/覆盖。
    3. seen.add 先于 business 检查：business 缺失的旧兼容首条也会标记同键已见，
       避免其后的同键 success 记录被误判为"首次"。
    4. business 非 dict → 保持旧客户端兼容，返回（_ingest 只登记 idem，不写副本）。
    5. status != success → 不触发 playable（pending/processing/failed-awaiting-retry 不误拒）。
    6. 只读查询 c4_idem：已登记 → 交还 _ingest（保护 UPDATE 的 content_mismatch，不补建业务行）。
    7. 复用 _media_playable（与 _ingest :301 守卫同输入），不可播放 ⇒ 整批 HTTP 400。
    """
    if rec.get("record_type") != "media_evidence":
        return
    key = (rec["record_type"], rec["source_record_id"])
    if key in seen:
        return
    seen.add(key)
    business = rec.get("business")
    if not isinstance(business, dict):
        return
    if business.get("status") != "success":
        return
    with db._connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM c4_idem WHERE record_type = ? AND source_record_id = ? LIMIT 1",
            (rec["record_type"], rec["source_record_id"]),
        ).fetchone() is not None
    if exists:
        return
    if not _media_playable(business.get("status"), business.get("upload_status"),
                           bool(business.get("object_key"))):
        raise HTTPException(status_code=400,
                            detail="media 首收 status=success 需 upload_status=success 且 object_key 非空")


def _write_business_copy(tx, rt: str, sid: str, business: Dict[str, Any], recv_iso: str) -> None:
    """在调用方事务内写 Windows 只读业务副本（readings / sensor_alerts）。

    同一事务与 c4_idem 登记一起提交；失败则整体回滚（不出现"已登记但无副本/有副本未登记"）。
    """
    if rt == "reading":
        tx.execute(
            """
            INSERT INTO readings
              (reading_id, sensor_id, temperature, humidity, device_captured_at,
               edge_received_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("sensor_id"), business.get("temperature"),
             business.get("humidity"), business.get("device_captured_at"),
             business.get("edge_received_at"), recv_iso),
        )
    elif rt == "sensor_alert":
        tx.execute(
            """
            INSERT INTO sensor_alerts
              (alert_id, type, severity, temperature, humidity, threshold, status,
               occurred_at, resolved_at, device_captured_at, received_at, reading_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("type"), business.get("severity"),
             business.get("temperature"), business.get("humidity"), business.get("threshold"),
             business.get("status", "active"), business.get("occurred_at"),
             business.get("resolved_at"), business.get("device_captured_at"),
             recv_iso, business.get("reading_id")),
        )
    elif rt == "person_event":
        payload = business.get("payload")
        payload_json = _json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None
        tx.execute(
            """
            INSERT INTO events
              (event_id, ts, type, severity, person_id, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("ts"), business.get("type"), business.get("severity"),
             business.get("person_id"), payload_json),
        )
    elif rt == "media_evidence":
        # 服务端媒体行：local_path 置空串（NOT NULL 占位，不接收 Pi 本地路径）；
        # object_key/uploaded_at 仅在上传成功后出现；不存 URL/签名/密钥。
        tx.execute(
            """
            INSERT INTO media_evidence
              (media_id, ref_id, source, media_type, status, upload_status,
               object_key, captured_at, created_at, uploaded_at, local_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("ref_id"), business.get("source"), business.get("media_type"),
             business.get("status"), business.get("upload_status"),
             business.get("object_key"), business.get("captured_at"),
             recv_iso, business.get("uploaded_at"), ""),
        )


def _ingest(db: Database, rec: Dict[str, Any]) -> str:
    """单条幂等接收+业务副本落地。

    - 无登记 → (业务合法且 hash 复核一致) 同一事务登记 c4_idem 并写业务副本 → synced；
      业务缺失(旧客户端/person_event) → 仅登记 → synced（兼容，不写副本）。
    - 同hash → already_synced（不重复入库）。
    - 异hash / hash 复核不一致 → content_mismatch（冲突审计，不覆盖首收，不写副本）。
    """
    _validate_record(rec)
    rt = rec.get("record_type")
    sid = rec.get("source_record_id")
    ch = rec.get("content_hash")
    edge = rec.get("edge_id")
    business = rec.get("business")
    now = int(__import__("time").time() * 1000)
    recv_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    with db.with_transaction() as tx:
        row = tx.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type=? AND source_record_id=?",
            (rt, sid),
        ).fetchone()
        if row is None:
            # 首收：若携带业务本体，则复核 hash；不一致 → 记冲突、不写副本
            if isinstance(business, dict):
                _h, _why = _hash_of_business(rt, business)
                if not _h or _h != ch:
                    tx.execute(
                        """
                        INSERT INTO c4_content_conflict
                          (record_type, source_record_id, content_hash, conflict_hash,
                           detected_at, edge_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (rt, sid, ch, _h or "", now, edge),
                    )
                    return "content_mismatch"
            tx.execute(
                "INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at, edge_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (rt, sid, ch, now, edge),
            )
            # 首收 media：status=success 需可播放（INSERT 期强制）
            if rt == "media_evidence" and isinstance(business, dict) \
                    and business.get("status") == "success" \
                    and not _media_playable(business.get("status"), business.get("upload_status"), bool(business.get("object_key"))):
                raise HTTPException(status_code=400,
                                    detail="media 首收 status=success 需 upload_status=success 且 object_key 非空")
            # 同事务写 Windows 只读业务副本（合法业务本体；person_event 不写）
            if rt in _BUSINESS_CARRIERS and isinstance(business, dict):
                _write_business_copy(tx, rt, sid, business, recv_iso)
            return "synced"
        if row["content_hash"] == ch:
            return "already_synced"
        # sensor_alert 生命周期：active -> resolved 等死亡量使 hash 变化，应更新既有业务副本
        # （单调推进，不回退；幂等 hash 同步前移以保证重复发送转 already_synced）。
        if rt == "sensor_alert" and isinstance(business, dict):
            _h, _why = _hash_of_business(rt, business)
            if not _h or _h != ch:
                tx.execute(
                    """
                    INSERT INTO c4_content_conflict
                        (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rt, sid, row["content_hash"], ch, now, edge),
                )
                return "content_mismatch"
            cur = tx.execute(
                "SELECT status FROM sensor_alerts WHERE alert_id = ?", (sid,)
            ).fetchone()
            new_status = str(business.get("status") or "active").lower()
            # 单调守卫：已 resolved 的副本不允许被旧 active 回退。
            if cur is not None and cur["status"] == "resolved" and new_status != "resolved":
                tx.execute(
                    """
                    INSERT INTO c4_content_conflict
                        (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rt, sid, row["content_hash"], ch, now, edge),
                )
                return "content_mismatch"
            tx.execute(
                """
                UPDATE sensor_alerts
                SET status = ?, resolved_at = ?
                WHERE alert_id = ?
                """,
                (new_status, business.get("resolved_at"), sid),
            )
            tx.execute(
                "UPDATE c4_idem SET content_hash = ? WHERE record_type = ? AND source_record_id = ?",
                (ch, rt, sid),
            )
            return "synced"
        if rt == "media_evidence":
            if not isinstance(business, dict):
                return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            _h, _why = _hash_of_business(rt, business)
            if not _h or _h != ch:
                return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            cur = tx.execute(
                "SELECT * FROM media_evidence WHERE media_id = ?", (sid,)
            ).fetchone()
            if cur is None:
                # idem 存在但媒体业务记录缺失：不自动补建，记冲突
                return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            # ref_id 不可变
            if (business.get("ref_id") is not None) and business["ref_id"] != cur["ref_id"]:
                return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            # status 状态机（含 keep；success 终态）
            new_s = business.get("status", cur["status"])
            if new_s not in MEDIA_STATUS_MP[cur["status"]]:
                return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            # upload_status：缺失→保留现值；显式 NULL/具体值→走状态机；success 终态不回退
            has_u = "upload_status" in business
            cur_u = cur["upload_status"]
            new_u = business["upload_status"] if has_u else cur_u
            eff_u = cur_u
            if has_u:
                if cur_u == "success":
                    if new_u != "success":
                        return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
                    eff_u = "success"
                else:
                    if new_u not in MEDIA_UPLOAD_MP[cur_u]:
                        return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
                    eff_u = new_u
            # object_key / uploaded_at：缺失→保留；已成功态禁止显式清空
            has_ok = "object_key" in business
            new_ok = business["object_key"] if has_ok else cur["object_key"]
            has_ua = "uploaded_at" in business
            new_ua = business["uploaded_at"] if has_ua else cur["uploaded_at"]
            if eff_u == "success":
                if has_ok and new_ok is None:
                    return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
                if has_ua and new_ua is None:
                    return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            # status=success 可播放（投影后有效态）
            proj_play = _media_playable(new_s, eff_u, new_ok)
            if new_s == "success" and not proj_play:
                return _media_conflict(tx, rt, sid, row["content_hash"], ch, now, edge)
            # 动态 SET：缺失字段不加列（保留现值）
            set_cols, vals = ["status=?"], [new_s]
            if has_u:
                set_cols.append("upload_status=?"); vals.append(eff_u)
            if has_ok:
                set_cols.append("object_key=?"); vals.append(new_ok)
            if has_ua:
                set_cols.append("uploaded_at=?"); vals.append(new_ua)
            set_cols.append("last_error=?")
            vals.append(None if new_s == "success" else business.get("last_error"))
            vals.append(sid)
            tx.execute(
                "UPDATE media_evidence SET " + ", ".join(set_cols) + " WHERE media_id = ?",
                vals,
            )
            tx.execute(
                "UPDATE c4_idem SET content_hash = ? WHERE record_type = ? AND source_record_id = ?",
                (ch, rt, sid),
            )
            return "synced"
        tx.execute(
            """
            INSERT INTO c4_content_conflict
                (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rt, sid, row["content_hash"], ch, now, edge),
        )
        return "content_mismatch"


@router.post("/c4/records")
def receive(
    payload: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(get_db),
) -> JSONResponse:
    try:
        _require_c4_token(authorization)
    except HTTPException as exc:
        code = "UNAUTHORIZED" if exc.status_code == 401 else "INTERNAL_ERROR"
        return _fail_json(code, exc.detail, exc.status_code)

    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        return _fail_json("INVALID_PARAMETER", "records must be non-empty list", 400)

    # 整批预校验: 任一条非法即整体 400, 不产生任何半写入 (不留下"部分已同步"路径)
    try:
        for rec in records:
            if not isinstance(rec, dict):
                raise HTTPException(status_code=400, detail="invalid record fields")
            _validate_record(rec)
        # 媒体首收 playable 语义预校验：任何 _ingest 前整批完成；失败整批400、零写入
        seen: set = set()
        for rec in records:
            _media_first_receive_guard(db, rec, seen)
    except HTTPException as exc:
        return _fail_json("INVALID_PARAMETER", exc.detail, 400)

    per: List[Dict[str, Any]] = []
    for rec in records:
        try:
            status = _ingest(db, rec)
            per.append({
                "record_type": rec.get("record_type"),
                "source_record_id": rec.get("source_record_id"),
                "status": status,
                "error_code": None,
            })
        except HTTPException as exc:  # noqa: BLE001
            return _fail_json("INVALID_PARAMETER", exc.detail, 400)
        except Exception:  # noqa: BLE001
            return _fail_json("INTERNAL_ERROR", "ingest failed", 500)

    # 诚实统计: content_mismatch 属可辨认冲突结果, 不计入"成功同步"
    succeeded = sum(1 for p in per if p["status"] in ("synced", "already_synced"))
    conflicted = sum(1 for p in per if p["status"] == "content_mismatch")
    return _ok_json(per, {"total": len(per), "succeeded": succeeded,
                          "conflicted": conflicted, "failed": len(per) - succeeded})
