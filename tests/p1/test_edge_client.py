"""Edge Client 测试 (Stage 7-B, tests/p1): FakeCamera + FakeTransport。

不调用真实摄像头、不访问真实 API。
"""

import json
import os
import sys
import unittest

import numpy as np
import requests

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.edge_client import (
    CONFIG_PATH,
    EdgeCam,
    EdgeEncodeError,
    EdgeUploader,
    encode_jpeg,
    extract_summary,
    load_config,
    render_overlay,
    run_loop,
    run_preview_only,
)

SUCCESS_BODY = {
    "success": True,
    "vlm_output": {"scene_type": "collapse", "overall_severity": "critical",
                   "hazards": [], "environment_note": ""},
    "llm_output": {"event_summary": "", "risk_explanations": [],
                   "rescue_suggestions": [],
                   "task_proposals": [{"action": "alert", "target": 1, "payload": {}},
                                      {"action": "log", "target": None, "payload": {}}]},
    "error": None,
}

PERSONS = [{"person_id": 1, "state": "Fall", "risk_score": 0.9, "priority": "HIGH"}]


class FakeCamera:
    """返回零值 BGR 帧的假摄像头。"""

    def __init__(self, width=1280, height=720, fail=False):
        self.width = width
        self.height = height
        self.fail = fail

    def capture(self):
        if self.fail:
            raise RuntimeError("no camera")
        return np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def close(self):
        pass


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeTransport:
    """记录请求并返回/抛错的假 requests.post。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


def no_sleep(_s):
    return None


class ConfigTest(unittest.TestCase):
    def test_load_config_has_expected_keys(self):
        cfg = load_config(CONFIG_PATH)
        self.assertEqual(cfg["server"]["api_url"],
                         "http://192.168.43.206:8000/api/v1/analyze")
        self.assertEqual(cfg["camera"]["width"], 1280)
        self.assertEqual(cfg["camera"]["height"], 720)
        self.assertEqual(cfg["upload"]["interval"], 10)
        self.assertEqual(cfg["upload"]["jpeg_quality"], 90)


class EncodeTest(unittest.TestCase):
    def test_encode_jpeg_returns_jpeg_bytes(self):
        frame = FakeCamera(width=320, height=240).capture()
        jpeg = encode_jpeg(frame, quality=90)
        self.assertTrue(jpeg)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))  # JPEG 魔数

    def test_encode_bad_input_raises(self):
        with self.assertRaises(EdgeEncodeError):
            encode_jpeg("not a frame", quality=90)  # type: ignore[arg-type]


class UploadFormatTest(unittest.TestCase):
    def test_multipart_format(self):
        transport = FakeTransport(response=FakeResponse(200, SUCCESS_BODY))
        up = EdgeUploader("http://fake/api/v1/analyze", transport=transport, timeout=25)
        jpeg = b"\xff\xd8\xfffakejpeg"
        status, body = up.upload(jpeg, person_events=PERSONS)

        self.assertEqual(status, 200)
        call = transport.calls[0]
        self.assertEqual(call["url"], "http://fake/api/v1/analyze")
        self.assertEqual(call["timeout"], 25)
        # 图片字段 multipart
        self.assertEqual(call["files"]["image"][0], "frame.jpg")
        self.assertEqual(call["files"]["image"][1], jpeg)
        self.assertEqual(call["files"]["image"][2], "image/jpeg")
        # person_events 为 JSON 字符串
        self.assertEqual(json.loads(call["data"]["person_events"]), PERSONS)

    def test_empty_person_events_defaults_to_empty_array(self):
        transport = FakeTransport(response=FakeResponse(200, SUCCESS_BODY))
        up = EdgeUploader("http://fake/api/v1/analyze", transport=transport)
        up.upload(b"\xff\xd8jj", person_events=None)
        self.assertEqual(json.loads(transport.calls[0]["data"]["person_events"]), [])


class ResponseParseTest(unittest.TestCase):
    def test_extract_summary_success(self):
        s = extract_summary(SUCCESS_BODY)
        self.assertEqual(s["scene"], "collapse")
        self.assertEqual(s["severity"], "critical")
        self.assertEqual(s["tasks"], ["alert", "log"])

    def test_extract_summary_missing_fields_fallback(self):
        s = extract_summary({"vlm_output": {}, "llm_output": {}})
        self.assertEqual(s["scene"], "unknown")
        self.assertEqual(s["severity"], "unknown")
        self.assertEqual(s["tasks"], [])

    def test_upload_surfaces_api_error(self):
        transport = FakeTransport(response=FakeResponse(500, {"error": "server boom"}))
        up = EdgeUploader("http://fake", transport=transport)
        status, body = up.upload(b"\xff\xd8jj")
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "server boom")


class LoopTest(unittest.TestCase):
    def test_success_loop_runs_one_iteration(self):
        transport = FakeTransport(response=FakeResponse(200, SUCCESS_BODY))
        up = EdgeUploader("http://fake", transport=transport)
        iters = run_loop(up, FakeCamera(width=640, height=480),
                         interval=0, quality=90, max_uploads=1, sleep=no_sleep)
        self.assertEqual(iters, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_network_error_continues(self):
        transport = FakeTransport(error=requests.ConnectionError("refused"))
        up = EdgeUploader("http://fake", transport=transport)
        # 每轮都网络失败, max_uploads 限制迭代数; 应正常返回而不异常退出
        iters = run_loop(up, FakeCamera(), interval=0, quality=90,
                         max_uploads=2, sleep=no_sleep)
        self.assertEqual(iters, 2)
        self.assertEqual(len(transport.calls), 2)

    def test_bad_body_non_json(self):
        class WeirdResp:
            status_code = 200

            def json(self):
                raise ValueError("not json")

        transport = FakeTransport(response=WeirdResp())
        up = EdgeUploader("http://fake", transport=transport)
        status, body = up.upload(b"\xff\xd8jj")
        self.assertEqual(status, 200)
        self.assertEqual(body, {})  # 非 JSON 响应兜底为空 dict

    def test_encode_failure_continues(self):
        class BadCam(FakeCamera):
            def capture(self):
                return "not-a-frame"  # type: ignore[return-value]

        transport = FakeTransport(response=FakeResponse(200, SUCCESS_BODY))
        up = EdgeUploader("http://fake", transport=transport)
        iters = run_loop(up, BadCam(), interval=0, quality=90,
                         max_uploads=1, sleep=no_sleep)
        self.assertEqual(iters, 1)
        self.assertEqual(len(transport.calls), 0)  # 编码失败未上传

    def test_perception_feeds_person_events_to_upload(self):
        class FakePerception:
            def analyze(self, frame):
                return [{"person_id": 1, "state": "Fall",
                         "risk_score": 0.92, "priority": "HIGH"}]

        transport = FakeTransport(response=FakeResponse(200, SUCCESS_BODY))
        up = EdgeUploader("http://fake", transport=transport)
        run_loop(up, FakeCamera(), interval=0, quality=90,
                 max_uploads=1, sleep=no_sleep, perception=FakePerception())
        events = json.loads(transport.calls[0]["data"]["person_events"])
        self.assertEqual(events,
                         [{"person_id": 1, "state": "Fall",
                           "risk_score": 0.92, "priority": "HIGH"}])

    def test_perception_failure_uploads_empty_events(self):
        class BrokenPerception:
            def analyze(self, frame):
                raise RuntimeError("perception down")

        transport = FakeTransport(response=FakeResponse(200, SUCCESS_BODY))
        up = EdgeUploader("http://fake", transport=transport)
        run_loop(up, FakeCamera(), interval=0, quality=90,
                 max_uploads=1, sleep=no_sleep, perception=BrokenPerception())
        self.assertEqual(json.loads(transport.calls[0]["data"]["person_events"]), [])


class RenderOverlayTest(unittest.TestCase):
    """叠加函数纯逻辑测试 (不触 cv2.imshow / waitKey, 无显示依赖)。"""

    def test_overlay_with_events_same_shape(self):
        frame = FakeCamera(width=320, height=240).capture()
        events = [{"person_id": 1, "state": "Fall",
                   "risk_score": 0.63, "priority": "HIGH"}]
        out = render_overlay(frame, events)
        self.assertEqual(out.shape, frame.shape)
        self.assertEqual(out.dtype, frame.dtype)

    def test_overlay_empty_events_ok(self):
        frame = FakeCamera(width=320, height=240).capture()
        out = render_overlay(frame, [])
        self.assertEqual(out.shape, frame.shape)

    def test_overlay_no_key_error_on_missing_fields(self):
        frame = FakeCamera(width=320, height=240).capture()
        out = render_overlay(frame, [{"person_id": 3}])  # 缺 state/risk/priority
        self.assertEqual(out.shape, frame.shape)


class FakeCameraFrameReader:
    """模拟 src/camera.py 的 CameraFrameReader 接口 (iter_frames/stop/width/height)。"""

    def __init__(self, n=2):
        self.width = 320
        self.height = 240
        self._frames = iter(
            [np.zeros((self.height, self.width, 3), dtype=np.uint8) for _ in range(n)]
        )
        self.stopped = False

    def iter_frames(self, frame_stride: int = 1):
        def gen():
            idx = 0
            try:
                while True:
                    f = next(self._frames)
                    yield idx, 0.0, f
                    idx += 1
            except StopIteration:
                return
        return gen()

    def stop(self):
        self.stopped = True


class EdgeCamReadertest(unittest.TestCase):
    """EdgeCam 兼容包装: 复用 CameraFrameReader, 不再自建 Picamera2。"""

    def test_capture_returns_bgr_frame(self):
        reader = FakeCameraFrameReader(n=2)
        cam = EdgeCam(reader=reader)
        frame = cam.capture()
        self.assertEqual(frame.shape, (240, 320, 3))
        self.assertEqual(frame.dtype, np.uint8)
        self.assertEqual(cam.width, 320)
        self.assertEqual(cam.height, 240)
        cam.close()
        self.assertTrue(reader.stopped)  # close 委托 reader.stop()

    def test_capture_iterates_reader_frames(self):
        reader = FakeCameraFrameReader(n=2)
        cam = EdgeCam(reader=reader)
        f1 = cam.capture()
        f2 = cam.capture()
        self.assertEqual(f1.shape, (240, 320, 3))
        self.assertEqual(f2.shape, (240, 320, 3))
        cam.close()


class CountingCam(FakeCamera):
    """计数 capture 调用, 供 preview-only 测试。"""

    def __init__(self):
        super().__init__(width=320, height=240)
        self.captures = 0

    def capture(self):
        self.captures += 1
        return super().capture()


class PreviewOnlyTest(unittest.TestCase):
    """--preview-only: 连续显示实时画面, 不感知/不编码/不上传, q 退出。"""

    def test_runs_continuous_frames(self):
        cam = CountingCam()
        frames = run_preview_only(cam, max_frames=2, sleep=no_sleep,
                                  show=lambda f, e: True)
        self.assertEqual(frames, 2)
        self.assertEqual(cam.captures, 2)

    def test_exits_on_q(self):
        cam = CountingCam()
        frames = run_preview_only(cam, max_frames=10, sleep=no_sleep,
                                  show=lambda f, e: False)
        self.assertEqual(frames, 0)          # 首帧即按 q 退出
        self.assertEqual(cam.captures, 1)

    def test_main_preview_only_skips_uploader_and_perception(self):
        """--preview-only 主流程只建摄像头+预览, 不构造 uploader/perception, 不需 Gateway。"""
        import src.edge_client as ec
        from unittest import mock

        dummy = mock.MagicMock()
        with mock.patch.object(ec, "_open_camera", return_value=(None, dummy)) as oc, \
             mock.patch.object(ec, "_close_camera") as closecam, \
             mock.patch.object(ec, "EdgeUploader") as uploader, \
             mock.patch.object(ec, "run_preview_only", return_value=7) as preview:
            code = ec.main(["--preview-only"])
        self.assertEqual(code, 0)
        uploader.assert_not_called()          # 不触发上传器
        preview.assert_called_once()          # 只跑纯预览
        oc.assert_called_once()               # 经统一 CameraService 装配打开
        closecam.assert_called_once()         # 统一释放(订阅→stop→close)


if __name__ == "__main__":
    unittest.main(verbosity=2)