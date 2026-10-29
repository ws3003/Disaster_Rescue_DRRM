"""X4 云端 YOLO 推理 + 边缘回退测试（全 Fake，不连真实服务/模型）。"""

from __future__ import annotations

import pytest

from src.x4 import (
    CloudConnectionError,
    CloudInvalidResponseError,
    CloudTimeoutError,
    CloudYoloClient,
    Detection,
    EdgeFallbackRouter,
    InferenceResult,
    X4Config,
    load_x4_config,
    validate_detections,
)

DET = Detection(class_id=0, class_name="person", confidence=0.9, bbox=(10, 20, 30, 40))


def _dets():
    return [DET]


def _cloud(infer_func=None, health_func=None, model_id="yolov26l"):
    return CloudYoloClient(infer_func=infer_func, health_func=health_func, model_id=model_id)


def _cfg(**over):
    base = dict(enabled=True, request_timeout_seconds=1.0, max_attempts=1,
                cloud_endpoint="http://fake", fallback_enabled=True, cloud_health_enabled=False)
    base.update(over)
    return X4Config(**base)


def _edge_ok(calls):
    def edge(img):
        calls.append(1)
        return _dets()
    return edge


class TestCloudSuccess:
    def test_cloud_success_no_fallback(self):
        edge_calls = []
        cloud = _cloud(infer_func=lambda img, timeout: _dets())
        r = EdgeFallbackRouter(cloud, _edge_ok(edge_calls), _cfg())
        res = r.infer(None)
        assert res.success and res.provider == "cloud" and res.fallback_used is False
        assert res.model_id == "yolov26l" and len(res.detections) == 1
        assert edge_calls == []          # 云端成功, 边缘未被调用

    def test_unified_format(self):
        cloud = _cloud(infer_func=lambda img, timeout: _dets())
        r = EdgeFallbackRouter(cloud, _edge_ok([]), _cfg())
        res = r.infer(None)
        assert isinstance(res, InferenceResult)
        assert set(res.__dataclass_fields__) == {
            "success", "provider", "model_id", "detections", "latency_ms",
            "fallback_used", "error_code", "error_message"}
        assert res.provider in ("cloud", "edge")
        assert isinstance(res.latency_ms, float) and res.latency_ms >= 0
        d = res.detections[0]
        assert d.class_id == 0 and d.class_name == "person" and d.confidence == 0.9
        assert d.bbox == (10, 20, 30, 40) and len(d.bbox) == 4   # xyxy 像素, 与 detector 一致
        assert res.error_code == ""


class TestFallback:
    def test_timeout_falls_back(self):
        edge_calls = []
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        res = EdgeFallbackRouter(cloud, _edge_ok(edge_calls), _cfg()).infer(None)
        assert res.success and res.provider == "edge" and res.fallback_used is True
        assert edge_calls == [1]

    def test_connection_fails_back(self):
        edge_calls = []
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudConnectionError()))
        res = EdgeFallbackRouter(cloud, _edge_ok(edge_calls), _cfg()).infer(None)
        assert res.success and res.provider == "edge" and res.fallback_used is True

    def test_invalid_response_falls_back(self):
        edge_calls = []
        # 云端返回非法结构 -> 客户端抛 CloudInvalidResponseError -> 回退
        cloud = _cloud(infer_func=lambda img, timeout: [{"bad": "shape"}])
        res = EdgeFallbackRouter(cloud, _edge_ok(edge_calls), _cfg()).infer(None)
        assert res.success and res.provider == "edge" and res.fallback_used is True

    def test_both_fail_returns_failure(self):
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        edge = lambda img: (_ for _ in ()).throw(RuntimeError("edge crash"))
        res = EdgeFallbackRouter(cloud, edge, _cfg()).infer(None)
        assert res.success is False and res.error_code == "edge_failed"
        assert res.detections == () and res.fallback_used is True  # 曾尝试回退

    def test_cloud_disabled_only_edge(self):
        cloud_calls = []
        edge_calls = []
        cloud = _cloud(infer_func=lambda img, timeout: cloud_calls.append(1) or _dets())
        r = EdgeFallbackRouter(cloud, _edge_ok(edge_calls), _cfg(enabled=False))
        res = r.infer(None)
        assert cloud_calls == []                 # 禁用时不调云端
        assert res.success and res.provider == "edge" and res.fallback_used is True
        assert edge_calls == [1]


