#!/usr/bin/env python3
"""明确标记的现场演示：模拟高温告警 + 真实拍照、录像、OSS 上传。

这不是传感器驱动，也不会篡改 ``config/edge.yaml``。它只在操作者明确传入
``--confirm-live-demo`` 时写入一条 ``demo-fake-temperature`` 模拟读数和
``sensor_hightemp_demo`` 告警；随后使用实际 Pi 摄像头拍摄一张照片、录制 E8
配置指定时长的视频，并将两份媒体关联到该告警。C4 worker 会照常把元数据同步
到 Windows，微信小程序可在该传感器告警详情页查看图片、播放视频。

运行前必须停止 disaster-edge.service，避免两个进程争用同一摄像头。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.e7e8_runner import (  # noqa: E402
    _acquire_e8_singleton_lock,
    _close_lock_fd,
    build_camera_recorder,
    build_uploader,
    load_e8_config,
    make_capture_factory,
)
from src.database.db import Database  # noqa: E402
from src.evidence.recorder import EvidenceRecorder  # noqa: E402
from src.sync import bridge as c4_bridge  # noqa: E402

DEMO_SENSOR_ID = "demo-fake-temperature"
DEMO_ALERT_TYPE = "sensor_hightemp_demo"
UPLOAD_PREFIX = "evidence"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _capture_photo(e8: Any, destination: Path) -> Path:
    """拍一张真实相机照片；无帧或写入失败绝不伪造成功。"""
    import cv2

    source = make_capture_factory(e8)()
    try:
        ok, frame = source.read()
        if not ok or frame is None:
            raise RuntimeError("相机未返回可用画面")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(destination), frame):
            raise RuntimeError("JPEG 写入失败")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("照片文件缺失或为空")
        return destination
    finally:
        source.release()


def _save_and_upload_photo(db: Database, uploader: Any, *, alert_id: str,
                           e8: Any, work_dir: Path) -> dict[str, Any]:
    media_id = db._media_id()
    photo_path = work_dir / f"{media_id}.jpg"
    captured_at = _now_iso()
    try:
        _capture_photo(e8, photo_path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "photo_capture_failed", "detail": str(exc)}

    db.save_media_evidence(
        source="sensor", ref_id=alert_id, media_type="image",
        local_path=str(photo_path), captured_at=captured_at, created_at=_now_iso(),
        status="processing", capture_result="ok", media_id=media_id,
    )
    try:
        response = uploader.upload(str(photo_path), prefix=UPLOAD_PREFIX)
    except Exception as exc:  # noqa: BLE001
        response = {"ok": False, "error": f"upload exception: {type(exc).__name__}: {exc}"}
    if not (isinstance(response, dict) and response.get("ok") is True):
        detail = response.get("error", "upload returned an invalid response") if isinstance(response, dict) else "upload returned non-dict"
        db.update_media_evidence_status(
            media_id, status="failed-awaiting-retry", upload_status="failed",
            last_error=f"demo photo upload: {detail}", retry_count=1,
        )
        return {"ok": False, "media_id": media_id, "error": "photo_upload_failed"}

    marked = db.update_media_evidence_status(
        media_id, status="success", upload_status="success",
        object_key=f"{UPLOAD_PREFIX}/{photo_path.name}", uploaded_at=_now_iso(),
        last_error=None,
    )
    if not marked:
        return {"ok": False, "media_id": media_id, "error": "photo_status_update_failed"}
    return {"ok": True, "media_id": media_id}


def _create_demo_alert(db: Database, temperature: float) -> tuple[str, str, str]:
    """创建一对清晰标记的模拟 reading/level-2 alert，并登记 C4 outbox。"""
    captured_at = _now_iso()
    reading = c4_bridge.create_sync_record(
        db,
        record_type="reading",
        business={
            "sensor_id": DEMO_SENSOR_ID,
            "temperature": c4_bridge.exact_number(temperature),
            "humidity": None,
            "device_captured_at": captured_at,
        },
    )
    alert = c4_bridge.create_sync_record(
        db,
        record_type="sensor_alert",
        business={
            "type": DEMO_ALERT_TYPE,
            "severity": "HIGH",
            "temperature": c4_bridge.exact_number(temperature),
            "humidity": None,
            "threshold": c4_bridge.exact_number(50.0),
            "device_captured_at": captured_at,
            "reading_id": reading["source_record_id"],
            "occurred_at": captured_at,
        },
    )
    return reading["source_record_id"], alert["source_record_id"], captured_at


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live-demo", action="store_true",
                        help="确认创建模拟告警，并真实使用摄像头和 OSS。")
    parser.add_argument("--temperature", type=float, default=55.0,
                        help="模拟温度，默认 55.0°C（必须在 -40 到 85 之间）。")
    parser.add_argument("--edge-config", default="config/edge.yaml")
    parser.add_argument("--storage-config", default="config/storage.yaml")
    parser.add_argument("--db", default=os.environ.get("DRRM_DB", "data/drrm.db"))
    args = parser.parse_args()

    if not args.confirm_live_demo:
        parser.error("这是实际相机/OSS 演示；请明确加 --confirm-live-demo")
    if not -40.0 <= args.temperature <= 85.0:
        parser.error("--temperature 必须在 -40 到 85°C，避免写入无效模拟读数")

    edge_path = Path(args.edge_config).resolve()
    db_path = Path(args.db).resolve()
    e8 = load_e8_config(str(edge_path))
    if not e8.enabled:
        raise SystemExit("STOP：edge.yaml 的 e8.enabled=false；演示不会伪造录像")

    db = Database(str(db_path))
    lock_fd = _acquire_e8_singleton_lock(str(db_path))
    try:
        uploader = build_uploader(args.storage_config)
        work_dir = Path(e8.tmp_dir).expanduser() if e8.tmp_dir else db_path.parent / "demo-evidence"
        work_dir.mkdir(parents=True, exist_ok=True)

        reading_id, alert_id, occurred_at = _create_demo_alert(db, args.temperature)
        event = {
            "type": "alert_created", "level": 2, "alert_id": alert_id,
            "temperature": args.temperature, "threshold": 50.0,
            "occurred_at": occurred_at, "device_captured_at": occurred_at,
        }
        video = EvidenceRecorder(
            db, recorder=build_camera_recorder(e8), uploader=uploader,
            record_seconds=e8.record_seconds, tmp_dir=str(work_dir),
        ).handle_event(event)
        photo = _save_and_upload_photo(
            db, uploader, alert_id=alert_id, e8=e8, work_dir=work_dir,
        )

        print("DEMO_RESULT")
        print("synthetic=true")
        print(f"sensor_id={DEMO_SENSOR_ID}")
        print(f"temperature_c={args.temperature}")
        print(f"reading_id={reading_id}")
        print(f"alert_id={alert_id}")
        print(f"video_ok={video.get('ok') is True}")
        print(f"video_media_id={video.get('media_id', '')}")
        print(f"photo_ok={photo.get('ok') is True}")
        print(f"photo_media_id={photo.get('media_id', '')}")
        print("C4 worker will sync alert and media metadata; open this sensor alert in the mini program.")
        return 0 if video.get("ok") is True and photo.get("ok") is True else 2
    finally:
        _close_lock_fd(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
