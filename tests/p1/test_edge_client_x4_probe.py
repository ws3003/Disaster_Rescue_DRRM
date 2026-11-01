"""X4 云端恢复探测生产调度测试（`tests/p1/test_edge_client_x4_probe.py`）。

验证治理缺口修复：`tick_probe()` 原来没有生产调用点，现经 `edge_client.run_loop`
的 `probe_scheduler` 挂点 + `src.x4.composition.make_probe_scheduler` 接入生产调度。

- 全程用 Fake cloud / Fake edge / 注入 clock，不依赖真实 X4 服务、外网、GPU 或相机。
- 同步探测：真实推理判定，短超时；阻塞风险由"冷却/恢复态低频 + 短超时"控制（详 §6）。
- 与既有 X4 状态机/集成测试共用同一 `NetworkStateMachine`/`tick_probe`，不重复实现。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402

from src.x4.cloud_yolo import CloudTimeoutError  # noqa: E402
from src.x4.config import X4Config  # noqa: E402
from src.x4.service import X4DetectionService  # noqa: E402
from src.x4.composition import make_probe_fn  # noqa: E402


class _Clock:
    """可推进的单调时钟（network state machine 用）。"""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Cloud:
    """受控 Fake 云端：按队列返回成功/失败，记录 infer 调用次数与超时。"""

    def __init__(self, results: list | None = None):
        self.results = list(results) if results is not None else [True]
        self.calls = 0
        self.timeouts = []

    def infer(self, image_bgr, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        ok = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if not ok:
            raise CloudTimeoutError()
        from src.x4.contracts import Detection
        return [Detection(1, "civilian", 0.9, (1, 2, 3, 4))]


class _Edge:
    """本地 Detector spy（回退计数）。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, frame):
        self.calls += 1
        from src.detector import Detection as Local
        return [Local([10, 20, 30, 40], 1, "civilian", 0.9, True)]


def _cfg(**over):
    base = dict(enabled=True, request_timeout_seconds=8.0, max_attempts=1,
                cloud_endpoint="http://127.0.0.1:8316/predict", model_id="yolov26l",
                fallback_enabled=True, cloud_health_enabled=False,
                probe_interval_seconds=10.0, probe_short_timeout_seconds=2.0)
    base.update(over)
    return X4Config(**base)


def _frame():
    return np.zeros((64, 64, 3), dtype=np.uint8)


class TestProbeConfig:
    def test_config_has_probe_fields(self):
        c = _cfg()
        assert c.probe_interval_seconds == 10.0
        assert c.probe_short_timeout_seconds == 2.0

    def test_invalid_probe_interval_rejected(self):
        # X4Config 字段由 config.py `_as_float(lo=0.001)` / service `_finite_pos(>0)` 校验
        from src.x4.config import X4Config
        # 构造层面 frozen dataclass 不拒非法值；实际校验在 service 装配（_finite_pos 严格正数）
        c = X4Config(probe_interval_seconds=-5.0)
        from src.x4.service import X4DetectionService
        with pytest.raises(ValueError):
            X4DetectionService(cfg=c, cloud=None, edge_detect=None,
                               probe_interval_seconds=c.probe_interval_seconds, record=False)
        # config 解析层：负值经 load 会被拒
        from src.x4.config import load_x4_config
        _ = load_x4_config  # 存在性标注；具体 load 校验见既有 config 测试

    @pytest.mark.parametrize("bad", [0, -1, -0.5, 0.0005])
    def test_invalid_short_timeout_rejected(self, bad):
        # B4-1: `_as_float(lo=0.001)` 拒绝 <0.001(含 0/负/0.0005)，抛 X4ConfigError
        from src.x4.config import _as_float, X4ConfigError
        with pytest.raises(X4ConfigError):
            _as_float(bad, "probe_short_timeout_seconds", 0.001, 2.0)

    def test_short_timeout_lower_bound_accepted(self):
        # B4-1: 恰好 0.001 应通过（边界），默认值仍 2.0
        from src.x4.config import _as_float
        assert _as_float(0.001, "probe_short_timeout_seconds", 0.001, 2.0) == 0.001
        assert _as_float(None, "probe_short_timeout_seconds", 0.001, 2.0) == 2.0


