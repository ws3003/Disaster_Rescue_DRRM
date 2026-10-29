"""X4/E6 HTTP 接口契约测试（离线；纯校验/映射，无网络/GPU/模型）。"""

from __future__ import annotations

import math

import pytest

from src.x4.cloud_yolo import (
    CloudConnectionError,
    CloudInvalidResponseError,
    CloudUnavailableError,
)
from src.x4.http_schema import (
    PredictRequest,
    PredictResponse,
    endpoint_enabled,
    map_status_to_error,
    parse_predict_response,
    validate_endpoint,
)


def _ok_det():
    return {"class_id": 1, "class_name": "civilian", "confidence": 0.9, "bbox": [10, 20, 30, 40]}


def _resp(detections=None, latency=12.3, model_id="yolov26l"):
    return {"model_id": model_id, "detections": detections if detections is not None else [_ok_det()],
            "latency_ms": latency}


class TestSuccessResponse:
    def test_legal_success_parsed(self):
        r = parse_predict_response(_resp())
        assert r.model_id == "yolov26l" and len(r.detections) == 1
        d = r.detections[0]
        assert d.class_id == 1 and d.class_name == "civilian" and d.confidence == 0.9
        assert d.bbox == (10, 20, 30, 40)

    def test_empty_detections_accepted(self):
        r = parse_predict_response(_resp(detections=[]))
        assert r.detections == () and r.latency_ms == 12.3   # 空=成功无目标


class TestInvalidResponse:
    @pytest.mark.parametrize("mut,op", [
        (lambda d: d.pop("class_id"), "missing field"),
        (lambda d: d.update(class_id="1"), "class_id type"),
        (lambda d: d.update(class_name=1), "class_name type"),
        (lambda d: d.update(bbox=[1, 2, 3]), "bbox len"),
        (lambda d: d.update(bbox=[float("nan"), 2, 3, 4]), "bbox NaN"),
        (lambda d: d.update(bbox=[float("inf"), 2, 3, 4]), "bbox Inf"),
        (lambda d: d.update(confidence=-0.1), "conf <0"),
        (lambda d: d.update(confidence=1.1), "conf >1"),
    ])
    def test_detection_invalid_rejected(self, mut, op):
        det = _ok_det()
        mut(det)
        with pytest.raises(ValueError):
            parse_predict_response(_resp(detections=[det]))

    @pytest.mark.parametrize("lat", [-1.0, float("nan"), float("inf"), -float("inf"), "x", True])
    def test_latency_invalid_rejected(self, lat):
        with pytest.raises(ValueError):
            parse_predict_response(_resp(latency=lat))

    @pytest.mark.parametrize("mid", ["", None, 123])
    def test_model_id_invalid_rejected(self, mid):
        d = _resp()
        d["model_id"] = mid
        with pytest.raises(ValueError):
            parse_predict_response(d)

    def test_response_not_dict_rejected(self):
        with pytest.raises(ValueError):
            parse_predict_response([1, 2])


class TestEndpoint:
    def test_empty_endpoint_disabled(self):
        assert endpoint_enabled("") is False and endpoint_enabled(None) is False
        assert validate_endpoint("") == ""

    def test_valid_http_https_accepted(self):
        assert validate_endpoint("http://host:8000/predict") == "http://host:8000/predict"
        assert validate_endpoint("https://host/predict").startswith("https://")

    @pytest.mark.parametrize("bad", ["http://", "https://", "ftp://host/predict", "host/predict",
                                     "not a url", "//nohost"])
    def test_invalid_endpoint_rejected(self, bad):
        with pytest.raises(ValueError):
            validate_endpoint(bad)


class TestStatusMapping:
    def test_400_to_invalid(self):
        assert isinstance(map_status_to_error(400), CloudInvalidResponseError)

    @pytest.mark.parametrize("code", [401, 403])
    def test_auth_codes_to_unavailable(self, code):
        assert isinstance(map_status_to_error(code), CloudUnavailableError)

    def test_404_to_unavailable(self):
        assert isinstance(map_status_to_error(404), CloudUnavailableError)

    @pytest.mark.parametrize("code", [500, 502, 599])
    def test_5xx_to_connection(self, code):
        assert isinstance(map_status_to_error(code), CloudConnectionError)

    def test_unknown_status_safe_default(self):
        assert isinstance(map_status_to_error(418), CloudUnavailableError)


class TestRequestFieldContract:
    def test_predict_request_valid(self):
        req = PredictRequest("yolov26l")
        assert req.model_id == "yolov26l"
        assert PredictRequest.form_fields("yolov26l") == {"model_id": "yolov26l"}

    @pytest.mark.parametrize("bad", ["", None, 12])
    def test_predict_request_bad_model(self, bad):
        with pytest.raises(ValueError):
            PredictRequest(bad)

    def test_multi_part_fields_exist(self):
        from src.x4.http_schema import FORM_FIELD_FILE, FORM_FIELD_MODEL
        assert {FORM_FIELD_FILE, FORM_FIELD_MODEL} == {"file", "model_id"}