class TestHealthAndDisabled:
    def test_health_fails_no_fake_success(self):
        # 未注入 health -> health() 必须为 False, 不伪造成功
        r = EdgeFallbackRouter(_cloud(health_func=None), _edge_ok([]), _cfg())
        assert r.health() is False
        # 禁用健康检查前置
        r2 = EdgeFallbackRouter(_cloud(health_func=lambda: True), _edge_ok([]), _cfg(cloud_health_enabled=False))
        assert r2.health() is False
        # 启用但 health 抛错 -> False
        def boom():
            raise RuntimeError("health fail")
        r3 = EdgeFallbackRouter(_cloud(health_func=boom), _edge_ok([]),
                                _cfg(cloud_health_enabled=True))
        assert r3.health() is False

    def test_both_disabled_no_edge_failure(self):
        r = EdgeFallbackRouter(_cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError())),
                               None, _cfg(fallback_enabled=False))
        res = r.infer(None)
        assert res.success is False and res.error_code == "cloud_timeout"
        assert res.detections == () and res.error_message == "edge fallback disabled or unavailable"


class TestConfig:
    def test_default_disabled_when_file_missing(self, tmp_path):
        cfg = load_x4_config(tmp_path / "does_not_exist.yaml")
        assert cfg.enabled is False and cfg.fallback_enabled is True

    def test_load_real_config(self):
        cfg = load_x4_config("config/x4.yaml")
        assert cfg.enabled is False             # 安全默认未开启云端
        assert cfg.request_timeout_seconds == 8.0 and cfg.max_attempts == 1

    def test_validate_detections_rejects_bad(self):
        with pytest.raises(ValueError):
            validate_detections(None)
        with pytest.raises(ValueError):
            validate_detections(["x"])                       # 非对象
        with pytest.raises(ValueError):
            validate_detections([Detection(0, "p", 5.0, (0, 0, 1, 1))])  # conf>1

    def test_no_sensitive_leak_in_error_codes(self):
        endpoint = "http://secret-endpoint.example"
        valid_codes = {"cloud_disabled", "cloud_unavailable", "cloud_timeout",
                       "cloud_connection_error", "cloud_invalid_response", "edge_failed"}
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        r = EdgeFallbackRouter(cloud, lambda img: (_ for _ in ()).throw(RuntimeError("boom")),
                               _cfg(enabled=True, cloud_endpoint=endpoint))
        res = r.infer(None)
        assert res.error_code in valid_codes
        assert endpoint not in res.error_message           # 不泄露端点/敏感串


