"""V4.4 阶段2.2 J-1B —— OSS 生产 Uploader 注入 fail-closed 专项测试。

覆盖（Mock/Fake，不连真实 OSS、不真实上传、不需真实 AK/SK）：
- E8 关闭 → 不创建 Provider（_make_e8_uploader 返回 None，不调 build_uploader）。
- E8 开启 + provider=aliyun_oss → 返回真实 OSSStorageProvider。
- Provider 类型不匹配（build_uploader 返回 LocalStorageProvider）→ fail-closed 返回 None，
  绝不把本地上传成功误判为 OSS 成功；并输出可诊断日志。
- 缺 OSS_EVIDENCE_BUCKET → OSSStorageProvider.upload(evidence) 返回 ok:False（fail-closed）。
- 缺 AK/SK → 返回 ok:False（fail-closed），不输出密钥。
- EdgeE7E8 的 uploader 注入接口保持(签名含 uploader 参数) —— 既有 e8_x5 集成已证注入贯通。
- sensor.enabled=False → _build_e7e8 仍返回 None，不建 Provider。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.edge_client import _build_e7e8, _make_e8_uploader
from src.storage import LocalStorageProvider, OSSStorageProvider, StorageConfig
from src.e7e8_integration import EdgeE7E8


def _oss_provider() -> OSSStorageProvider:
    # OSSStorageProvider 构造只存 config，不解析 env、不连网。
    return OSSStorageProvider(StorageConfig(provider="aliyun_oss", bucket="x", endpoint="y"))


def test_e8_disabled_no_provider(monkeypatch):
    called = []
    monkeypatch.setattr("scripts.e7e8_runner.build_uploader",
                        lambda *a, **k: called.append(a) or _oss_provider())
    assert _make_e8_uploader(False) is None
    assert called == []                      # E8 关不调 build_uploader -> 不建 Provider


def test_e8_enabled_oss_provider(monkeypatch):
    prov = _oss_provider()
    monkeypatch.setattr("scripts.e7e8_runner.build_uploader", lambda _: prov)
    out = _make_e8_uploader(True)
    assert out is prov
    assert isinstance(out, OSSStorageProvider)


def test_provider_type_mismatch_fail_closed(monkeypatch, capsys):
    # build_uploader 返回 LocalStorageProvider（create_storage 静默回退的典型情形）
    monkeypatch.setattr("scripts.e7e8_runner.build_uploader", lambda _: LocalStorageProvider())
    out = _make_e8_uploader(True)
    assert out is None                       # 不注入, 杜绝 Local ok:True 被误判 OSS 成功
    log = capsys.readouterr().out
    assert "OSSStorageProvider" in log or "fail-closed" in log   # 有可诊断输出


def test_build_e7e8_sensor_disabled_returns_none(monkeypatch):
    # sensor.enabled=False 早退，不加载 E8/不建 Provider；配置为准，无需真实 camera/config 文件。
    monkeypatch.setenv("C4_API_TOKEN", "noop")
    out = _build_e7e8({"sensor": {"enabled": False}}, None, "config/edge.yaml")
    assert out is None


def test_missing_evidence_bucket_not_fakes_success(monkeypatch):
    for k in ("OSS_EVIDENCE_BUCKET", "OSS_ACCESS_KEY", "OSS_ACCESS_SECRET", "OSS_ENDPOINT", "OSS_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    f = os.path.join(tempfile.gettempdir(), "j1b_ev.mp4")
    open(f, "w").write("x")
    try:
        res = _oss_provider().upload(f, prefix="evidence")
    finally:
        try:
            os.remove(f)
        except OSError:
            pass
    assert res.get("ok") is not True                        # 缺 evidence 桶 fail-closed
    assert "OSS_EVIDENCE_BUCKET" in (res.get("error") or "")


def test_missing_aksk_fails_closed(monkeypatch):
    monkeypatch.setenv("OSS_EVIDENCE_BUCKET", "evb")
    monkeypatch.setenv("OSS_ENDPOINT", "ep")
    monkeypatch.delenv("OSS_ACCESS_KEY", raising=False)
    monkeypatch.delenv("OSS_ACCESS_SECRET", raising=False)
    f = os.path.join(tempfile.gettempdir(), "j1b_ak.mp4")
    open(f, "w").write("x")
    try:
        res = _oss_provider().upload(f, prefix="evidence")
    finally:
        try:
            os.remove(f)
        except OSError:
            pass
    assert res.get("ok") is False                           # 缺 AK/SK fail-closed
    assert "OSS_ACCESS_KEY" in (res.get("error") or "") and "SECRET" in (res.get("error") or "")
    assert "OSS_ACCESS_KEY=" not in repr(res)               # 不输出密钥值


def test_edge_e7e8_uploader_interface_unchanged():
    # EdgeE7E8 仍接受 uploader 注入参数（接口未被 J-1B 改动）。
    import inspect
    assert "uploader" in inspect.signature(EdgeE7E8.__init__).parameters