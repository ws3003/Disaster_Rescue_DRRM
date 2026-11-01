"""X4/E6 HTTP Transport 离线测试：所有 HTTP 均经 stub `_post` 拦截，不连外网/GPU/模型。"""

from __future__ import annotations

import numpy as np
import pytest
import requests

from src.x4 import http_transport as ht
from src.x4.cloud_yolo import (
    CloudConnectionError,
    CloudInvalidResponseError,
    CloudTimeoutError,
    CloudUnavailableError,
    CloudYoloClient,
)
from src.x4.config import X4Config
from src.x4.contracts import Detection


def _bgr(h=120, w=160):
    return np.zeros((h, w, 3), dtype=np.uint8)


def _det():
    return {"class_id": 1, "class_name": "civilian", "confidence": 0.9, "bbox": [10, 20, 30, 40]}


def _payload(dets=None, latency=12.3, model_id="yolov26l"):
    return {"model_id": model_id, "detections": dets if dets is not None else [_det()], "latency_ms": latency}


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _cfg(**over):
    base = dict(enabled=True, request_timeout_seconds=8.0, max_attempts=1,
                cloud_endpoint="http://cloud:8000/predict", model_id="yolov26l",
                fallback_enabled=True, cloud_health_enabled=False)
    base.update(over)
    return X4Config(**base)


class TestEncodeJpeg:
    def test_valid_encode(self):
        assert ht.encode_jpeg(_bgr()) and len(ht.encode_jpeg(_bgr())) > 0

    def test_encode_failure_raises(self, monkeypatch):
        def fake_enc(*a, **k):
            return False, None
        monkeypatch.setattr(ht.cv2, "imencode", fake_enc)
        with pytest.raises(CloudUnavailableError):
            ht.encode_jpeg(_bgr())

    def test_bad_frame_raises(self):
        with pytest.raises(CloudUnavailableError):
            ht.encode_jpeg("not-an-array")