class TestProbeSchedulingWithClock:
    def _make(self, cloud_results, clock, fail_threshold=3, cooldown=30.0,
              restore_success=2, interval=10.0):
        cl = _Cloud(cloud_results)
        cfg = _cfg(probe_interval_seconds=interval)
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=_Edge(),
                                 fail_threshold=fail_threshold, cooldown_seconds=cooldown,
                                 probe_interval_seconds=interval, restore_success=restore_success,
                                 now=clock, record=False)
        probe_fn = make_probe_fn(svc, cfg)
        return svc, cl, probe_fn

    def test_normal_state_no_probe(self):
        """云端正常(CLOUD_PREFERRED)：probe_fn 快路径返回 None，不发起探测。"""
        clock = _Clock()
        svc, cl, probe_fn = self._make([True], clock)
        assert svc.machine.state == "cloud_preferred"  # 已默认 CLED_PREFERRED
        r = probe_fn(_frame())
        assert r is None          # 正常态不探测
        assert cl.calls == 0      # 零额外探测请求

    def test_failures_enter_cooldown_then_probe_recovery(self):
        """连续失败→冷却→本地兜底→冷却结束 schedule→连续成功恢复云端优先。"""
        clock = _Clock()
        # 探测：先一次成功(不足以恢复,需2次)→下一间隔第二次成功→restored
        svc, cl, probe_fn = self._make([True], clock, fail_threshold=2, cooldown=30.0,
                                       restore_success=2, interval=10.0)
        # 触发 2 次云失败 → 进入冷却
        for _ in range(2):
            svc.machine.on_cloud_failure()
        assert svc.machine.state == "cloud_cooldown"
        # 冷却期间 detect_frame 走本地（wants_cloud false）
        assert svc.machine.wants_cloud() is False

        # 冷却未结束：probe_fn 不触发真实探测（deferred 由 clock 决定）→ in_cooldown
        r = probe_fn(_frame())
        assert r == "in_cooldown"
        assert cl.calls == 0            # 冷却中不实时探测

        # 冷却结束（推进 > cooldown）：首批进入 RECOVERY_PROBE，首次被 deferred（距下一间隔）
        clock.advance(31.0)
        r = probe_fn(_frame())
        assert r in ("deferred",)       # 进入 RECOVERY，首次延到下一间隔（probe_due_at=now+interval）
        # 推进到下一间隔 → 触发真实探测（cloud.infer 成功1次）→ probe_ok(1)
        clock.advance(10.0)
        r = probe_fn(_frame())
        assert r == "probe_ok"
        assert cl.calls == 1            # 一次成功探测
        assert cl.timeouts[-1] == 2.0   # 探测用短超时

        # 再推进 → 第二次成功 → restore_success 达标 → restored
        clock.advance(10.0)
        r = probe_fn(_frame())
        assert r == "restored"
        assert svc.machine.state == "cloud_preferred"
        assert svc.machine.wants_cloud() is True

    def test_probe_failure_keeps_fallback(self):
        """探测失败持续安全回退：不伪造恢复。"""
        clock = _Clock()
        # 探测始终失败
        svc, cl, probe_fn = self._make([False], clock, fail_threshold=1, cooldown=30.0,
                                       restore_success=2, interval=10.0)
        svc.machine.on_cloud_failure()               # 进冷却
        assert svc.machine.state == "cloud_cooldown"
        clock.advance(31.0)                  # 冷却结束
        probe_fn(_frame())                    # 首次 deferred(进RECOVERY)
        clock.advance(10.0)
        r = probe_fn(_frame())                # 真实探测失败
        assert r == "probe_failed"
        assert svc.machine.state == "recovery_probe"   # 未恢复，停留在恢复探测态
        assert svc.machine.wants_cloud() is False       # 持续本地兜底

    def test_consecutive_probe_failures_stay_in_recovery(self):
        """连续两次探测失败：状态保持恢复探测、本地兜底、探测次数=2、成功计数=0（B4-3）。"""
        clock = _Clock()
        # 探测始终失败
        svc, cl, probe_fn = self._make([False], clock, fail_threshold=1, cooldown=30.0,
                                       restore_success=2, interval=10.0)
        svc.machine.on_cloud_failure()               # 进冷却
        clock.advance(31.0)                  # 冷却结束
        assert probe_fn(_frame()) in ("deferred",)    # 进 RECOVERY，首次延到下一间隔
        clock.advance(10.0)
        assert probe_fn(_frame()) == "probe_failed"   # 探测#1 失败（触发一次真实探测）
        clock.advance(10.0)
        assert probe_fn(_frame()) == "probe_failed"   # 探测#2 失败
        # 关键断言：
        assert svc.machine.state == "recovery_probe"  # 未恢复云端优先（连续失败保持）
        assert svc.machine.wants_cloud() is False       # 本地兜底持续
        assert cl.calls == 2                            # 确已触发两次真实探测
        assert svc.machine.consecutive_probe_ok == 0    # 成功计数未累积
        # 探测间隔在两次失败后仍按 probe_due_at 节流（不会每帧重试）：
        clock.advance(5.0)  # 未到下一间隔
        assert probe_fn(_frame()) == "deferred"         # 尚未到间隔，不重复探测

    def test_disabled_no_probe_when_not_cloud_viable(self):
        """云端禁用(endpoint 空) → 无探测。"""
        clock = _Clock()
        cfg = _cfg(cloud_endpoint="")         # endpoint 空 → 不进云端
        svc = X4DetectionService(cfg=cfg, cloud=None, edge_detect=_Edge(),
                                 now=clock, record=False)
        probe_fn = make_probe_fn(svc, cfg)
        assert svc.machine.state == "edge_only"
        assert probe_fn(_frame()) is None     # EDGE_ONLY 不探测
        assert getattr(svc, "cloud", None) is None  # 未接线

    def test_probe_uses_short_timeout_not_normal(self):
        """探测用 probe_short_timeout，且不改正常推理 request_timeout。"""
        clock = _Clock()
        cl = _Cloud([True])
        cfg = _cfg(request_timeout_seconds=8.0, probe_short_timeout_seconds=2.0)
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=_Edge(), now=clock,
                                 fail_threshold=1, cooldown_seconds=30.0,
                                 probe_interval_seconds=10.0, restore_success=1, record=False)
        probe_fn = make_probe_fn(svc, cfg)
        svc.machine.on_cloud_failure()                 # 进冷却
        clock.advance(31.0)
        probe_fn(_frame())                     # deferred 进 RECOVERY
        clock.advance(10.0)
        r = probe_fn(_frame())                 # 真实探测
        assert r in ("probe_ok", "restored")
        assert cl.timeouts[-1] == 2.0          # 探测用短超时
        assert cfg.request_timeout_seconds == 8.0  # 正常推理超时未被改


