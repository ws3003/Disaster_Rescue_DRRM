"""V4.4 C4 生产接入 bridge: 业务创建 + C4 分流 的窄职责协调模块 (单事务)。

职责 (尽量窄):
- 接收明确的 record_type 与"真实最终业务字段" dict。
- 在唯一 with_transaction() 内:
  1) 用 db._xxx_write_core(tx, ...) 写业务记录并取最终 ID (source_record_id, 自动/透传);
  2) 用"仅含 canonical 白名单字段"的视图调用 canonical.classify() (同步元数据如 edge_received_at 不入业务哈希);
  3) 有 hash 无 reasons -> db._enqueue_sync_in_tx(tx, ...) (正常 Outbox, 同一事务);
  4) 有 reasons -> db._add_verify_in_tx(tx, ...) (待核验, 同一事务, 不建 Outbox);
  5) 任一 DB 操作失败 -> 整事务由 with_transaction 回滚 (无半成品)。

不做的事: 不开第二连接补写; 不调会自动开事务的 enqueue_sync/add_verify; 不改 canonical 白名单;
不对未确认精度舍入; 不复制风险/告警/阈值逻辑; 不发网络/OSS/Pi; 不承担 worker/轮询; 不静默吞异常。

版本说明: 传入的 version 为 C4 同步/规范化协议版本常量 (协议元数据, 非业务字段), 使明确受控的新记录可哈希;
未持协议的调用方可省略. 本模块不注册任何业务字段小数位 (FIELD_PRECISION 保持空)。
"""

from __future__ import annotations

from typing import Any

from src.database import canonical

# C4 同步/规范化协议版本 (协议元数据, 非业务字段; 与 canonical._HASH_VERSION 对齐)
SYNC_NORM_VERSION = "v1"

# 各类型业务 dict 必须提供的键 (其余键可选; 缺失键由 canonical 的 required 检查兜底)
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "person_event": ("type", "severity", "person_id", "payload"),
    "reading": ("sensor_id", "temperature", "device_captured_at"),
    "sensor_alert": ("type", "occurred_at"),
}


def _write_core(db, tx, record_type: str, business: dict) -> str:
    """在同一连接/事务内写业务记录, 返回最终 source_record_id (自动生成)."""
    if record_type == "person_event":
        return db._event_write_core(
            tx, source_id=None, type=business["type"], severity=business.get("severity"),
            person_id=business.get("person_id"), payload=business.get("payload", {}),
        )
    if record_type == "reading":
        return db._reading_write_core(
            tx, source_id=None, sensor_id=business["sensor_id"],
            temperature=business["temperature"], humidity=business.get("humidity"),
            device_captured_at=business.get("device_captured_at"),
            edge_received_at=business.get("edge_received_at"),
        )
    if record_type == "sensor_alert":
        return db._sensor_alert_write_core(
            tx, source_id=None, type=business["type"], temperature=business.get("temperature"),
            humidity=business.get("humidity"), threshold=business.get("threshold"),
            severity=business.get("severity"), device_captured_at=business.get("device_captured_at"),
            reading_id=business.get("reading_id"), occurred_at=business.get("occurred_at"),
        )
    raise ValueError(f"未知 C4 record_type: {record_type!r}")


def _canonical_view(record_type: str, business: dict) -> dict:
    """构造传入 canonical 的业务视图: 仅含白名单字段 + 完整 payload(事件).

    - person_event: 传完整 payload(含未知键), 使 canonical 检出 unknown_keys 等。
    - reading/sensor_alert: 仅投影白名单字段 (同步元如 edge_received_at 不入业务哈希)。
    """
    wl = canonical.WHITELIST.get(record_type, frozenset())
    if record_type == "person_event":
        payload = business.get("payload") if isinstance(business.get("payload"), dict) else {}
        view = {"payload": payload}
        if "person_id" in business:
            view["person_id"] = business["person_id"]  # 仅当存在才传, 区分缺失 vs 显式 None
        return view
    return {k: business[k] for k in wl if k in business}


def create_sync_record(
    db,
    *,
    record_type: str,
    business: dict,
    version: str | None = SYNC_NORM_VERSION,
    edge_id: str | None = None,
) -> dict:
    """在唯一事务内创建业务记录并按 C4 规范分流; 返回结果 dict。

    result["status"] ∈ {"queued"(已登记Outbox), "verified"(待核验), (异常时抛错)}。
    """
    if record_type not in _REQUIRED_KEYS:
        raise ValueError(f"未知 record_type: {record_type!r} (仅支持 {sorted(_REQUIRED_KEYS)})")
    missing = [k for k in _REQUIRED_KEYS[record_type] if k not in business]
    if missing:
        raise ValueError(f"{record_type} 缺少必要业务字段: {missing}")

    with db.with_transaction() as tx:
        source_record_id = _write_core(db, tx, record_type, business)
        content_hash, reasons = canonical.classify(record_type, _canonical_view(record_type, business),
                                                   version=version)
        if reasons:
            db._add_verify_in_tx(tx, record_type, source_record_id, reasons,
                                 business_json=business, edge_id=edge_id)
            return {"status": "verified", "source_record_id": source_record_id,
                    "reasons": sorted(reasons)}
        key = (record_type, source_record_id)
        result = db._enqueue_sync_in_tx(tx, *key, content_hash, edge_id=edge_id)
        return {"status": "queued", "source_record_id": source_record_id, "result": result}