"""X4/E6 动态网络切换服务离线测试（注入时钟/fake 云边，不联网/不触模型/不写正式库）。"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from src.database.db import Database
from src.detector import Detection as LocalDetection
from src.x4.cloud_yolo import CloudTimeoutError, CloudYoloClient
from src.x4.config import X4Config
from src.x4.contracts import Detection
from src.x4.frame_selector import FrameSelector
from src.x4.service import (
    CLOUD_COOLDOWN,
    CLOUD_PREFERRED,
    EDGE_ONLY,
    RECOVERY_PROBE,
    X4DetectionError,
    X4DetectionService,
)


@pytest.fixture()
def db() -> Database:
    tmp = tempfile.TemporaryDirectory()
    d = Database(db_path=os.path.join(tmp.name, "x4_service_test.db"))
    d._tmpdir = tmp
    return d


class _Clock:
    def __init__(self, start=1000.0):
        self.value = float(start)

    def advance(self, sec):
        self.value += float(sec)

    def __call__(self):
        return self.value


def _cfg(**over):
    base = dict(enabled=True, request_timeout_seconds=1.0, max_attempts=1,
                cloud_endpoint="http://fake", model_id="yolov26l",
                fallback_enabled=True, cloud_health_enabled=False)
    base.update(over)
    return X4Config(**base)


def x4(cid, name, conf=0.9, bbox=(1, 2, 3, 4)):
    return Detection(class_id=int(cid), class_name=name, confidence=conf, bbox=tuple(bbox))


def _edge_ok(local=None):
    local = local if local is not None else [LocalDetection([10, 20, 30, 40], 1, "civilian", 0.9, True)]
    return lambda frame: list(local)


def _edge_raise():
    return lambda frame: (_ for _ in ()).throw(RuntimeError("edge boom"))


def _edge_spy(calls):
    def edge(frame):
        calls.append(1)
        return [LocalDetection([10, 20, 30, 40], 1, "civilian", 0.9, True)]
    return edge


def _cloud_ok(*dets):
    return CloudYoloClient(infer_func=lambda img, timeout: list(dets))


def _cloud_timeout():
    return CloudYoloClient(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))


def _cloud_invalid():
    return CloudYoloClient(infer_func=lambda img, timeout: [{"bad": 1}])


_SVC_PARAMS = {"fail_threshold", "cooldown_seconds", "probe_interval_seconds", "restore_success",
               "frame_selector"}


def _svc(db=None, cloud=None, edge=None, clock=None, **kw):
    clock = clock or _Clock()
    svc_kw = {k: kw.pop(k) for k in list(kw) if k in _SVC_PARAMS}
    return X4DetectionService(cfg=_cfg(**kw), cloud=cloud, edge_detect=edge or _edge_ok(),
                              db=db, now=clock, **svc_kw), clock


def _first(dets):
    assert dets, "expected non-empty detection"
    return dets[0]


class TestCloudSuccessAndConversion:
    def test_cloud_success_returns_compatible_and_logs(self, db):
        svc, _ = _svc(db=db, cloud=_cloud_ok(x4(1, "civilian"), x4(0, "cat")))
        out = svc.detect_frame(None)
        assert len(out) == 2
        assert out[0].is_person is True and out[1].is_person is False
        assert out[0].class_id == 1 and out[0].class_name == "civilian"
        assert out[0].bbox == [1, 2, 3, 4] and out[0].confidence == 0.9
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent_logs").fetchone()[0] == 1

    def test_is_person_map_1_5_true_0_2_3_4_false(self):
        svc, _ = _svc(cloud=_cloud_ok(x4(0, "cat"), x4(1, "civilian"), x4(2, "cow"),
                                      x4(3, "dog"), x4(4, "horse"), x4(5, "rescuer")))
        out = svc.detect_frame(None)
        expect = {0: False, 1: True, 2: False, 3: False, 4: False, 5: True}
        assert {d.class_id: d.is_person for d in out} == expect

    def test_empty_cloud_result_normal_no_fault(self):
        svc, _ = _svc(cloud=_cloud_ok())
        assert svc.detect_frame(None) == []               # 空检测=正常
        assert svc.machine.consecutive_failures == 0      # 未计故障


class TestEdgeOnlyWhenDisabled:
    def test_cloud_disabled_uses_edge(self):
        probe = _CloudProbe()
        svc, _ = _svc(cloud=_cloud_ok(x4(1, "civilian")), enabled=False)
        out = svc.detect_frame(None)
        assert _first(out).is_person is True              # 来自 edge(本地的 civilian)
        assert svc.machine.state == EDGE_ONLY

    def test_endpoint_empty_edges(self):
        svc, _ = _svc(cloud=_cloud_ok(x4(1, "civilian")), cloud_endpoint="")
        assert svc.machine.state == EDGE_ONLY
        assert _first(svc.detect_frame(None)).is_person is True


class TestFallback:
    def test_cloud_exception_falls_back_this_frame(self):
        svc, _ = _svc(cloud=_cloud_timeout())
        out = svc.detect_frame(None)                      # 首帧已回退本地
        assert _first(out).class_id == 1                  # 本地 civilian
        assert svc.machine.consecutive_failures == 1

    def test_cloud_invalid_falls_back(self):
        svc, _ = _svc(cloud=_cloud_invalid())
        out = svc.detect_frame(None)
        assert _first(out).is_person is True and svc.machine.consecutive_failures == 1

    def test_local_failure_propagates(self):
        svc, _ = _svc(cloud=_cloud_timeout(), edge=_edge_raise())
        with pytest.raises(X4DetectionError):
            svc.detect_frame(None)


class TestCooldownAndRecovery:
    def test_consecutive_fail_threshold_enters_cooldown(self):
        svc, _ = _svc(cloud=_cloud_timeout(), fail_threshold=3)
        svc.detect_frame(None); svc.detect_frame(None)
        assert svc.machine.state == CLOUD_PREFERRED
        svc.detect_frame(None)                            # 第3次失败
        assert svc.machine.state == CLOUD_COOLDOWN

    def test_cooldown_frames_use_edge_no_cloud_calls(self):
        wrapped = _CountingCloud()
        svc, _ = _svc(cloud=wrapped, fail_threshold=1)
        svc.detect_frame(None)                            # 第1次失败 → 冷却
        assert svc.machine.state == CLOUD_COOLDOWN
        d1 = wrapped.calls
        for _ in range(3):
            svc.detect_frame(None)                        # 冷却期每帧走本地
        assert wrapped.calls == d1                        # 冷却期不再请求云端

    def test_cooldown_not_ended_early(self):
        clock = _Clock()
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1)
        for _ in range(1):
            svc.detect_frame(None)
        assert svc.machine.state == CLOUD_COOLDOWN
        assert svc.tick_probe(lambda: True) == "in_cooldown"      # 冷却未结束
        assert svc.machine.state == CLOUD_COOLDOWN

    def test_cooldown_ends_then_probe_by_interval(self):
        clock = _Clock()
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1, cooldown_seconds=30,
                                 probe_interval_seconds=10, restore_success=2)
        svc.detect_frame(None)                            # → COOLDOWN (cooldown_until=1000+30)
        assert svc.machine.state == CLOUD_COOLDOWN
        clock.advance(31)                                 # 冷却结束
        assert svc.tick_probe(lambda: True) == "deferred"  # 进入 RECOVERY_PROBE, 首次探测延到下次间隔
        assert svc.machine.state == RECOVERY_PROBE
        clock.advance(11)                                 # 到探测间隔
        assert svc.tick_probe(lambda: True) == "probe_ok"  # 第1次探测成功(需2次才恢复)

    def test_probe_success_x2_restores_cloud(self):
        clock = _Clock()
        cloud = _cloud_ok(x4(1, "civilian"))
        svc = X4DetectionService(cfg=_cfg(), cloud=cloud, edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1, cooldown_seconds=30,
                                 probe_interval_seconds=10, restore_success=2)
        fail_cloud = _cloud_timeout()
        svc.cloud = fail_cloud
        svc.detect_frame(None)                            # 失败 → COOLDOWN
        clock.advance(31)                                 # 冷却结束
        svc.tick_probe(lambda: True)                      # →RECOVERY, deferred
        clock.advance(11)
        svc.tick_probe(lambda: True)                      # probe_ok(1)
        clock.advance(11)
        r = svc.tick_probe(lambda: True)                  # probe_ok(2) → restored
        assert r == "restored" and svc.machine.state == CLOUD_PREFERRED
        # 恢复后云端重新可用（换回可用 cloud）
        svc.cloud = cloud
        out = svc.detect_frame(None)
        assert _first(out).is_person is True and svc.machine.state == CLOUD_PREFERRED

    def test_probe_fail_keeps_cooldown_probe(self):
        clock = _Clock()
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1, cooldown_seconds=30,
                                 probe_interval_seconds=10, restore_success=2)
        svc.detect_frame(None)
        clock.advance(31); svc.tick_probe(lambda: True)   # →RECOVERY deferred
        clock.advance(11)
        assert svc.tick_probe(lambda: False) == "probe_failed"
        assert svc.machine.state == RECOVERY_PROBE and svc.machine.consecutive_probe_ok == 0

    def test_restored_then_fail_recooldowns(self):
        clock = _Clock()
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=3)
        svc.detect_frame(None)                            # 失败1
        # 恢复到可 cloud 再重试更简单：强制直接进入 CLOUD_PREFERRED，连续失败3次
        svc.machine.state = CLOUD_PREFERRED; svc.machine.consecutive_failures = 0
        for _ in range(3):
            svc.detect_frame(None)
        assert svc.machine.state == CLOUD_COOLDOWN

    def test_no_duplicate_probe_within_interval(self):
        clock = _Clock()
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1, cooldown_seconds=30,
                                 probe_interval_seconds=10, restore_success=2)
        svc.detect_frame(None)
        clock.advance(31); svc.tick_probe(lambda: True)   # →RECOVERY deferred
        clock.advance(11)
        calls = [0]
        def probe():
            calls[0] += 1
            return True
        svc.tick_probe(probe)
        assert calls[0] == 1
        assert svc.tick_probe(probe) == "deferred"        # 间隔未到，不重复探测
        assert calls[0] == 1


class TestNonBlockingAndLogIsolation:
    def test_normal_frames_never_touch_probe(self):
        probe = _CloudProbe()
        clock = _Clock()
        # 冷却/恢复态下：探测只经 tick_probe 显式触发；detect_frame 绝不调用探测
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1,
                                 cooldown_seconds=30, probe_interval_seconds=10, restore_success=2)
        for _ in range(4):
            svc.detect_frame(None)                        # 第1次即冷却，其后全走本地
        assert probe.calls == 0                           # 正常帧从不调用探测
        clock.advance(31)                                 # 冷却结束
        svc.tick_probe(probe)                             # →RECOVERY（首探测延后）
        clock.advance(11)
        svc.tick_probe(probe)                             # 到间隔后执行探测
        assert probe.calls == 1                           # 探测仅由显式 tick 触发

    def test_log_failure_does_not_affect_result(self, db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk")
        monkeypatch.setattr(db, "save_agent_log", boom)
        svc, _ = _svc(db=db, cloud=_cloud_ok(x4(1, "civilian")))
        out = svc.detect_frame(None)                      # 日志失败不影响推理
        assert _first(out).is_person is True

    def test_multi_frame_state_rule(self):
        # 云端可用成功多帧后保持云端优先
        cloud = _cloud_ok(x4(1, "civilian"))
        svc, _ = _svc(cloud=cloud)
        for _ in range(3):
            assert svc.detect_frame(None)
            assert svc.machine.state == CLOUD_PREFERRED


class _CloudProbe:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return True


class _CountingCloud(CloudYoloClient):
    def __init__(self):
        super().__init__(infer_func=lambda img, timeout: (_ for _ in ()).throw(CloudTimeoutError()))
        self.calls = 0

    def infer(self, image_bgr, timeout=None):
        self.calls += 1
        return super().infer(image_bgr, timeout)


class TestFallbackSwitch:
    def test_fallback_enabled_cloud_fail_uses_edge(self, db):
        edge_calls = []
        svc, _ = _svc(db=db, cloud=_cloud_timeout(), edge=_edge_spy(edge_calls), fallback_enabled=True)
        out = svc.detect_frame(None)
        assert _first(out).is_person is True            # 来自 edge
        assert edge_calls == [1]                        # 回退开启 → 调本地

    def test_fallback_disabled_cloud_fail_raises_no_edge(self, db):
        edge_calls = []
        svc, _ = _svc(db=db, cloud=_cloud_timeout(), edge=_edge_spy(edge_calls), fallback_enabled=False)
        with pytest.raises(X4DetectionError):
            svc.detect_frame(None)
        assert edge_calls == []                         # 禁回退 → 本地调用次数为零
        assert svc.machine.consecutive_failures == 1    # 仍更新失败状态
        with db._connect() as conn:                     # 仍记录日志
            assert conn.execute("SELECT COUNT(*) FROM agent_logs").fetchone()[0] == 1

    def test_fallback_disabled_cloud_success_returns_cloud(self, db):
        svc, _ = _svc(db=db, cloud=_cloud_ok(x4(1, "civilian")), fallback_enabled=False)
        out = svc.detect_frame(None)
        assert _first(out).is_person is True and svc.machine.state == CLOUD_PREFERRED

    def test_dual_failure_keeps_contract(self):
        svc, _ = _svc(cloud=_cloud_timeout(), edge=_edge_raise(), fallback_enabled=True)
        with pytest.raises(X4DetectionError):          # 云失败+本地失败 → 原契约抛错
            svc.detect_frame(None)

    def test_single_cloud_call_per_frame(self):
        cloud = _CountingCloud()
        svc, _ = _svc(cloud=cloud)
        # 云失败(瞬时)每帧只调一次客户端（服务层不重试）
        for _ in range(3):
            try:
                svc.detect_frame(None)
            except X4DetectionError:
                pass
        assert cloud.calls == 3


class TestProbeException:
    def test_probe_raise_returns_probe_failed_and_recovers(self, caplog):
        import logging
        clock = _Clock()
        svc = X4DetectionService(cfg=_cfg(), cloud=_cloud_timeout(), edge_detect=_edge_ok(),
                                 db=None, now=clock, fail_threshold=1,
                                 cooldown_seconds=30, probe_interval_seconds=10, restore_success=2)
        svc.detect_frame(None)                          # → COOLDOWN
        clock.advance(31)                               # 冷却结束 → RECOVERY(deferred)
        svc.tick_probe(lambda: True)
        clock.advance(11)
        with caplog.at_level(logging.WARNING, logger="x4.service"):
            r = svc.tick_probe(lambda: (_ for _ in ()).throw(RuntimeError("probe boom")))
        assert r == "probe_failed"                      # 不误判恢复
        assert svc.machine.state == RECOVERY_PROBE
        assert svc.machine.consecutive_probe_ok == 0    # 成功计数清零
        assert svc.machine._probe_in_flight is False    # in_flight 清理
        assert svc.machine.probe_due_at is not None and svc.machine.probe_due_at >= clock.value  # 定下次
        fail_msgs = [m for m in caplog.messages if "probe failed" in m]
        assert len(fail_msgs) == 1                      # 只记录一次
        # 探测失败后可再次探测并最终恢复
        clock.advance(11)
        assert svc.tick_probe(lambda: True) == "probe_ok"
        clock.advance(11)
        assert svc.tick_probe(lambda: True) == "restored"


class TestParamValidation:
    @pytest.mark.parametrize("bad", [0, -1, True, False, 2.0, "3"])
    def test_fail_threshold_invalid(self, bad):
        with pytest.raises(ValueError):
            X4DetectionService(cfg=_cfg(), fail_threshold=bad)

    @pytest.mark.parametrize("bad", [0, -1, True, "3", float("nan"), float("inf"), -float("inf")])
    def test_cooldown_seconds_invalid(self, bad):
        with pytest.raises(ValueError):
            X4DetectionService(cfg=_cfg(), cooldown_seconds=bad)

    @pytest.mark.parametrize("bad", [0, -5, True, "1", float("nan"), float("inf")])
    def test_probe_interval_invalid(self, bad):
        with pytest.raises(ValueError):
            X4DetectionService(cfg=_cfg(), probe_interval_seconds=bad)

    @pytest.mark.parametrize("bad", [0, -1, True, 1.0, "2"])
    def test_restore_success_invalid(self, bad):
        with pytest.raises(ValueError):
            X4DetectionService(cfg=_cfg(), restore_success=bad)

    def test_defaults_valid(self):
        svc, _ = _svc(cloud=_cloud_ok(x4(1, "civilian")))
        assert svc.machine.fail_threshold == 3 and svc.machine.cooldown_seconds == 30.0


# ---------------------------------------------------------------------------
# E6-01 云端选帧（本地逐帧兜底 + 选定帧云复核）
# ---------------------------------------------------------------------------
def _counting_cloud(calls, *dets):
    return CloudYoloClient(
        infer_func=lambda img, timeout: (calls.append(1), list(dets))[1])


def _sel(clock, *, interval=10.0):
    return FrameSelector(enabled=True, interval_seconds=interval, now=clock)


def _cloud_fail(calls):
    return CloudYoloClient(
        infer_func=lambda img, timeout: (calls.append(1), (_ for _ in ()).throw(CloudTimeoutError()))[1])


class TestE6FrameSelection:
    def test_unselected_frame_no_cloud_local_result(self):
        clock = _Clock()
        cloud_calls: list = []
        edge_calls: list = []
        svc, _ = _svc(db=None, cloud=_counting_cloud(cloud_calls, x4(1, "civilian")),
                      edge=_edge_spy(edge_calls), clock=clock,
                      frame_selector=_sel(clock))
        # 第 1 帧：选定 → 云（本地也兜底跑一次）
        assert svc.detect_frame(None)  # 云成功返回云转换
        assert cloud_calls == [1] and len(edge_calls) == 1
        # 第 2 帧（同一时刻窗口未翻）：未选定 → 不触云、本地
        out2 = svc.detect_frame(None)
        assert cloud_calls == [1]                      # 未选定不调云
        assert len(edge_calls) == 2                    # 本地仍逐帧
        assert _first(out2).bbox == [10, 20, 30, 40]   # 返回本地结果
        assert svc.machine.consecutive_failures == 0 and svc.machine.state == CLOUD_PREFERRED  # 未计云失败

    def test_selected_cloud_success_returns_cloud(self):
        clock = _Clock()
        cloud_calls: list = []
        edge_calls: list = []
        svc, _ = _svc(db=None, cloud=_counting_cloud(cloud_calls, x4(1, "civilian")),
                      edge=_edge_spy(edge_calls), clock=clock, frame_selector=_sel(clock))
        out = svc.detect_frame(None)                   # 选定帧
        assert _first(out).bbox == [1, 2, 3, 4]        # 云转换结果（override 本地）
        assert cloud_calls == [1] and len(edge_calls) == 1   # 本地兜底仍跑了一次
        assert svc.machine.consecutive_failures == 0

    def test_selected_cloud_fail_reuses_local_and_counts(self):
        clock = _Clock()
        cloud_calls: list = []
        edge_calls: list = []
        svc, _ = _svc(db=None, cloud=_cloud_fail(cloud_calls),
                      edge=_edge_spy(edge_calls), clock=clock, frame_selector=_sel(clock))
        out = svc.detect_frame(None)                   # 选定帧，云失败
        assert _first(out).bbox == [10, 20, 30, 40]    # 复用本帧本地（不重复本地推理）
        assert cloud_calls == [1] and len(edge_calls) == 1   # 本地只跑一次（兜底）
        assert svc.machine.consecutive_failures == 1   # 真云失败计入熔断计数

    def test_disabled_no_selector_every_frame_cloud(self):
        """默认未注入选帧器：每帧发云、云成功不跑本地（既有语义回归）。"""
        clock = _Clock()
        cloud_calls: list = []
        edge_calls: list = []
        svc, _ = _svc(db=None, cloud=_counting_cloud(cloud_calls, x4(1, "civilian")),
                      edge=_edge_spy(edge_calls), clock=clock)   # frame_selector=None
        svc.detect_frame(None)
        svc.detect_frame(None)
        assert cloud_calls == [1, 1]                   # 每帧都发云
        assert edge_calls == []                        # 云成功不双跑本地