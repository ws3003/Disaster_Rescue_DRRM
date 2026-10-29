"""X4/E6 独立 YOLO HTTP 服务端离线测试（`tests/test_x4_server_predict.py`）。

- 经 `build_app` 注入配置 dict 与 `runner_factory`（FakeRunner），不读取真实 YAML 白名单、
  不加载真实 `.pt` 权重、不启动服务、无网络/GPU。
- HTTP 行为用 FastAPI TestClient 验证，`model_id` 以 form 字段提交（与服务端 `Form(...)` 一致）。
- `ModelRunner` 懒加载/缓存/非法输出用替换 `sys.modules["ultralytics"]` 的假模块验证，
  不触碰真实模型。
- 成功响应严格兼容既有 `parse_predict_response`。
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from fastapi.testclient import TestClient

import src.x4.model_runner as model_runner_mod
from src.x4.contracts import Detection
from src.x4.http_schema import parse_predict_response
from src.x4.model_runner import ModelRunner, ModelRunnerError
import server.x4_predict as x4p
from server.x4_predict import (
    X4ServerConfigError,
    build_app,
    ensure_safe_bind,
    load_server_config,
    main,
)

FAKE_WEIGHTS = "D:/weights/yolov26l_best.pt"   # 仅测试占位，真实模型不加载


def _config(**overrides):
    """最小合法服务端配置 dict（与 load_server_config 返回结构一致）。"""
    cfg = {
        "token_env": "X4_API_TOKEN",
        "max_file_bytes": 10 * 1024 * 1024,
        "conf": 0.25,
        "imgsz": 640,
        "models": {"yolov26l": FAKE_WEIGHTS},
    }
    cfg.update(overrides)
    return cfg


class FakeRunner:
    """可注入的假 runner：按预设返回 detections 或抛异常。"""

    def __init__(self, detections=(), exc: Exception | None = None, calls=None):
        self.detections = detections
        self.exc = exc
        self.calls = calls if calls is not None else []

    def run(self, image_bgr):
        self.calls.append(image_bgr)
        if self.exc is not None:
            raise self.exc
        return self.detections


class _BadRunner:
    """返回非契约对象，触发服务端严格校验。"""

    def run(self, image_bgr):
        return [{"class_id": "not-an-int", "class_name": "x", "confidence": 0.5, "bbox": [1, 2, 3, 4]}]


def _jpg_bytes(seed=0, size=(32, 32)):
    import cv2
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, size=(*size, 3), dtype=np.uint8)
    ok, enc = cv2.imencode(".jpg", arr)
    assert ok
    return enc.tobytes()


def _make_app(**build_kwargs):
    build_kwargs.setdefault("config", _config())
    return build_app(**build_kwargs)


def _post(client, *, token=None, model_id="yolov26l", body=b"x"):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/predict",
        headers=headers,
        data={"model_id": model_id},
        files={"file": ("a.jpg", body, "image/jpeg")},
    )


# ----------------------------------------------------------------------
# HTTP 层：路由 / 状态码 / 鉴权 / 大小 / 输入校验 / 响应契约
# ----------------------------------------------------------------------

class TestRouting:
    def test_get_predict_returns_405(self):
        client = TestClient(_make_app())
        assert client.get("/predict").status_code == 405

    def test_success_returns_200_and_parses(self):
        dets = (Detection(1, "civilian", 0.9, (10, 20, 30, 40)),)
        client = TestClient(_make_app(runner_factory=lambda mid: FakeRunner(dets)))
        resp = _post(client, body=_jpg_bytes())
        assert resp.status_code == 200
        body = resp.json()
        # 成功响应顶层即业务 payload，无统一信封（契约 http_schema.parse_predict_response）
        assert set(body) == {"model_id", "detections", "latency_ms"}
        assert not any(k in body for k in ("code", "message", "data", "meta"))
        parsed = parse_predict_response(body)     # 整包直接解析，不再剥 data
        assert parsed.model_id == "yolov26l"
        (d,) = parsed.detections
        assert d.class_id == 1 and d.class_name == "civilian"
        assert d.confidence == 0.9 and d.bbox == (10, 20, 30, 40)

    def test_empty_detections_returns_200_empty_list(self):
        client = TestClient(_make_app(runner_factory=lambda mid: FakeRunner(())))
        resp = _post(client, body=_jpg_bytes())
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"model_id", "detections", "latency_ms"}   # 无信封
        assert body["detections"] == []
        parsed = parse_predict_response(body)     # 空=成功无目标，整包解析
        assert parsed.detections == ()


class TestInputValidation:
    def test_missing_file_returns_400(self):
        # 缺 file 字段 → handler 内显式 400（不再由框架返回 422）+ 统一错误信封。
        client = TestClient(_make_app())
        resp = client.post("/predict", data={"model_id": "yolov26l"})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "INVALID_IMAGE"
        assert body["data"] is None
        assert body["meta"] == {}
        assert isinstance(body["message"], str) and body["message"]

    def test_empty_file_returns_400(self):
        client = TestClient(_make_app())
        resp = _post(client, body=b"")
        assert resp.status_code == 400

    def test_corrupt_image_returns_400(self):
        client = TestClient(_make_app())
        resp = _post(client, body=b"notanimage")
        assert resp.status_code == 400
        assert resp.json()["code"] == "INVALID_IMAGE"

    def test_unknown_model_id_returns_404(self):
        client = TestClient(_make_app())
        resp = _post(client, model_id="nope", body=_jpg_bytes())
        assert resp.status_code == 404
        assert resp.json()["code"] == "INVALID_MODEL"


class TestAuth:
    def test_missing_token_returns_401(self):
        client = TestClient(_make_app(token="secret"))
        resp = _post(client, body=_jpg_bytes())
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self):
        client = TestClient(_make_app(token="secret"))
        resp = _post(client, token="wrong", body=_jpg_bytes())
        assert resp.status_code in (401, 403)

    def test_correct_token_returns_200(self):
        client = TestClient(_make_app(token="secret", runner_factory=lambda mid: FakeRunner()))
        resp = _post(client, token="secret", body=_jpg_bytes())
        assert resp.status_code == 200

    def test_token_via_env(self, monkeypatch):
        monkeypatch.setenv("X4_API_TOKEN", "envsecret")
        client = TestClient(_make_app(runner_factory=lambda mid: FakeRunner()))  # 不显式传 token，走 env
        resp = _post(client, token="envsecret", body=_jpg_bytes())
        assert resp.status_code == 200


class TestSizeLimit:
    def test_file_over_limit_returns_413(self):
        client = TestClient(_make_app(max_file_bytes=100))
        resp = _post(client, body=b"x" * 1024)
        assert resp.status_code == 413
        assert resp.json()["code"] == "PAYLOAD_TOO_LARGE"


class TestServerErrors:
    def test_runner_exception_returns_500(self):
        client = TestClient(_make_app(
            runner_factory=lambda mid: FakeRunner(exc=ModelRunnerError("load failed"))))
        assert _post(client, body=_jpg_bytes()).status_code == 500

    def test_model_load_failure_is_error_not_empty(self):
        # 模型初始化失败 → 500 + data=None；绝不伪装成 detections:[]。
        client = TestClient(_make_app(
            runner_factory=lambda mid: FakeRunner(exc=ModelRunnerError("model init failed"))))
        resp = _post(client, body=_jpg_bytes())
        assert resp.status_code == 500
        assert resp.json()["data"] is None

    def test_invalid_runner_output_is_500_not_empty(self):
        client = TestClient(_make_app(runner_factory=lambda mid: _BadRunner()))
        resp = _post(client, body=_jpg_bytes())
        assert resp.status_code == 500
        assert resp.json()["data"] is None

    def test_error_response_has_no_traceback_or_token_or_path(self):
        client = TestClient(_make_app(
            runner_factory=lambda mid: FakeRunner(exc=ValueError("boom trace"))))
        resp = _post(client, body=_jpg_bytes())
        assert resp.status_code == 500
        body = resp.json()
        assert "Traceback" not in str(body)
        assert FAKE_WEIGHTS not in str(body)


class TestSecurityBind:
    def test_remote_bind_rejected_without_token(self):
        with pytest.raises(RuntimeError):
            ensure_safe_bind("0.0.0.0", token_configured=False)
        with pytest.raises(RuntimeError):
            _make_app(host="0.0.0.0", token=None)

    def test_loopback_bind_allowed_without_token(self):
        ensure_safe_bind("127.0.0.1", token_configured=False)
        ensure_safe_bind("localhost", token_configured=False)
        ensure_safe_bind("::1", token_configured=False)

    def test_remote_bind_allowed_with_token(self):
        ensure_safe_bind("0.0.0.0", token_configured=True)


def _install_fake_uvicorn(monkeypatch, runs):
    """用假 uvicorn 模块替换 sys.modules['uvicorn']，捕获 run 调用（不启动真实服务）。"""
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **kwargs: runs.append((app, kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    return fake


def _capture_run(monkeypatch):
    runs = []
    _install_fake_uvicorn(monkeypatch, runs)
    return runs


class TestStartupSecurity:
    """受控启动：模块级 app 已移除；main() 以真实 host 校验，安全失败不启动 Uvicorn。"""

    def test_no_module_level_app(self):
        # 移除模块级 `app`，禁用 `uvicorn server.x4_predict:app` CLI 直启入口
        assert not hasattr(x4p, "app")

    def test_main_remote_host_without_token_rejected_before_uvicorn(self, monkeypatch):
        runs = _capture_run(monkeypatch)
        monkeypatch.setattr(x4p, "load_server_config", lambda: _config())
        monkeypatch.delenv("X4_API_TOKEN", raising=False)
        monkeypatch.setenv("X4_BIND_HOST", "0.0.0.0")
        with pytest.raises(RuntimeError):
            main()
        assert runs == []                      # 安全校验失败 → uvicorn.run 未被调用

    def test_main_loopback_host_without_token_allowed(self, monkeypatch):
        runs = _capture_run(monkeypatch)
        monkeypatch.setattr(x4p, "load_server_config", lambda: _config())
        monkeypatch.delenv("X4_API_TOKEN", raising=False)
        monkeypatch.setenv("X4_BIND_HOST", "127.0.0.1")
        main()                                   # 无 token + 回环 → 不抛
        assert runs                              # uvicorn.run 至少被调用一次

    def test_main_with_token_remote_host_allowed_with_same_host(self, monkeypatch):
        runs = _capture_run(monkeypatch)
        monkeypatch.setattr(x4p, "load_server_config", lambda: _config())
        monkeypatch.setenv("X4_API_TOKEN", "tok")
        monkeypatch.setenv("X4_BIND_HOST", "0.0.0.0")
        main()                                   # 配 token → 远程 host 放行
        assert runs
        app_arg, kwargs = runs[0]
        assert kwargs.get("host") == "0.0.0.0"
        assert kwargs.get("port") == 8316
        assert app_arg is not None              # 传 app 对象而非 ":app" 字符串

    def test_same_host_used_for_check_and_launch(self, monkeypatch):
        # 同一 host 值同时用于安全校验（build_app 内 ensure_safe_bind）与 uvicorn 启动。
        seen = {}
        monkeypatch.setattr(x4p, "load_server_config", lambda: _config())
        monkeypatch.setenv("X4_API_TOKEN", "tok")
        monkeypatch.setenv("X4_BIND_HOST", "192.168.1.10")
        orig_build = x4p.build_app

        def spy_build(**kw):
            seen["build_host"] = kw.get("host")
            return orig_build(**kw)

        monkeypatch.setattr(x4p, "build_app", spy_build)
        runs = _capture_run(monkeypatch)
        main()
        assert seen.get("build_host") == "192.168.1.10"   # build 用真实 host
        assert runs
        assert runs[0][1].get("host") == "192.168.1.10"   # uvicorn 用同一 host


# ----------------------------------------------------------------------
# ModelRunner 层：懒加载 / 缓存 / 非法输出
# ----------------------------------------------------------------------

class _FakeResults:
    def __init__(self, boxes=None, names=None):
        self.boxes = boxes if boxes is not None else _FakeBoxes()
        self.names = names if names is not None else {0: "civilian"}


class _FakeBoxes:
    def __init__(self, xyxy=((10.0, 20.0, 30.0, 40.0),), cls=(0,), conf=(0.9,)):
        self.xyxy = np.array(xyxy, dtype=np.float32)
        self.cls = np.array(cls)
        self.conf = np.array(conf, dtype=np.float32)


class _FakeUltralytics:
    """替换 sys.modules["ultralytics"]；YOLO 构造计数，__call__ 返回预设结果。"""

    instances = 0

    def __init__(self, results_per_call):
        self._results = results_per_call

    class FakeYOLO:
        def __init__(self, path, runner):
            self.path = path
            self._runner = runner

        def __call__(self, image_bgr, conf, imgsz):
            return self._runner._results

    def YOLO(self, path):
        _FakeUltralytics.instances += 1
        return self.FakeYOLO(path, self)


def _patch_ultralytics(monkeypatch, results):
    fake = _FakeUltralytics(results)
    mod = types.ModuleType("ultralytics")
    mod.YOLO = fake.YOLO
    monkeypatch.setitem(sys.modules, "ultralytics", mod)
    return fake


class TestModelRunner:
    def test_constructor_is_lazy(self):
        # 构造不加载权重（_model 保持 None），也不强依赖 ultralytics 已导入。
        runner = ModelRunner(FAKE_WEIGHTS, conf=0.25, imgsz=640)
        assert runner._model is None

    def test_lazy_load_caches_model(self, monkeypatch):
        fake = _patch_ultralytics(monkeypatch, [_FakeResults()])
        runner = ModelRunner(FAKE_WEIGHTS)
        assert runner._model is None                # 构造不加载

        out1 = runner.run(np.zeros((32, 32, 3), dtype=np.uint8))
        out2 = runner.run(np.zeros((32, 32, 3), dtype=np.uint8))
        assert _FakeUltralytics.instances == 1      # 仅第一次加载，缓存复用
        assert runner._model is not None
        assert len(out1) == 1 and out1 == out2

        (d,) = out1
        assert d.class_id == 0 and d.class_name == "civilian"
        assert d.confidence == pytest.approx(0.9)
        assert d.bbox == pytest.approx((10.0, 20.0, 30.0, 40.0))

    def test_no_detections_returns_empty(self, monkeypatch):
        boxes = _FakeBoxes(xyxy=(), cls=np.array([]), conf=np.array([]))
        _patch_ultralytics(monkeypatch, [_FakeResults(boxes)])
        assert ModelRunner(FAKE_WEIGHTS).run(np.zeros((16, 16, 3), dtype=np.uint8)) == ()

    def test_invalid_conf_rejected_at_construction(self):
        for bad in (0.0, 1.1, -0.5, 2.0):
            with pytest.raises(ModelRunnerError):
                ModelRunner(FAKE_WEIGHTS, conf=bad)

    def test_invalid_imgsz_rejected_at_construction(self):
        for bad in (0, -4, True):
            with pytest.raises(ModelRunnerError):
                ModelRunner(FAKE_WEIGHTS, imgsz=bad)

    def test_non_finite_confidence_raises(self, monkeypatch):
        boxes = _FakeBoxes(xyxy=((10, 20, 30, 40),), cls=(0,), conf=(float("nan"),))
        _patch_ultralytics(monkeypatch, [_FakeResults(boxes)])
        with pytest.raises(ModelRunnerError):
            ModelRunner(FAKE_WEIGHTS).run(np.zeros((16, 16, 3), dtype=np.uint8))

    def test_oob_confidence_raises(self, monkeypatch):
        boxes = _FakeBoxes(xyxy=((10, 20, 30, 40),), cls=(0,), conf=(1.5,))
        _patch_ultralytics(monkeypatch, [_FakeResults(boxes)])
        with pytest.raises(ModelRunnerError):
            ModelRunner(FAKE_WEIGHTS).run(np.zeros((16, 16, 3), dtype=np.uint8))

    def test_non_finite_bbox_raises(self, monkeypatch):
        boxes = _FakeBoxes(xyxy=((float("inf"), 20, 30, 40),), cls=(0,), conf=(0.9,))
        _patch_ultralytics(monkeypatch, [_FakeResults(boxes)])
        with pytest.raises(ModelRunnerError):
            ModelRunner(FAKE_WEIGHTS).run(np.zeros((16, 16, 3), dtype=np.uint8))

    def test_unknown_class_uses_index_as_name(self, monkeypatch):
        # 类别名不硬编码：names 缺失该 id 时退回索引字符串。
        boxes = _FakeBoxes(xyxy=((10, 20, 30, 40),), cls=(42,), conf=(0.9,))
        _patch_ultralytics(monkeypatch, [_FakeResults(boxes)])
        (d,) = ModelRunner(FAKE_WEIGHTS).run(np.zeros((16, 16, 3), dtype=np.uint8))
        assert d.class_id == 42 and d.class_name == "42"


# ----------------------------------------------------------------------
# 配置层：缺失 / 越界 / 空白名单被明确拒绝
# ----------------------------------------------------------------------

def _write_cfg(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestConfigValidation:
    def test_missing_models_rejected(self, tmp_path):
        cfg = _write_cfg(tmp_path, "x4_server:\n  auth:\n    token_env: X4_API_TOKEN\n"
                                  "  limits:\n    max_file_bytes: 10485760\n"
                                  "  inference:\n    conf: 0.25\n    imgsz: 640\n")
        with pytest.raises(X4ServerConfigError):
            load_server_config(cfg)

    def test_empty_models_rejected(self, tmp_path):
        cfg = _write_cfg(tmp_path, "x4_server:\n  auth:\n    token_env: T\n"
                                  "  models:\n    yolov26l: ''\n")
        with pytest.raises(X4ServerConfigError):
            load_server_config(cfg)

    def test_bad_conf_rejected(self, tmp_path):
        cfg = _write_cfg(tmp_path, "x4_server:\n  auth:\n    token_env: T\n"
                                  "  inference:\n    conf: 1.5\n"
                                  "  models:\n    yolov26l: 'D:/w.pt'\n")
        with pytest.raises(X4ServerConfigError):
            load_server_config(cfg)

    def test_bad_imgsz_rejected(self, tmp_path):
        cfg = _write_cfg(tmp_path, "x4_server:\n  auth:\n    token_env: T\n"
                                  "  inference:\n    imgsz: -5\n"
                                  "  models:\n    yolov26l: 'D:/w.pt'\n")
        with pytest.raises(X4ServerConfigError):
            load_server_config(cfg)

    def test_missing_auth_token_env_rejected(self, tmp_path):
        cfg = _write_cfg(tmp_path, "x4_server:\n  models:\n    yolov26l: 'D:/w.pt'\n")
        with pytest.raises(X4ServerConfigError):
            load_server_config(cfg)

    def test_unknown_model_id_does_not_construct_runner(self):
        touched = []
        client = TestClient(_make_app(runner_factory=lambda mid: touched.append(mid) or FakeRunner()))
        resp = _post(client, model_id="not-an-alias", body=_jpg_bytes())
        assert resp.status_code == 404
        assert touched == []                      # 未命中白名单，绝不触达 runner/磁盘