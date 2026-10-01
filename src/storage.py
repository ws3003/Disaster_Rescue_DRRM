"""
src/storage.py — Snapshot 云存储层 (Phase 2.6)
============================================
目的: 把 edge 本地 snapshot jpg 映射为一个可公开访问的 URL (picURL),
      供 DingTalk 图片报警及后续 VLM/LLM 消费图片证据链。

设计:
  StorageProvider (抽象)
    ├── LocalStorageProvider   (测试/单机: base_url + 相对路径 → URL)
    └── OSSStorageProvider     (预留: 阿里云 OSS 占位, 未接入, 抛 NotImplemented)

不做:
  - 不绑定具体云厂商(插件化)
  - 不硬编码 AK/SK(env / config 注入)
  - 不启动 HTTP server(Local 只负责 URL 映射; 实际静态服务由外部 server 挂载)
"""

from __future__ import annotations

import os
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class StorageConfig:
    provider: str = "local"
    base_url: str = "http://localhost:8000"
    bucket: Optional[str] = None
    endpoint: Optional[str] = None
    access_key: Optional[str] = None
    access_secret: Optional[str] = None


def _load_storage_config(path: str = "config/storage.yaml") -> StorageConfig:
    """读取 storage 配置; 缺失项用 env / 默认兜底。绝不硬编码 AK/SK。"""
    cfg = StorageConfig()
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        storage = data.get("storage", {}) or {}
        cfg.provider = str(storage.get("provider", "local") or "local")
        local = storage.get("local", {}) or {}
        cfg.base_url = str(local.get("base_url") or os.getenv("STORAGE_BASE_URL")
                            or cfg.base_url)
        oss = storage.get("aliyun_oss", {}) or {}
        cfg.bucket = oss.get("bucket") or os.getenv("OSS_BUCKET")
        cfg.endpoint = oss.get("endpoint") or os.getenv("OSS_ENDPOINT")
        cfg.access_key = oss.get("access_key") or os.getenv("OSS_ACCESS_KEY")
        cfg.access_secret = oss.get("access_secret") or os.getenv("OSS_ACCESS_SECRET")
    except Exception:
        traceback.print_exc()
    return cfg


class StorageProvider:
    """存储 Provider 抽象接口。"""

    name = "abstract"

    def upload(self, path: str, prefix: str = "snapshots") -> Dict:
        raise NotImplementedError

    @staticmethod
    def _is_abs_url(path: str) -> bool:
        return path.startswith("http://") or path.startswith("https://")


class LocalStorageProvider(StorageProvider):
    """本地 -> HTTP URL 映射 (测试 / 单机)。

    把 data/snapshots/a.jpg 映射为 {base_url}/snapshots/a.jpg。
    实际 HTTP 服务由外部 server(如 FastAPI 静态挂载)提供; 本类只产 URL。
    """

    name = "local"

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = str(base_url).rstrip("/")

    def upload(self, path: str, prefix: str = "snapshots") -> Dict:
        try:
            if not path or not Path(path).exists():
                return {"ok": False, "error": f"snapshot not found: {path}"}
            p = str(Path(path))
            # 取 "snapshots/<file>" 相对部分
            fs = p.replace("\\", "/")
            key = fs.split("/snapshots/")[-1] if "/snapshots/" in fs else Path(p).name
            url = f"{self.base_url}/snapshots/{key}"
            return {"ok": True, "url": url, "provider": self.name}
        except Exception as exc:
            return {"ok": False, "error": f"local upload failed: {exc}"}


class OSSStorageProvider(StorageProvider):
    """阿里云 OSS 预留接口 (未实现)。

    接入需 OSS SDK(oss2) + AK/SK(env)。当前仅抛 NotImplementedError,
    避免产生"假上传成功"。后续接入时在此实现。
    """

    name = "aliyun_oss"

    def __init__(self, config: StorageConfig):
        self.config = config

    def upload(self, path: str, prefix: str = "snapshots") -> Dict:
        """把本地文件上传到阿里云 OSS, 返回可公网访问的 URL。

        默认对象前缀 snapshots/(图片/快照); prefix 可传 "audio" 等供 ASR 音频复用,
        不改变已有图片上传(bucket/URL 组装不变)。
        AK/SK 仅从环境变量读取(绝不写入 yaml / 不写死代码):
          OSS_ACCESS_KEY / OSS_ACCESS_SECRET (必填, env-only)
          OSS_BUCKET / OSS_ENDPOINT (env 优先, 缺失回退 config 非机密字段)
        """
        try:
            if not path or not Path(path).exists():
                return {"ok": False, "error": f"file not found: {path}"}
            # 机密仅来自 env, 不回退 config
            access_key = os.environ.get("OSS_ACCESS_KEY")
            access_secret = os.environ.get("OSS_ACCESS_SECRET")
            bucket_name = os.environ.get("OSS_BUCKET") or self.config.bucket
            endpoint = os.environ.get("OSS_ENDPOINT") or self.config.endpoint
            if not all((access_key, access_secret, bucket_name, endpoint)):
                return {"ok": False,
                        "error": "OSS 配置缺失: 需 env OSS_ACCESS_KEY / OSS_ACCESS_SECRET / "
                                 "OSS_BUCKET / OSS_ENDPOINT"}
            import oss2  # 惰性导入, 避免 blob 依赖影响其它 provider
            auth = oss2.Auth(access_key, access_secret)
            bucket = oss2.Bucket(auth, endpoint, bucket_name)
            filename = Path(path).name
            object_key = f"{prefix or 'snapshots'}/{filename}"
            bucket.put_object_from_file(object_key, path)
            # 输出: https://{bucket}.{endpoint}/{object_key}  (endpoint 去掉协议前缀)
            ep = str(endpoint).rstrip("/")
            if "://" in ep:
                ep = ep.split("://", 1)[1]
            url = f"https://{bucket_name}.{ep}/{object_key}"
            return {"ok": True, "url": url, "provider": self.name}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def create_storage(config_path: str = "config/storage.yaml") -> StorageProvider:
    """按配置创建 StorageProvider。"""
    cfg = _load_storage_config(config_path)
    if cfg.provider == "local":
        return LocalStorageProvider(base_url=cfg.base_url)
    if cfg.provider in ("oss", "aliyun_oss"):
        return OSSStorageProvider(config=cfg)
    return LocalStorageProvider(base_url=cfg.base_url)


def upload_snapshot(image_path: str, config_path: str = "config/storage.yaml",
                    prefix: str = "snapshots") -> Dict:
    """便捷入口: 上传文件, 返回 {ok, url?, error?}。

    prefix 默认 snapshots(维持图片/快照行为); ASR 音频可传 prefix="audio"。
    """
    provider = create_storage(config_path)
    return provider.upload(image_path, prefix=prefix)


if __name__ == "__main__":
    import tempfile
    tp = os.path.join(tempfile.gettempdir(), "test_snap.jpg")
    Path(tp).write_bytes(b"fake")
    print(upload_snapshot(tp))