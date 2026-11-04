"""V4.4 C4-01 阶段 2: 确定性业务内容规范化 + 7 稳定原因码 + canonical 哈希。

职责:
- 依字段类型白名单生成确定性业务内容哈希。
- 识别待核验原因(`unversioned/missing_required/unparsable/unknown_keys/
  version_mismatch/legacy_ts`)。`content_conflict` 只在"同键已可靠判异"的比较阶段产生,
  不在本项目由 classify 产出。
- 纯函数, 无数据库访问, 不连接任何事务/连接。
- 不迁移、不回填历史数据; 已丢精度不宣称恢复。

范围说明(阶段 2 会话确认):
- events.payload 哈希白名单 = {state, risk_score, priority, timestamp}。
- frame_idx/title/message 为已知非业务字段: 不参与哈希、不触发 unknown_keys、
  在原始 business_json 完整保留(不裁剪)。
- trigger_type/severity 等白名单外键存在 -> 进入待核验(unknown_keys), 不自动合并。
- 数值: 偏好 Decimal/整数/十进制字符串; float 仅当其整数值可精确表示时接受, 否则 unparsable。
- 时间: 带时区->UTC 规范化; 无时区保留字面(不猜时区); 超微秒无法无损 -> unparsable。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# 7 个稳定原因码 (集合, 多原因可并存)
REASONS = frozenset({
    "unversioned",
    "missing_required",
    "unparsable",
    "unknown_keys",
    "version_mismatch",
    "legacy_ts",
    "content_conflict",
})

_UNP = object()          # 规范化失败哨兵
_HASH_VERSION = "v1"

# 字段白名单 (每拥有型: 参与业务内容哈希的字段)
WHITELIST: dict[str, frozenset] = {
    "person_event": frozenset({"state", "risk_score", "priority", "timestamp", "person_id"}),
    "reading": frozenset({"sensor_id", "temperature", "humidity", "device_captured_at"}),
    "sensor_alert": frozenset({
        "type", "temperature", "humidity", "threshold",
        "occurred_at", "device_captured_at", "reading_id",
        "status", "resolved_at",
    }),
    "media_evidence": frozenset({
        "media_id", "media_type", "ref_id", "source",
        "status", "upload_status", "object_key", "captured_at",
    }),
}

# 每拥有型必填字段(缺失 -> missing_required)
REQUIRED: dict[str, frozenset] = {
    "person_event": frozenset({"timestamp"}),
    "reading": frozenset({"sensor_id", "device_captured_at"}),
    "sensor_alert": frozenset({"type", "occurred_at"}),
    "media_evidence": frozenset({"status", "ref_id", "media_type", "source"}),
}

# 已知非业务字段 (不参与哈希, 不触发 unknown_keys, 在 business_json 完整保留)
BENIGN_KEYS = frozenset({"frame_idx", "title", "message"})

# 数值字段: 要求精确十进制语义
NUMERIC_FIELDS = frozenset({"risk_score", "temperature", "humidity", "threshold"})
# 时间字段: 带时区按 UTC 时刻, 无时区保留字面
TIME_FIELDS = frozenset({"timestamp", "device_captured_at", "occurred_at"})


def canonical_reasons_json(reasons: set[str]) -> str:
    """将原因码集合规范化为确定性 JSON (稳定, 与插入顺序无关)。"""
    return json.dumps(sorted(str(r) for r in reasons), separators=(",", ":"))


def parse_reasons_json(text: str | None) -> set[str]:
    if not text:
        return set()
    try:
        return {str(x) for x in json.loads(text)}
    except (ValueError, TypeError):
        return set()


def _norm_number(value: Any):
    if isinstance(value, bool):
        return _UNP
    if isinstance(value, int):
        return str(value)  # 整数精确十进制
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            return _UNP  # NaN/±Inf
        if value.is_integer():
            return str(int(value))  # 整数值浮点(1.0 -> "1")
        return _UNP  # 0.1 类: 已丢精度, 不宣称恢复
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return _UNP
        try:
            d = Decimal(s)  # 十进制字符串精确解析
        except InvalidOperation:
            return _UNP
        if not d.is_finite():
            return _UNP
        return format(d, "f")

    return _UNP


def _norm_time(value: Any):
    if value is None or not isinstance(value, str):
        return _UNP
    s = value.strip()
    if s == "":
        return _UNP
    # fromisoformat accepts >6 fractional digits and truncates them. Reject
    # before parsing so distinct source timestamps cannot share a C4 hash.
    if re.search(r"[.,][0-9]{7,}", s):
        return _UNP
    text = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return _UNP  # 含超微秒(>6 位)或非法 -> 不截断判同
    if dt.tzinfo is not None:
        # 带时区 -> UTC 规范化 (同瞬时同字符串)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return s  # 无时区: 保留字面, 不猜时区


def classify(record_type: str, business: dict | None, *, version: str | None = None,
             other_version: str | None = None) -> tuple[str | None, set[str]]:
    """对一条业务载荷做规范化+分类。

    返回 (content_hash, reasons):
      - reasons 为空且 hash 非空: 可进入自动合并 (enqueue_sync)。
      - reasons 非空: 属待核验, 调用方必须走 c4_verify, 不得调用 enqueue_sync。
    """
    reasons: set[str] = set()
    business = business if isinstance(business, dict) else {}

    # ---- 版本判定 ----
    if version is None:
        reasons.add("unversioned")
    elif other_version is not None and version != other_version:
        reasons.add("version_mismatch")

    # ---- records: 载荷 (events 取内嵌 payload) ----
    if record_type == "person_event":
        payload = business.get("payload") if isinstance(business.get("payload"), dict) else business
        # 顶层 person_id 纳入规范化输入; 缺失与显式 None 区分 (仅当业务含该键才注入)
        if "person_id" in business:
            payload = dict(payload)
            payload["person_id"] = business["person_id"]
    else:
        payload = business

    if not isinstance(payload, dict):
        reasons.add("unparsable")
        return (None, reasons)

    whitelist = WHITELIST.get(record_type, frozenset())
    required = REQUIRED.get(record_type, frozenset())

    # ---- 必填缺失 ----
    for key in required:
        if key not in payload or payload[key] is None or payload[key] == "":
            reasons.add("missing_required")

    # ---- 未知键 / 已知非业务键 ----
    for key in payload.keys():
        if key in whitelist:
            continue
        if key in BENIGN_KEYS:
            continue  # 已知非业务, 不哈希不触发
        # trigger_type/severity 或真正未知键: 保留原文并进入待核验
        reasons.add("unknown_keys")

    # ---- 旧 ts 兼容 ----
    if record_type == "person_event" and "timestamp" not in payload and "ts" in payload:
        reasons.add("legacy_ts")

    # 若已存在任何待核验原因, 不计算/不允许自动合并
    if reasons:
        return (None, reasons)

    # ---- 规范化白名单字段 ----
    norm: dict[str, Any] = {}
    for key in whitelist:
        if key not in payload:
            continue
        val = payload[key]
        if val is None:
            norm[key + "__NULL__"] = None  # 显式 null, 与缺失区分
            continue
        if key in NUMERIC_FIELDS:
            nv = _norm_number(val)
            if nv is _UNP:
                reasons.add("unparsable")
                break
            norm[key] = nv  # 规范十进(字符串 stable)
        elif key in TIME_FIELDS:
            tv = _norm_time(val)
            if tv is _UNP:
                reasons.add("unparsable")
                break
            norm[key] = tv
        else:
            norm[key] = str(val)
    if reasons:
        return (None, reasons)

    # 稳定哈希 (版本前缀 + 排序键)
    digest_input = _HASH_VERSION.encode() + json.dumps(
        norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    import hashlib
    return (hashlib.sha256(digest_input).hexdigest(), reasons)


def compute_content_hash(record_type: str, business: dict | None, *, version: str | None = None,
                         other_version: str | None = None) -> tuple[str | None, set[str]]:
    """thin wrapper: 返回 (hash|None, reasons)。"""
    return classify(record_type, business, version=version, other_version=other_version)
