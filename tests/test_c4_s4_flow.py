"""C4->S4 software integration with DISTINCT edge/server SQLite databases.

Production bridge/worker/routers are exercised through in-process ASGI HTTP.
Business inputs and lost acknowledgements are test scenarios, not Pi/physical
network evidence. Databases are retained outside the repo for inspection; this
does not fix or certify the separately tracked Database connection leak.
"""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import c4, s4
from src.database import canonical
from src.database.db import Database
from src.sync import bridge
from src.sync.worker import OutboxWorker


class TestC4S4Flow(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1].parent / "_handover"
        parent.mkdir(exist_ok=True)
        self.artifacts = Path(tempfile.mkdtemp(prefix="c4_s4_ASGI_TEST_", dir=parent))
        self.requests = []
        self.edge = Database(db_path=str(self.artifacts / "edge_TEST.db"))
        self.server = Database(db_path=str(self.artifacts / "server_TEST.db"))
        self.c4_token, self.s4_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        env = patch.dict(os.environ, {"C4_API_TOKEN": self.c4_token, "S4_API_TOKEN": self.s4_token})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(c4.router)
        app.include_router(s4.router)
        app.dependency_overrides[c4.get_db] = lambda: self.server
        app.dependency_overrides[s4.get_db] = lambda: self.server
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.clock = int(time.time() * 1000) + 60_000
        self.worker = self.make_worker()

    def tearDown(self):
        # No auth headers/tokens are persisted in evidence.
        evidence = {"scope": "SOFTWARE_ASGI_TEST_NOT_REAL_HARDWARE_OR_NETWORK",
                    "case": self.id(), "http": self.requests,
                    "edge_outbox": self.edge.list_outbox(),
                    "server_readings": self.server.query_readings(limit=100),
                    "server_alerts": self.server.query_sensor_alerts(limit=100)}
        (self.artifacts / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def post(self, url, json=None, headers=None, timeout=None):
        response = self.client.post(urlsplit(url).path, json=json, headers=headers)
        self.requests.append({"payload": copy.deepcopy(json), "status": response.status_code,
                              "response": response.json()})
        return response

    def make_worker(self, post=None, **kwargs):
        return OutboxWorker(self.edge, "http://in-process.acceptance.invalid", self.c4_token,
                            http_post=post or self.post, now_ms=lambda: self.clock, **kwargs)

    def get(self, path, **kwargs):
        response = self.client.get(path, headers={"Authorization": f"Bearer {self.s4_token}"}, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"code", "message", "data", "meta"})
        self.assertEqual((body["code"], body["message"]), ("OK", "ok"))
        return body

    def seed_reading(self):
        result = bridge.create_sync_record(self.edge, record_type="reading", business={
            "sensor_id": "ASGI-TEST-sensor", "temperature": "26.75", "humidity": None,
            "device_captured_at": "2026-11-03T10:00:00+08:00",
            "edge_received_at": "2026-11-03T10:00:01+08:00",
        })
        self.assertEqual(result["status"], "queued")
        return result["source_record_id"]

    def seed_alert(self, reading_id=None):
        result = bridge.create_sync_record(self.edge, record_type="sensor_alert", business={
            "type": "sensor_hightemp", "temperature": "52.25", "threshold": "50.0",
            "humidity": None, "severity": "HIGH", "reading_id": reading_id,
            "device_captured_at": "2026-11-03T10:00:00+08:00",
            "occurred_at": "2026-11-03T10:00:02+08:00",
        })
        self.assertEqual(result["status"], "queued")
        return result["source_record_id"]

    def test_reading_copy_preserves_distinct_capture_edge_and_server_times(self):
        sid = self.seed_reading()
        self.assertIsNone(self.server.get_reading(sid))
        self.assertEqual(self.get("/api/v1/environment/readings")["meta"]["total"], 0)
        self.assertEqual(self.worker.process_batch(10), ["success"])
        body = self.get("/api/v1/environment/readings")
        self.assertEqual(body["meta"]["total"], 1)
        item = body["data"][0]
        self.assertEqual(item["reading_id"], sid)
        self.assertEqual(item["temperature"], 26.75)
        self.assertEqual(item["device_captured_at"], "2026-11-03T10:00:00+08:00")
        self.assertIsNotNone(item["received_at"])
        self.assertEqual(item["edge_received_at"], self.edge.get_reading(sid)["edge_received_at"],
                         "C4 must preserve the edge-receipt time already defined by S4")

    def test_sensor_copy_visible_in_list_detail_and_explicit_reading_relation(self):
        reading_id = self.seed_reading()
        alert_id = self.seed_alert(reading_id)
        self.assertEqual(self.worker.process_batch(10), ["success", "success"])
        body = self.get("/api/v1/alerts", params={"category": "sensor_environment", "page_size": 1})
        self.assertEqual(body["meta"], {"page": 1, "page_size": 1, "total": 1, "has_more": False})
        key = f"sensor:{alert_id}"
        self.assertEqual(body["data"][0]["alert_key"], key)
        detail = self.get(f"/api/v1/alerts/{key}")["data"]
        self.assertEqual(detail["reading_id"], reading_id)
        related = self.get(f"/api/v1/alerts/{key}/related")["data"]
        self.assertEqual([row["reading_id"] for row in related["readings"]], [reading_id])
        self.assertEqual(related["media"], [])
        self.assertIs(related["media_supported"], False)

    def test_same_payload_replay_does_not_duplicate_server_row(self):
        self.seed_reading()
        self.assertEqual(self.worker.process_batch(10), ["success"])
        original = self.requests[0]["payload"]
        response = self.post("http://in-process.acceptance.invalid/api/v1/c4/records", json=original,
                             headers={"Authorization": f"Bearer {self.c4_token}"})
        self.assertEqual(response.json()["data"][0]["status"], "already_synced")
        self.assertEqual(self.get("/api/v1/environment/readings")["meta"]["total"], 1)

    def test_lost_ack_retry_after_worker_restart_keeps_one_copy(self):
        sid = self.seed_reading()
        def lose_ack(*args, **kwargs):
            self.post(*args, **kwargs)  # Actual production receiver commits first.
            raise TimeoutError("TEST: simulated lost acknowledgement")
        first = self.make_worker(post=lose_ack, worker_id="before-restart")
        self.assertEqual(first.process_batch(10), ["retry"])
        self.assertIsNotNone(self.server.get_reading(sid))
        self.assertEqual(self.edge.list_outbox()[0]["status"], "retry_wait")
        self.clock += 60_000
        restarted = self.make_worker(worker_id="after-restart")
        self.assertEqual(restarted.process_batch(10), ["success"])
        self.assertEqual(self.requests[-1]["response"]["data"][0]["status"], "already_synced")
        self.assertEqual(self.get("/api/v1/environment/readings")["meta"]["total"], 1)

    def test_same_id_changed_content_does_not_overwrite_original(self):
        sid = self.seed_reading()
        self.assertEqual(self.worker.process_batch(10), ["success"])
        payload = copy.deepcopy(self.requests[0]["payload"])
        record = payload["records"][0]
        record["business"]["temperature"] = "31.5"
        record["content_hash"], reasons = canonical.classify(
            "reading", bridge._canonical_view("reading", record["business"]), version=bridge.SYNC_NORM_VERSION)
        self.assertFalse(reasons)
        response = self.post("http://in-process.acceptance.invalid/api/v1/c4/records", json=payload,
                             headers={"Authorization": f"Bearer {self.c4_token}"})
        self.assertEqual(response.json()["data"][0]["status"], "content_mismatch")
        self.assertEqual(self.server.get_reading(sid)["temperature"], 26.75)

    def test_bad_sync_auth_never_looks_like_a_successful_copy(self):
        sid = self.seed_reading()
        self.worker.token = "TEST-invalid-token"
        self.assertEqual(self.worker.process_batch(10), ["retry"])
        self.assertIsNone(self.server.get_reading(sid))
        response = self.client.get("/api/v1/environment/readings")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "UNAUTHORIZED")

    def test_person_event_reaches_server_s4_after_worker_reports_success(self):
        result = bridge.create_sync_record(self.edge, record_type="person_event", business={
            "type": "high_risk_alert", "severity": "HIGH", "person_id": 1,
            "payload": {"state": "Help-Waving", "priority": "HIGH", "risk_score": 1,
                        "timestamp": "2026-11-03T10:00:00+08:00"},
        })
        self.assertEqual(result["status"], "queued")
        sid = result["source_record_id"]
        self.assertIsNone(self.server.get_event(sid))
        self.assertEqual(self.worker.process_batch(10), ["success"])
        body = self.get("/api/v1/alerts", params={"category": "person"})
        self.assertEqual([row["alert_key"] for row in body["data"]], [f"person:{sid}"],
                         "C4 acknowledgement alone cannot substitute for a queryable person-event copy")

    def test_sensor_resolution_reaches_server_after_initial_sync(self):
        sid = self.seed_alert()
        self.assertEqual(self.worker.process_batch(10), ["success"])
        resolved = "2026-11-03T10:01:00+08:00"
        self.assertTrue(self.edge.update_sensor_alert_status(sid, "resolved", resolved_at=resolved))
        self.assertEqual(self.edge.get_sensor_alert(sid)["status"], "resolved")
        self.worker.process_batch(10)
        detail = self.get(f"/api/v1/alerts/sensor:{sid}")["data"]
        self.assertEqual(detail["status"], "resolved", "Server must not retain active after authoritative recovery")
        self.assertEqual(detail["resolved_at"], resolved)


if __name__ == "__main__":
    unittest.main()
