"""E8/X5 视频取证 Recorder 与失败重试/恢复 (`src/evidence/recorder.py`)。

职责（窄，只消费取证事件）：
- 只消费 **二级** 取证事件（`alert_created level==2` / `alert_upgraded`）；一级/持续/恢复不触发录像。
- 对同一告警幂等（一个告警至多一条 media_evidence；重复事件不重复建）。
- **先录像（默认 10s，SRS REQ-E8-01）并校验文件存在且非空，再登记**——确保"登记失败时本地视频仍在"（场景 B）。
- 经现有 `media_evidence` DAO 登记：`source='sensor'`、`ref_id=alert_id`、`media_type='video'`。
- 复用 `src.storage` 上传语义；仅 `ok==True` 才登记上传成功；绝不伪造 URL/object_key/成功状态。
- 失败处理：
  - 录像失败 → 无伪造成功记录，原告警保持有效，返回可诊断错误（场景 C）。
  - 登记失败 → 本地视频保留 + 可诊断错误（场景 B）。
  - 上传失败 → 媒体保留、`upload_status='failed'`、status=failed-awaiting-retry、可重试（场景 D1）。
  - 上传成功但状态翻转失败 → 不宣称整体成功，返回可诊断状态（场景 D2）。
- 状态推进用 DAO 允许的转换：pending→processing→success；processing→failed-awaiting-retry 可回退重试。

可注入：recorder / uploader / clock(now) / tmp dir；同实例 `_in_progress` 防重复处理同一 media_id。
Fake/注入全依赖，Windows 离线可测；真实 Pi 录像适配器另接，不声称已验真实硬件。

跨进程恢复边界：现有 `media_evidence` schema 无 `next_attempt_at`/认领人/租约列，无法持久化
"下次尝试时间/多消费者认领"；本模块提供 DB 持久（status/retry_count/last_error）+ 进程内安全恢复入口，
跨进程定时/认领恢复需 schema 增列（见报告提案；本模块不擅自改 schema）。

object_key 契约：本模块把 `object_key` 推导为**稳定的逻辑/OSS 目标键** `evidence/{media_id}.mp4`，
与 OSSStorageProvider「`{prefix}/{filename}`」的键生成规则一致；**LocalStorageProvider 实际使用
`snapshots/` 路径并忽略 prefix**，故不能声称该逻辑键等于 Local 的真实路径。又因当前尚未接入生产
Runner，此约定不代表已完成生产验证。本模块不把远端返回值（如 url/provider）持久化为 object_key。
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from src.database.db import Database

_EVID_LOGGER = logging.getLogger("x4.evidence")

MEDIA_SOURCE = "sensor"
MEDIA_TYPE = "video"
UPLOAD_PREFIX = "evidence"          # 证据对象前缀（上传用）
DEFAULT_RECORD_SECONDS = 10
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 5            # 秒; 运行时退避 = min(base*2^retry, cap)（不持久化）


class EvidenceError(Exception):
    """证据链可诊断错误；message 不含敏感凭据。"""


class FakeRecorder:
    """可注入录像器：向目标路径写入非空文件（测试/离线联调用）。接口 record(seconds, dest_path)->str。"""

    def record(self, seconds, dest_path) -> str:  # noqa: B027
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake-video-bytes:non-empty")
        return str(p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EvidenceRecorder:
    """E8 取证消费者：二级事件 → 录像 → media_evidence 登记 → 上传 → 状态更新。"""

    def __init__(
        self,
        db: Database,
        *,
        recorder: Optional[Any] = None,
        uploader: Optional[Any] = None,
        record_seconds: int = DEFAULT_RECORD_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: int = DEFAULT_BACKOFF_BASE,
        tmp_dir: Optional[str] = None,
        now: Optional[Callable[[], int]] = None,
    ):
        self.db = db
        self.recorder = recorder or FakeRecorder()
        self.uploader = uploader                  # 形如 upload(path, prefix)-> {ok, url?, error?}; None=跳过上传
        self.record_seconds = int(record_seconds)
        self.max_retries = int(max_retries)
        self.backoff_base = max(0, int(backoff_base_seconds))
        self._tmp_base = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir())
        self._now = now or (lambda: int(datetime.now(timezone.utc).timestamp()))
        self._in_progress: set[str] = set()

    # ------------------------------------------------------------------
    @staticmethod
    def decide_from_event(event: Any) -> Optional[str]:
        """只有合法二级事件返回 alert_id；一级/持续/恢复/None 返回 None。"""
        if not isinstance(event, dict):
            return None
        typ = event.get("type")
        if typ == "alert_created" and event.get("level") == 2:
            return event.get("alert_id")
        if typ == "alert_upgraded":
            return event.get("alert_id")
        return None

    def _dest_for(self, media_id: str) -> str:
        # 目标文件名 = {media_id}.mp4：使真实对象键 stable = evidence/{media_id}.mp4（确定性、幂等重传覆盖）
        return str(self._tmp_base / f"{media_id}.mp4")

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def handle_event(self, event: Any) -> dict:
        alert_id = self.decide_from_event(event)
        if not alert_id:
            return {"handled": False, "reason": "not_level2"}
        if not isinstance(alert_id, str) or not alert_id:
            return {"handled": False, "reason": "no_alert_id"}
        existing = self.db.list_media_evidence_by_ref(MEDIA_SOURCE, alert_id)
        if existing:
            return {"handled": False, "reason": "already_exists", "media_id": existing[0]["media_id"]}
        # 0) 预生成 media_id 并用其确定本地文件名/对象键（save 用同一 id，保证真实键 deterministic）
        media_id = self.db._media_id()
        dest = self._dest_for(media_id)
        # 1) 先录像（保证登记失败时本地文件仍在）
        try:
            dest = self._record(dest)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "handled": False, "error": "record_failed",
                    "detail": str(exc), "alert_kept": True}
        # 2) 再登记（processing + capture_result=ok, 用预生成的 media_id）
        captured_at = event.get("device_captured_at") or event.get("occurred_at") or _now_iso()
        try:
            media_id = self.db.save_media_evidence(
                source=MEDIA_SOURCE, ref_id=alert_id, media_type=MEDIA_TYPE,
                local_path=dest, captured_at=captured_at, created_at=_now_iso(),
                status="processing", capture_result="ok", media_id=media_id,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "handled": False, "error": "evidence_register_failed",
                    "detail": str(exc), "local_path_kept": dest, "alert_kept": True}
        # 3) 上传 + 推进状态
        return self._upload_finish(media_id)

    def execute_media(self, media_id: str) -> dict:
        """恢复/重试入口：领取→(必要时录像)→上传→推进。同 media_id 防重入。"""
        if media_id in self._in_progress:
            return {"ok": False, "handled": False, "reason": "in_progress", "media_id": media_id}
        self._in_progress.add(media_id)
        try:
            return self._run(media_id)
        finally:
            self._in_progress.discard(media_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _record(self, dest: str) -> str:
        path = self.recorder.record(self.record_seconds, dest)
        p = Path(path)
        if not (p.exists() and p.stat().st_size > 0):
            raise EvidenceError(f"录像文件缺失或为空: {path}")
        return str(p)

    def _upload_and_mark(self, dest: str, media_id: str) -> Optional[str]:
        """执行上传并按结果推进。成功返回 None；失败返回错误标识（已标记 failed-awaiting-retry）。

        上传成功判定为**严格契约**：仅当返回 dict 且 `res.get("ok") is True` 才认定成功。
        以下均不得判成功：res 非 dict / ok 缺失 / ok=False / ok="true" / ok=1 / uploader 抛异常。
        object_key 按**稳定逻辑/OSS 目标键**推导为 `evidence/{media_id}.mp4`（与 OSS provider
        `{prefix}/{basename}` 一致；Local provider 实际用 `snapshots/` 路径，不等于该逻辑键）。
        仅向数据库回写 object_key/uploaded_at/upload_status；不写 url/provider（schema 无对应列），
        不把远端返回的未验证字段当 object_key 持久化。
        """
        if self.uploader is None:
            # 未接上传后端：明确告警"上传被跳过"，不得静默；DB 行保持在 processing，
            # 由持单实例锁的启动恢复处理（见 recover(allow_processing=True)）。
            _EVID_LOGGER.warning(
                "E8 uploader=None; OSS 上传被跳过，记录保持 processing, media_id=%s", media_id)
            return None
        try:
            res = self.uploader.upload(dest, prefix=UPLOAD_PREFIX)
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(media_id, last_error=f"upload:{exc}", upload_status="failed")
            return "upload_failed"
        if not (isinstance(res, dict) and res.get("ok") is True):
            err = (res or {}).get("error") if isinstance(res, dict) else "upload returned non-dict"
            self._mark_failed(media_id, last_error=f"upload:{err}", upload_status="failed")
            return "upload_failed"
        object_key = f"{UPLOAD_PREFIX}/{Path(dest).name}"  # 确定性: evidence/{media_id}.mp4
        uploaded_at = _now_iso()

        def _write_success() -> bool:
            # 仅推进"成功状态回写"; 不重录、不重传、不带出任何附加副作用
            return self.db.update_media_evidence_status(
                media_id, status="success", upload_status="success",
                object_key=object_key, uploaded_at=uploaded_at, last_error=None,
            )

        uploaded_ok = _write_success()
        if not uploaded_ok:
            # 成功状态回写失败: 做一次有限重试（仍失败则返回明确标识, 不宣称整体成功）。
            uploaded_ok = _write_success()
        if not uploaded_ok:
            return "status_update_failed_after_upload"
        # X5-06：数据库已确认成功后才清理本地视频；清理失败不回退上传成功状态。
        self._clean_up_proof(dest, media_id)
        return None

    def _clean_up_proof(self, dest: str, media_id: str) -> None:
        """上传 + 数据库成功状态均确认后，安全清理本地媒体文件（X5-06）。

        - 只删除 `media_id` 对应的预期路径 `<tmp_base>/{media_id}.mp4`；不递归、不用通配符、不删其他媒体。
        - 路径校验：`dest` 必须是 `self._tmp_base` 目录下的文件（防止不可信 `local_path` 越界）。
        - 幂等：文件不存在视为清理成功（不抛、不误删他文件）。
        - 删除异常被捕获并记录到日志：**不得因清理失败把已确认的上传成功改回失败**。
        """
        try:
            target = Path(dest).resolve()
            base = self._tmp_base.resolve()
            expected = (self._tmp_base / f"{media_id}.mp4").resolve()
            # 只允许清理预期证据目录内的精确文件
            if not expected.is_relative_to(base):
                return
            if target != expected:
                return
            if target.exists():
                target.unlink()
        except Exception as exc:  # noqa: BLE001 — 清理失败仅记录, 不翻转上传成功事实
            import logging
            logging.getLogger("x4.evidence").warning(
                "media cleanup failed (upload stays success): media_id=%s err=%s",
                media_id, type(exc).__name__)

    def _mark_failed(self, media_id: str, *, last_error: str,
                     capture_result: Any = None, upload_status: Any = None) -> bool:
        row = self.db.get_media_evidence(media_id) or {}
        retry = int(row.get("retry_count") or 0) + 1
        kw: dict[str, Any] = dict(status="failed-awaiting-retry", last_error=last_error, retry_count=retry)
        if capture_result is not None:
            kw["capture_result"] = capture_result
        if upload_status is not None:
            kw["upload_status"] = upload_status
        return self.db.update_media_evidence_status(media_id, **kw)

    def _upload_finish(self, media_id: str) -> dict:
        row = self.db.get_media_evidence(media_id)
        if row is None:
            return {"ok": False, "media_id": media_id, "error": "missing"}
        err = self._upload_and_mark(row["local_path"], media_id)
        if err:
            return {"ok": False, "media_id": media_id, "error": err}
        return {"ok": True, "handled": True, "media_id": media_id, "ref_id": row["ref_id"]}

    def _run(self, media_id: str) -> dict:
        row = self.db.get_media_evidence(media_id)
        if row is None:
            return {"ok": False, "media_id": media_id, "error": "missing"}
        status = row["status"]
        if status in ("pending", "failed-awaiting-retry"):
            # pending 或重试回收: 先进入 processing（DAO 合法转换）
            if not self.db.update_media_evidence_status(media_id, status="processing"):
                return {"ok": False, "media_id": media_id, "error": "cannot_enter_processing"}
            row = self.db.get_media_evidence(media_id)
        elif status != "processing":
            return {"ok": False, "media_id": media_id, "error": f"unexpected_status:{status}"}
        dest = row["local_path"] or self._dest_for(media_id)
        if row.get("capture_result") != "ok":
            try:
                dest = self._record(dest)
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(media_id, last_error=f"record:{exc}", capture_result="failed")
                return {"ok": False, "media_id": media_id, "error": "record_failed", "detail": str(exc)}
            if not self.db.update_media_evidence_status(media_id, status="processing",
                                                         capture_result="ok", last_error=None):
                return {"ok": False, "media_id": media_id, "error": "status_update_failed_after_record"}
        err = self._upload_and_mark(dest, media_id)
        if err:
            return {"ok": False, "media_id": media_id, "error": err}
        return {"ok": True, "handled": True, "media_id": media_id, "ref_id": row.get("ref_id")}

    # ------------------------------------------------------------------
    # 恢复 / 重试（DB 持久 + 进程内安全）
    # ------------------------------------------------------------------
    def _recoverable(self, allow_processing: bool = False) -> list[dict]:
        """返回可恢复任务。

        - 默认（allow_processing=False）只含 pending / failed-awaiting-retry（运行时轮询安全）。
        - allow_processing=True 额外纳入 processing —— 仅限持单实例锁的启动恢复，且必须满足
          capture_result='ok' 且本地录像文件存在（只补传既有文件，绝不重新录像）。
        - 重试次数达到 max_retries 的任务不再回收（有界）。
        """
        statuses = ["'pending'", "'failed-awaiting-retry'"]
        if allow_processing:
            statuses.append("'processing'")
        with self.db._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM media_evidence WHERE status IN (%s) ORDER BY created_at"
                % ",".join(statuses))
            rows = [dict(r) for r in cur.fetchall()]
        result: list[dict] = []
        for r in rows:
            if int(r.get("retry_count") or 0) >= self.max_retries:
                continue
            if r.get("status") == "processing":
                if not allow_processing:
                    continue
                if r.get("capture_result") != "ok":
                    continue  # 需重录的任务不自动恢复，避免伪造已录
                local = r.get("local_path")
                if not local or not Path(local).is_file():
                    continue  # 本地录像缺失，不明知数据下补传/重录
            result.append(r)
        return result

    def recover(self, max_tasks: int = 10, *, allow_processing: bool = False) -> list[dict]:
        """执行一次恢复/重试：默认只处理 pending/failed-awaiting-retry。

        仅当显式 allow_processing=True（且调用方已持单实例锁）才处理遗留 processing。
        """
        results: list[dict] = []
        for row in self._recoverable(allow_processing=allow_processing)[: int(max_tasks)]:
            results.append(self.execute_media(row["media_id"]))
        return results