class TestSendPredict:
    def test_post_fields_and_success(self, monkeypatch):
        calls = []

        def stub(url, files=None, data=None, timeout=None, **k):
            calls.append(dict(url=url, files=files, data=data, timeout=timeout))
            return _Resp(200, _payload())

        monkeypatch.setattr(ht, "_post", stub)
        out = ht.send_predict("http://cloud/predict", _bgr(), "yolov26l", timeout=3.0)
        assert len(out) == 1 and out[0].class_id == 1
        c = calls[0]
        assert c["url"] == "http://cloud/predict"
        assert c["files"]["file"][0] == "frame.jpg" and c["files"]["file"][2] == "image/jpeg"
        assert c["data"]["model_id"] == "yolov26l"
        assert c["timeout"] == 3.0
        assert "max_attempts" not in c["data"]

    def test_empty_detections_success(self, monkeypatch):
        monkeypatch.setattr(ht, "_post", lambda *a, **k: _Resp(200, _payload(dets=[])))
        assert ht.send_predict("http://cloud/predict", _bgr(), "m") == ()

    @pytest.mark.parametrize("code,exc_type", [
        (400, CloudInvalidResponseError), (401, CloudUnavailableError), (403, CloudUnavailableError),
        (404, CloudUnavailableError), (500, CloudConnectionError), (503, CloudConnectionError),
        (418, CloudUnavailableError),
    ])
    def test_status_mapping(self, monkeypatch, code, exc_type):
        monkeypatch.setattr(ht, "_post", lambda *a, **k: _Resp(code, {"detail": "x"}))
        with pytest.raises(exc_type):
            ht.send_predict("http://cloud/predict", _bgr(), "m")

    def test_timeout_maps_to_cloud_timeout(self, monkeypatch):
        def stub(*a, **k):
            raise requests.exceptions.Timeout()
        monkeypatch.setattr(ht, "_post", stub)
        with pytest.raises(CloudTimeoutError):
            ht.send_predict("http://cloud/predict", _bgr(), "m")

    def test_connection_maps_to_cloud_connection(self, monkeypatch):
        def stub(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(ht, "_post", stub)
        with pytest.raises(CloudConnectionError):
            ht.send_predict("http://cloud/predict", _bgr(), "m")

    def test_invalid_json_rejected(self, monkeypatch):
        monkeypatch.setattr(ht, "_post", lambda *a, **k: _Resp(200, ValueError("bad json")))
        with pytest.raises(CloudInvalidResponseError):
            ht.send_predict("http://cloud/predict", _bgr(), "m")

    @pytest.mark.parametrize("payload", [
        {"model_id": "m"}, {"model_id": "m", "detections": [{"class_id": 1}], "latency_ms": 1},
        {"model_id": "m", "detections": [{"class_id": 1, "class_name": "c", "confidence": 1.2, "bbox": [1, 2, 3]}], "latency_ms": 1},
        {"model_id": "m", "detections": [], "latency_ms": -1.0},
        {"model_id": "", "detections": [], "latency_ms": 1},
    ])
    def test_invalid_structure_rejected(self, monkeypatch, payload):
        monkeypatch.setattr(ht, "_post", lambda *a, **k: _Resp(200, payload))
        with pytest.raises(CloudInvalidResponseError):
            ht.send_predict("http://cloud/predict", _bgr(), "m")

    def test_empty_endpoint_no_post(self, monkeypatch):
        calls = []

        def stub(*a, **k):
            calls.append(1)
            return _Resp(200, _payload())
        monkeypatch.setattr(ht, "_post", stub)
        with pytest.raises(CloudUnavailableError):
            ht.send_predict("", _bgr(), "m")
        assert calls == []

    @pytest.mark.parametrize("bad", ["http://", "ftp://h", "not a url"])
    def test_malformed_endpoint_no_post(self, monkeypatch, bad):
        calls = []

        def stub(*a, **k):
            calls.append(1)
            return _Resp(200, _payload())
        monkeypatch.setattr(ht, "_post", stub)
        with pytest.raises(ValueError):
            ht.send_predict(bad, _bgr(), "m")
        assert calls == []

    def test_single_request_per_transport_call(self, monkeypatch):
        calls = []

        def stub(*a, **k):
            calls.append(1)
            return _Resp(200, _payload())
        monkeypatch.setattr(ht, "_post", stub)
        ht.send_predict("http://cloud/predict", _bgr(), "m")
        assert len(calls) == 1                             # transport 不自带重试


class TestSendPredictToken:
    """Bearer Token 注入：仅向配置的 X4 请求附加；空/空白不发送；禁用重定向；不泄露。"""

    def _stub(self, monkeypatch):
        calls = []

        def stub(url, files=None, data=None, headers=None, allow_redirects=None, timeout=None, **k):
            calls.append(dict(url=url, headers=headers, allow_redirects=allow_redirects))
            return _Resp(200, _payload())
        monkeypatch.setattr(ht, "_post", stub)
        return calls

    def test_token_sets_bearer_header(self, monkeypatch):
        calls = self._stub(monkeypatch)
        ht.send_predict("http://cloud/predict", _bgr(), "m", token="s3cr3t")
        assert calls[0]["headers"] == {"Authorization": "Bearer s3cr3t"}

    def test_no_token_no_authorization_header(self, monkeypatch):
        monkeypatch.delenv("X4_API_TOKEN", raising=False)
        calls = self._stub(monkeypatch)
        ht.send_predict("http://cloud/predict", _bgr(), "m")   # 未设置 token，环境为空
        assert calls[0]["headers"] is None

    @pytest.mark.parametrize("tok", ["", "   ", "\t\n"])
    def test_blank_token_no_header(self, monkeypatch, tok):
        calls = self._stub(monkeypatch)
        ht.send_predict("http://cloud/predict", _bgr(), "m", token=tok)
        assert calls[0]["headers"] is None                    # 空白不发送，且不回落 env

    def test_token_from_env(self, monkeypatch):
        monkeypatch.setenv("X4_API_TOKEN", "envtoken")
        calls = self._stub(monkeypatch)
        ht.send_predict("http://cloud/predict", _bgr(), "m")   # token=None → 读 env
        assert calls[0]["headers"] == {"Authorization": "Bearer envtoken"}

    def test_blank_env_no_header(self, monkeypatch):
        monkeypatch.setenv("X4_API_TOKEN", "   ")
        calls = self._stub(monkeypatch)
        ht.send_predict("http://cloud/predict", _bgr(), "m")
        assert calls[0]["headers"] is None

    def test_redirects_disabled_with_token(self, monkeypatch):
        calls = self._stub(monkeypatch)
        ht.send_predict("http://cloud/predict", _bgr(), "m", token="s3cr3t")
        assert calls[0]["allow_redirects"] is False           # 认证请求不跟随重定向

    def test_token_not_in_error_message(self, monkeypatch):
        def stub(*a, **k):
            raise requests.exceptions.Timeout()
        monkeypatch.setattr(ht, "_post", stub)

        with pytest.raises(CloudTimeoutError) as ei:
            ht.send_predict("http://cloud/predict", _bgr(), "m", token="supersecret")
        assert "supersecret" not in str(ei.value)


class TestFactoryAndRetry:
    def test_default_max_attempts_single(self, monkeypatch):
        calls = []

        def stub(*a, **k):
            calls.append(1)
            raise requests.exceptions.Timeout()
        monkeypatch.setattr(ht, "_post", stub)
        client = ht.new_http_client(_cfg())                # max_attempts=1
        with pytest.raises(CloudTimeoutError):
            client.infer(_bgr(), timeout=8.0)
        assert len(calls) == 1                             # 单次，不额外重试

    def test_max_attempts_bounds_transient(self, monkeypatch):
        calls = []

        def stub(*a, **k):
            calls.append(1)
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(ht, "_post", stub)
        client = ht.new_http_client(_cfg(max_attempts=3))
        with pytest.raises(CloudConnectionError):
            client.infer(_bgr(), timeout=8.0)
        assert len(calls) == 3                             # 总尝试=3（客户端负责重试）

    def test_non_transient_not_retried(self, monkeypatch):
        calls = []

        def stub(*a, **k):
            calls.append(1)
            return _Resp(400, {"detail": "x"})
        monkeypatch.setattr(ht, "_post", stub)
        client = ht.new_http_client(_cfg(max_attempts=3))
        with pytest.raises(CloudInvalidResponseError):
            client.infer(_bgr(), timeout=8.0)
        assert len(calls) == 1                             # 400 非瞬时不重试

    def test_factory_passes_config(self):
        cfg = _cfg(request_timeout_seconds=2.0, max_attempts=4, model_id="yolov26l")
        client = ht.new_http_client(cfg)
        assert client.model_id == "yolov26l" and client.max_attempts == 4

    def test_factory_empty_endpoint_raises(self):
        with pytest.raises(CloudUnavailableError):
            ht.new_http_client(_cfg(cloud_endpoint=""))

    def test_infer_func_injection_still_compatible(self, monkeypatch):
        # transport 不改 CloudYoloClient：注入 fake 仍可用（无网络）
        det = Detection(class_id=1, class_name="civilian", confidence=0.9, bbox=(1, 2, 3, 4))
        client = CloudYoloClient(infer_func=lambda img, timeout: [det])
        assert client.infer(_bgr()) == (det,)