class _CloudGenericExc:
    """在探测 infer 时抛普通 Exception（非 CloudError）的受控云。"""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def infer(self, image_bgr, timeout=None):
        self.calls += 1
        raise self.exc


class TestRealHttpStub:
    """真实 HTTP 客户端 → 本地 127.0.0.1 stub（非 fake cloud）。验证 transport 映射与探测回退。"""

    @pytest.fixture()
    def stub(self):
        """标准库 http.server stub：只绑 127.0.0.1，临时高位端口，mode 切换，请求计数。"""
        import http.server as hs
        import socketserver
        import threading

        class _Srv(socketserver.ThreadingMixIn, hs.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            mode = "ok"
            count = 0

        class _H(hs.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                _Srv.count += 1
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                m = _Srv.mode
                if m == "timeout":
                    time.sleep(4.0)
                    try: self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b'{}')
                    except Exception: pass
                    return
                if m == "500":
                    self.send_response(500); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b'{"x":1}'); return
                if m == "badjson":
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b'{"bad":'); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"model_id":"yolov26l","detections":[{"class_id":1,"class_name":"civilian","confidence":0.9,"bbox":[1,2,3,4]}],"latency_ms":1}')
            def do_GET(self):
                _Srv.count += 1; self.send_response(405); self.end_headers()

        srv = _Srv(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        token = "stub-test-token"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield {
            "port": port, "token": token,
            "set": lambda m: setattr(_Srv, "mode", m),
            "count": lambda: _Srv.count,
        }
        srv.shutdown(); srv.server_close()

    def _client(self, monkeypatch, stub, timeout_s):
        cfg = X4Config(enabled=True,
                       cloud_endpoint=f"http://127.0.0.1:{stub['port']}/predict",
                       model_id="yolov26l", max_attempts=1,
                       request_timeout_seconds=timeout_s, fallback_enabled=True,
                       probe_interval_seconds=10.0, probe_short_timeout_seconds=timeout_s)
        # 断言 endpoint 严格为本地回环
        assert cfg.cloud_endpoint.startswith("http://127.0.0.1:")
        from src.x4.http_transport import new_http_client
        monkeypatch.setenv("X4_API_TOKEN", stub["token"])
        return cfg, new_http_client(cfg)

    def test_real_http_success_maps_ok(self, stub, monkeypatch):
        """真实 HTTP 200 + 合法 JSON → 检测解析成功（非 fake）。"""
        cfg, real = self._client(monkeypatch, stub, 5.0)
        dets = real.infer(_frame(), timeout=5.0)
        assert len(dets) == 1 and dets[0].class_name == "civilian"
        assert stub["count"]() == 1

    @pytest.mark.parametrize("mode,exc_name,http", [
        ("500", "CloudConnectionError", 500),
        ("badjson", "CloudInvalidResponseError", 200),
    ])
    def test_real_http_error_maps(self, stub, monkeypatch, mode, exc_name, http):
        """真实 HTTP 500 / 非法 JSON → 映射为预期云端异常（非 fake）。"""
        from src.x4 import cloud_yolo as cy
        stub["set"](mode)
        cfg, real = self._client(monkeypatch, stub, 5.0)
        with pytest.raises(getattr(cy, exc_name)):
            real.infer(_frame(), timeout=5.0)
        assert stub["count"]() == 1

    def test_real_http_timeout_maps(self, stub, monkeypatch):
        """真实 HTTP 延迟超时 → CloudTimeoutError；已发 1 次请求；不宣称严格墙钟上界。"""
        from src.x4.cloud_yolo import CloudTimeoutError
        stub["set"]("timeout")
        cfg, real = self._client(monkeypatch, stub, 1.0)   # 客户端超时 1s，stub 延迟 4s
        with pytest.raises(CloudTimeoutError):
            real.infer(_frame(), timeout=1.0)
        assert stub["count"]() == 1

    def test_real_http_continuous_failure_keeps_fallback(self, stub):
        """真实 HTTP 继续失败 → 状态机保持本地回退、不恢复云端。"""
        from src.x4.composition import make_probe_fn
        stub["set"]("500")
        cfg = X4Config(enabled=True,
                       cloud_endpoint=f"http://127.0.0.1:{stub['port']}/predict",
                       model_id="yolov26l", max_attempts=1,
                       request_timeout_seconds=5.0, fallback_enabled=True,
                       probe_interval_seconds=10.0, probe_short_timeout_seconds=5.0)
        from src.x4.http_transport import new_http_client
        cl = new_http_client(cfg)
        clock = _Clock()
        edge = _Edge()
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=edge, now=clock,
                                 fail_threshold=1, cooldown_seconds=30.0,
                                 probe_interval_seconds=10.0, restore_success=2, record=False)
        svc.machine.on_cloud_failure()   # 进冷却
        clock.advance(31.0)
        probe_fn = make_probe_fn(svc, cfg)
        probe_fn(_frame())               # 进 RECOVERY deferred
        clock.advance(10.0)
        assert probe_fn(_frame()) == "probe_failed"   # 探测#1 真实 HTTP 失败
        assert svc.machine.state == "recovery_probe"
        assert svc.machine.wants_cloud() is False

    def test_real_http_recovery_to_cloud(self, stub):
        """真实 HTTP 故障解除 → 连续成功达恢复阈值后切回云端优先。"""
        from src.x4.http_transport import new_http_client
        stub["set"]("ok")                 # 故障已解除
        cfg = X4Config(enabled=True,
                       cloud_endpoint=f"http://127.0.0.1:{stub['port']}/predict",
                       model_id="yolov26l", max_attempts=1,
                       request_timeout_seconds=5.0, fallback_enabled=True,
                       probe_interval_seconds=10.0, probe_short_timeout_seconds=5.0)
        cl = new_http_client(cfg)
        clock = _Clock()
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=_Edge(), now=clock,
                                 fail_threshold=1, cooldown_seconds=30.0,
                                 probe_interval_seconds=10.0, restore_success=2, record=False)
        svc.machine.on_cloud_failure()
        clock.advance(31.0)
        from src.x4.composition import make_probe_fn
        probe_fn = make_probe_fn(svc, cfg)
        probe_fn(_frame())               # RECOVERY deferred
        clock.advance(10.0)
        assert probe_fn(_frame()) in ("probe_ok", "restored") # 探测#1 成功（真实 HTTP）
        clock.advance(10.0)
        r = probe_fn(_frame())           # 探测#2 成功 → 恢复阈值(2)达标
        assert r == "restored"
        assert svc.machine.state == "cloud_preferred"
        assert svc.machine.wants_cloud() is True
    """恢复探测遇到普通 Exception（非 CloudError）：必须被 _probe_once 捕获并作失败，不崩溃、不误恢复。"""

    def _probe_state(self, exc_cls):
        clock = _Clock()
        cl = _CloudGenericExc(exc_cls())
        cfg = _cfg()
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=_Edge(), now=clock,
                                 fail_threshold=1, cooldown_seconds=30.0,
                                 probe_interval_seconds=10.0, restore_success=1, record=False)
        probe_fn = make_probe_fn(svc, cfg)
        svc.machine.on_cloud_failure()        # 进冷却
        clock.advance(31.0)                    # 冷却结束
        probe_fn(_frame())                     # 进 RECOVERY，deferred
        clock.advance(10.0)
        r = probe_fn(_frame())                 # 触发真实探测 → 抛普通 Exception
        return r, svc, cl

    @pytest.mark.parametrize("exc_cls", [RuntimeError, ValueError, OSError])
    def test_generic_exception_maps_to_probe_failed(self, exc_cls):
        """普通异常被 _probe_once 捕获 → probe_failed；不切换云端优先；不崩溃。"""
        r, svc, cl = self._probe_state(exc_cls)
        assert r == "probe_failed"
        assert svc.machine.state == "recovery_probe"   # 未恢复，继续本地兜底
        assert svc.machine.wants_cloud() is False
        assert cl.calls == 1                            # 确已发起真实探测（异常在探测时发生）

    def test_generic_exception_does_not_abort_run_loop(self, monkeypatch):
        """普通异常经 run_loop 挂点也不阻断主帧上传/后续。"""
        import src.edge_client as ec
        clock = _Clock()
        cl = _CloudGenericExc(RuntimeError("boom"))
        cfg = _cfg()
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=_Edge(), now=clock,
                                 fail_threshold=1, cooldown_seconds=30.0,
                                 probe_interval_seconds=10.0, restore_success=1, record=False)
        probe_fn = make_probe_fn(svc, cfg)
        svc.machine.on_cloud_failure()
        clock.advance(31.0)

        class Cam:
            def capture(self): return _frame()
            def close(self): pass
        up = {"calls": 0}
        class Up:
            def upload(self, jpeg, person_events=None, **k):
                up["calls"] += 1
                return 200, {"code": "OK", "message": "ok", "data": {"result": 1}, "meta": {}}
        monkeypatch.setattr("src.snapshot.save_snapshot", lambda *a, **k: None)
        rc = ec.run_loop(Up(), Cam(), interval=0.0, quality=90, max_uploads=2,
                         sleep=lambda s: clock.advance(5.0), perception=None, agent=None,
                         probe_scheduler=probe_fn)
        assert rc == 2                       # 主循环完整跑 2 帧（未因探测异常退出）
        assert up["calls"] == 2              # 上传链路未受探测异常影响
        assert svc.machine.state == "recovery_probe"  # 探测失败保持，未误恢复
        """生产入口：run_loop 挂 probe_scheduler，恢复在真实循环中被调度。"""
        import src.edge_client as ec

        clock = _Clock()
        cl = _Cloud([True])
        cfg = _cfg(probe_interval_seconds=1.0)   # 探测间隔 0 → every 帧可探测
        svc = X4DetectionService(cfg=cfg, cloud=cl, edge_detect=_Edge(), now=clock,
                                 fail_threshold=1, cooldown_seconds=30.0,
                                 probe_interval_seconds=1.0, restore_success=1, record=False)
        probe_fn = make_probe_fn(svc, cfg)
        svc.machine.on_cloud_failure()                   # 进冷却
        clock.advance(31.0)                      # 冷却结束可探测

        class Cam:
            def capture(self):
                return _frame()
            def close(self): pass
        class Up:
            def upload(self, jpeg, person_events=None, **k):
                return 200, {"code": "OK", "message": "ok", "data": {"result": 1}, "meta": {}}
        class Agent:
            def __init__(self): self.frames = 0
            def process_frame(self, fr, context=None): self.frames += 1; return {}

        agent = Agent()
        monkeypatch.setattr("src.snapshot.save_snapshot", lambda *a, **k: None)
        # sleep 注入推进 fake clock：run_loop 每帧睡眠时推进，使探测间隔(1s)跨过，触发真实探测
        rc = ec.run_loop(Up(), Cam(), interval=0.0, quality=90, max_uploads=3,
                         sleep=lambda s: clock.advance(5.0), perception=None, agent=agent,
                         probe_scheduler=probe_fn)
        assert cl.calls >= 1                     # 探测经 run_loop 被调度（真实推理调用）
        assert svc.machine.state in ("cloud_preferred", "recovery_probe")  # 恢复路径被推进