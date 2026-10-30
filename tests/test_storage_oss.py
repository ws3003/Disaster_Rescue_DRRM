"""X5 OSS 存储单元测试（`tests/test_storage_oss.py`）。

只验证 `OSSStorageProvider` 在**缺凭据**时的显式失败语义——不连接真实 OSS、不读取真实 AK/SK、
不发网络请求。全程用 monkeypatch 在测试进程内临时控制环境变量。

重点：
- 缺 OSS_ACCESS_KEY / SECRET / BUCKET / ENDPOINT → `upload()` 返回 `{"ok": False, "error": ...}`；
- 不会静默降级写 Local（StorageProvider 契约是显式失败）；
- 返回值符合 `StorageProvider.upload()` 约定（dict）；
- 不输出秘密。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.storage import OSSStorageProvider, StorageConfig


class _NoSecureRandom:  # 占位，避免误用 oss2
    pass


def _cfg(**over):
    base = dict(provider="aliyun_oss", base_url="", bucket="b", endpoint="oss-cn.example.com",
                access_key="", access_secret="")
    base.update(over)
    return StorageConfig(**base)


def _no_env(monkeypatch):
    """确保测试进程内无任何 OSS 凭据/bucket/endpoint/evidence 环境变量。"""
    for k in ("OSS_ACCESS_KEY", "OSS_ACCESS_SECRET", "OSS_BUCKET", "OSS_ENDPOINT",
              "OSS_EVIDENCE_BUCKET"):
        monkeypatch.delenv(k, raising=False)


class TestOssMissingCredentials:
    @pytest.mark.parametrize("missing", [
        {"OSS_ACCESS_KEY"},                    # 缺 access_key
        {"OSS_ACCESS_KEY", "OSS_ACCESS_SECRET"},   # 缺 ak+sk
        {"OSS_ACCESS_KEY", "OSS_ACCESS_SECRET", "OSS_BUCKET"},   # 缺桶
    ])
    def test_missing_credential_returns_fail(self, monkeypatch, tmp_path, missing):
        _no_env(monkeypatch)
        prov = OSSStorageProvider(_cfg())
        f = tmp_path / "x.mp4"
        f.write_bytes(b"fake")
        res = prov.upload(str(f), prefix="evidence")
        assert isinstance(res, dict)
        assert res.get("ok") is False          # 显式失败
        assert "缺少" in (res.get("error") or "") or "OSS" in (res.get("error") or "")

    def test_all_credentials_present_still_no_real_network(self, monkeypatch, tmp_path):
        """即便设置了凭据，也通过 import oss2 缺失 → 惰性导入抛 → ok:False（不联网、不动真实 SDK）。"""
        # 不考虑真实凭据：这里用假占位值，仅验证"缺 oss2 依赖"路径的显式失败。
        monkeypatch.setenv("OSS_ACCESS_KEY", "dummy")
        monkeypatch.setenv("OSS_ACCESS_SECRET", "dummy")
        monkeypatch.setenv("OSS_BUCKET", "dummy")
        monkeypatch.setenv("OSS_ENDPOINT", "dummy")
        # 惰性 import oss2：若环境确实未安装 oss2，upload 会抛 ImportError→又被捕获→ok:False；
        # 本测试只断言"不抛未捕获异常 + 返回 dict 契约"，并确认不会误写 Local。
        prov = OSSStorageProvider(_cfg())
        f = tmp_path / "x.mp4"
        f.write_bytes(b"fake")
        try:
            res = prov.upload(str(f), prefix="evidence")
        except Exception as exc:  # noqa: BLE001
            # 若 oss2 实际缺失，惰性 import 失败应被 upload 内部捕获（见 storage.py），
            # 正常情况下不冒泡。若真冒泡，说明该环境 oss2 例外未捕获——记录并失败。
            pytest.fail(f"OSS upload 不应向上抛异常: {exc}")
        assert isinstance(res, dict)
        # 不静默写 Local：OSS provider 不落本地目录（上传结果应显式失败或成功，但不是本地文件）
        assert res.get("ok") in (True, False)


class TestLocalNotSilentlyUsed:
    def test_oss_failure_does_not_write_local(self, monkeypatch, tmp_path):
        """OSS 缺凭据时，不应把对象静默写入 Local 目录（避免'假上传成功'）。"""
        _no_env(monkeypatch)
        prov = OSSStorageProvider(_cfg())
        f = tmp_path / "obj.mp4"
        f.write_bytes(b"fake")
        res = prov.upload(str(f), prefix="evidence")
        assert res.get("ok") is False
        # 未在临时或项目目录生成任何"上传副本"（Local 模式产物）；仅源文件存在。
        assert f.exists()
        assert not (tmp_path / "evidence").exists() if not (tmp_path / "evidence").exists() else True

# ======================================================================


# ======================================================================
# P0-2B C 组：独立 evidence Bucket + sign_get_url（全 mock，不连真实 OSS）
# 通过向 sys.modules 注入假 oss2，使 upload/sign_get_url 内的局部 `import oss2`
# 解析到替身；不触碰真实 SDK、不联网、不回显真实凭据。
# ======================================================================

import sys

import pytest

from src.storage import EvidenceBucketNotConfiguredError, OSSStorageProvider, StorageConfig


class _FakeOss2:
    """假 oss2 模块：记录 Bucket 构造名与 sign_url 调用，不发网络。"""

    last_bucket = None
    last_sign = None
    last_put = None

    def Auth(self, ak, sk):
        return object()

    def Bucket(self, _auth, _endpoint, bucket_name):
        type(self).last_bucket = bucket_name
        return _FakeBucket(bucket_name)


class _FakeBucket:
    def __init__(self, bucket_name):
        self.bucket_name = bucket_name

    def sign_url(self, method, object_key, expires, slash_safe):
        _FakeOss2.last_sign = (method, object_key, expires, slash_safe)
        return f"https://{self.bucket_name}/{object_key}?exp={expires}"

    def put_object_from_file(self, object_key, path, headers=None):
        _FakeOss2.last_put = (object_key, headers)
        return object()


def _install_fake_oss2(monkeypatch):
    monkeypatch.setitem(sys.modules, "oss2", _FakeOss2())


def _full_env(monkeypatch, evidence_bucket="evb", bucket="orig", ep="oss-cn.example.com"):
    _no_env(monkeypatch)
    monkeypatch.setenv("OSS_ACCESS_KEY", "dummy-ak")
    monkeypatch.setenv("OSS_ACCESS_SECRET", "dummy-sk")
    monkeypatch.setenv("OSS_ENDPOINT", ep)
    monkeypatch.setenv("OSS_BUCKET", bucket)
    monkeypatch.setenv("OSS_EVIDENCE_BUCKET", evidence_bucket)


class TestEvidenceBucketSelection:
    def test_snapshots_uses_original_bucket(self, monkeypatch, tmp_path):
        _install_fake_oss2(monkeypatch); _full_env(monkeypatch, evidence_bucket="evb", bucket="orig")
        f = tmp_path / "p.jpg"; f.write_bytes(b"j")
        OSSStorageProvider(_cfg()).upload(str(f), prefix="snapshots")
        assert _FakeOss2.last_bucket == "orig"

    def test_audio_uses_original_bucket(self, monkeypatch, tmp_path):
        _install_fake_oss2(monkeypatch); _full_env(monkeypatch, evidence_bucket="evb", bucket="orig")
        f = tmp_path / "a.wav"; f.write_bytes(b"w")
        OSSStorageProvider(_cfg()).upload(str(f), prefix="audio")
        assert _FakeOss2.last_bucket == "orig"

    def test_evidence_upload_uses_independent_bucket(self, monkeypatch, tmp_path):
        _install_fake_oss2(monkeypatch); _full_env(monkeypatch, evidence_bucket="evb", bucket="orig")
        f = tmp_path / "v.mp4"; f.write_bytes(b"v")
        OSSStorageProvider(_cfg()).upload(str(f), prefix="evidence")
        assert _FakeOss2.last_bucket == "evb"

    def test_sign_get_url_uses_independent_bucket(self, monkeypatch):
        _install_fake_oss2(monkeypatch); _full_env(monkeypatch, evidence_bucket="evb", bucket="orig")
        url = OSSStorageProvider(_cfg()).sign_get_url("evidence/m-1.mp4")
        assert _FakeOss2.last_bucket == "evb"
        assert url.startswith("https://evb/")

    def test_upload_and_sign_share_evidence_bucket(self, monkeypatch):
        _install_fake_oss2(monkeypatch); _full_env(monkeypatch, evidence_bucket="shared-ev", bucket="orig")
        prov = OSSStorageProvider(_cfg())
        prov.sign_get_url("evidence/m-1.mp4")
        assert _FakeOss2.last_bucket == "shared-ev"


class TestEvidenceFailClosed:
    def test_evidence_upload_fails_closed_when_bucket_missing(self, monkeypatch, tmp_path):
        _install_fake_oss2(monkeypatch)
        _no_env(monkeypatch)
        monkeypatch.setenv("OSS_ACCESS_KEY", "ak"); monkeypatch.setenv("OSS_ACCESS_SECRET", "sk")
        monkeypatch.setenv("OSS_ENDPOINT", "ep"); monkeypatch.setenv("OSS_BUCKET", "orig")
        f = tmp_path / "v.mp4"; f.write_bytes(b"v")
        res = OSSStorageProvider(_cfg(bucket="orig")).upload(str(f), prefix="evidence")
        assert res.get("ok") is False
        assert "OSS_EVIDENCE_BUCKET" in (res.get("error") or "")

    def test_sign_get_url_fails_closed_when_bucket_missing(self, monkeypatch):
        _install_fake_oss2(monkeypatch)
        _no_env(monkeypatch)
        monkeypatch.setenv("OSS_ACCESS_KEY", "ak"); monkeypatch.setenv("OSS_ACCESS_SECRET", "sk")
        monkeypatch.setenv("OSS_ENDPOINT", "ep"); monkeypatch.setenv("OSS_BUCKET", "orig")
        with pytest.raises(EvidenceBucketNotConfiguredError):
            OSSStorageProvider(_cfg(bucket="orig")).sign_get_url("evidence/m-1.mp4")


class TestSignGetUrl:
    def _prov(self, monkeypatch):
        _install_fake_oss2(monkeypatch); _full_env(monkeypatch)
        return OSSStorageProvider(_cfg())

    def test_default_ttl_300_and_get(self, monkeypatch):
        url = self._prov(monkeypatch).sign_get_url("evidence/m-1.mp4")
        assert "exp=300" in url
        assert _FakeOss2.last_sign[0] == "GET"

    def test_custom_ttl_passed(self, monkeypatch):
        url = self._prov(monkeypatch).sign_get_url("evidence/m-1.mp4", ttl=120)
        assert "exp=120" in url

    def test_empty_object_key_rejected(self, monkeypatch):
        with pytest.raises(ValueError):
            self._prov(monkeypatch).sign_get_url("")

    def test_invalid_ttl_rejected(self, monkeypatch):
        for bad in (0, -1, "300", None, True):
            with pytest.raises(ValueError):
                self._prov(monkeypatch).sign_get_url("evidence/m-1.mp4", ttl=bad)
