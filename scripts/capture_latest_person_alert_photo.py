#!/usr/bin/env python3
"""为已入库的最新人员告警拍摄一张真实现场图片并上传 OSS。

该脚本只处理 type=person_alert 的既有事件：不伪造人员、不创建传感器告警、
不录制视频。成功后写入 C4 outbox，运行中的 c4-worker 会将图片元数据同步到
Windows Gateway，微信小程序的该人员告警即可请求短期签名 URL 查看图片。

运行前须停止 disaster-edge.service 或手工预览进程，避免争用 Pi 摄像头。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.e7e8_runner import (  # noqa: E402
    _acquire_e8_singleton_lock,
    _close_lock_fd,
    build_uploader,
    load_e8_config,
    make_capture_factory,
)
from src.database.db import Database  # noqa: E402

UPLOAD_PREFIX = "evidence"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _latest_person_alert(db: Database, event_id: str | None) -> str:
    if event_id:
        event = db.get_event(event_id)
        if event is None or event.get("type") != "person_alert":
            raise ValueError(f"不是有效人员告警: {event_id}")
        return event_id
    with db._connect() as conn:
        row = conn.execute(
            "SELECT event_id FROM events WHERE type='person_alert' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise ValueError("未找到人员告警；请先完成一次真实人员告警识别")
    return str(row["event_id"])


def _capture_photo(e8: Any, destination: Path) -> None:
    import cv2

    source = make_capture_factory(e8)()
    try:
        ok, frame = source.read()
        if not ok or frame is None:
            raise RuntimeError("相机未返回可用画面")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(destination), frame):
            raise RuntimeError("JPEG 写入失败")
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise RuntimeError("照片文件缺失或为空")
    finally:
        source.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", help="指定人员告警 ID；默认选择最新 person_alert。")
    parser.add_argument("--edge-config", default="config/edge.yaml")
    parser.add_argument("--storage-config", default="config/storage.yaml")
    parser.add_argument("--db", default=os.environ.get("DRRM_DB", "data/drrm.db"))
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    db = Database(str(db_path))
    event_id = _latest_person_alert(db, args.event_id)
    e8 = load_e8_config(str(Path(args.edge_config).resolve()))
    if not e8.enabled:
        raise SystemExit("STOP：e8.enabled=false，拒绝在未启用摄像头配置时拍照")

    lock_fd = _acquire_e8_singleton_lock(str(db_path))
    try:
        media_id = db._media_id()
        base_dir = Path(e8.tmp_dir).expanduser() if e8.tmp_dir else db_path.parent / "person-evidence"
        photo_path = base_dir / f"{media_id}.jpg"
        captured_at = _now_iso()
        _capture_photo(e8, photo_path)
        db.save_media_evidence(
            source="person", ref_id=event_id, media_type="image",
            local_path=str(photo_path), captured_at=captured_at, created_at=_now_iso(),
            status="processing", capture_result="ok", media_id=media_id,
        )
        uploader = build_uploader(args.storage_config)
        response = uploader.upload(str(photo_path), prefix=UPLOAD_PREFIX)
        if not isinstance(response, dict) or response.get("ok") is not True:
            detail = response.get("error", "invalid upload response") if isinstance(response, dict) else "invalid upload response"
            db.update_media_evidence_status(
                media_id, status="failed-awaiting-retry", upload_status="failed",
                last_error=f"person photo upload: {detail}", retry_count=1,
            )
            print(f"RESULT=FAIL event_id={event_id} media_id={media_id} reason=upload_failed")
            return 2
        db.update_media_evidence_status(
            media_id, status="success", upload_status="success",
            object_key=f"{UPLOAD_PREFIX}/{photo_path.name}", uploaded_at=_now_iso(),
        )
        print(f"RESULT=PASS event_id={event_id} media_id={media_id} media_type=image")
        print("C4 worker will sync this person image automatically; wait one polling interval, then refresh the mini program.")
        return 0
    finally:
        _close_lock_fd(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