class TestLastCloudError:
    def test_cloud_success_last_error_empty(self):
        r = EdgeFallbackRouter(_cloud(infer_func=lambda img, timeout: _dets()),
                               _edge_ok([]), _cfg())
        res = r.infer(None)
        assert res.provider == "cloud" and res.fallback_used is False
        assert r.last_cloud_error == ""

    def test_cloud_timeout_then_edge_success_keeps_code(self):
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        r = EdgeFallbackRouter(cloud, _edge_ok([]), _cfg())
        res = r.infer(None)
        assert res.provider == "edge" and res.success is True and res.fallback_used is True
        assert r.last_cloud_error == "cloud_timeout"      # 稳定错误码保留(最终语义不变)
        assert res.error_code == ""                       # 边缘成功时不需要置错

    def test_both_fail_cloud_code_auditable(self):
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudConnectionError()))
        r = EdgeFallbackRouter(cloud, lambda img: (_ for _ in ()).throw(RuntimeError("e")), _cfg())
        res = r.infer(None)
        assert res.success is False and res.error_code == "edge_failed"
        assert r.last_cloud_error == "cloud_connection_error"

    def test_cloud_disabled_no_residual(self):
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        # 独立实例(初始化即空)；云端禁用 → 不尝试云端, last_cloud_error 保持 ""
        r = EdgeFallbackRouter(cloud, _edge_ok([]), _cfg(enabled=False))
        res = r.infer(None)
        assert res.provider == "edge" and r.last_cloud_error == ""

    def test_consecutive_calls_no_pollution(self):
        state = {"n": 0}
        def flaky(img, timeout):
            state["n"] += 1
            if state["n"] == 1:
                raise CloudTimeoutError()
            return _dets()
        r = EdgeFallbackRouter(_cloud(infer_func=flaky), _edge_ok([]), _cfg())
        assert r.infer(None).provider == "edge"           # 首次云端失败→边缘
        assert r.last_cloud_error == "cloud_timeout"
        assert r.infer(None).provider == "cloud"          # 第二次云端成功
        assert r.last_cloud_error == ""                   # 不残留上次

    def test_edge_failure_does_not_overwrite_cloud_code(self):
        cloud = _cloud(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        r = EdgeFallbackRouter(cloud, lambda img: (_ for _ in ()).throw(RuntimeError("edge")), _cfg())
        r.infer(None)
        assert r.last_cloud_error == "cloud_timeout"      # 边缘失败不覆盖云端错误码

class TestClientBoundedRetry:
    def test_default_single_call(self):
        calls = [0]
        def f(img, timeout):
            calls[0] += 1
            raise CloudTimeoutError()
        c = CloudYoloClient(infer_func=f)
        with pytest.raises(CloudTimeoutError):
            c.infer(None, 1.0)
        assert calls[0] == 1                             # max_attempts 默认 1

    def test_transient_retry_then_success(self):
        calls = [0]
        det = Detection(class_id=1, class_name="civilian", confidence=0.9, bbox=(1, 2, 3, 4))
        def f(img, timeout):
            calls[0] += 1
            if calls[0] < 3:
                raise CloudTimeoutError()
            return [det]
        c = CloudYoloClient(infer_func=f, max_attempts=3)
        res = c.infer(None, 1.0)
        assert len(res) == 1 and calls[0] == 3           # 含首次共 3 次

    def test_transient_reaches_cap_raises(self):
        calls = [0]
        def f(img, timeout):
            calls[0] += 1
            raise CloudConnectionError()
        c = CloudYoloClient(infer_func=f, max_attempts=2)
        with pytest.raises(CloudConnectionError):
            c.infer(None, 1.0)
        assert calls[0] == 2                             # 不超过 max_attempts

    def test_non_transient_not_retried(self):
        calls = [0]
        def f(img, timeout):
            calls[0] += 1
            return [{"bad": 1}]                           # 无效响应 -> invalid
        c = CloudYoloClient(infer_func=f, max_attempts=3)
        with pytest.raises(CloudInvalidResponseError):
            c.infer(None, 1.0)
        assert calls[0] == 1                             # 非瞬时不重试

    @pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "3"])
    def test_illegal_max_attempts(self, bad):
        with pytest.raises(ValueError):
            CloudYoloClient(infer_func=lambda img, t: [], max_attempts=bad)

    def test_final_failure_surfaces_once(self):
        # 客户端内部重试后，最终失败只抛一次（服务层只见最终结果）
        calls = [0]
        def f(img, timeout):
            calls[0] += 1
            raise CloudTimeoutError()
        c = CloudYoloClient(infer_func=f, max_attempts=3)
        seen = 0
        try:
            c.infer(None, 1.0)
        except CloudTimeoutError:
            seen += 1
        assert seen == 1 and calls[0] == 3
