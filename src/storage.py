"""
src/storage.py — Snapshot 云存储层 (Phase 2.6)
============================================
目的: 把 edge 本地 snapshot jpg 映射为一个可公开访问的 URL (picURL),
      供 DingTalk 图片报警及后续 VLM/LLM 消费图片证据链。

设计:
  StorageProvider (抽象)
    ├── LocalStorageProvider   (测试/单机: base_url + 相对路径 → URL)
    └── OSSStorageProvider     (阿里云 OSS: 真实上传; 图片/快照公网 URL; 音频签名 URL;
                                evidence 证据独立私有桶 + 短期 GET 签名 URL)

不做:
  - 不绑定具体云厂商(插件化)
  - 不硬编码 AK/SK(env / config 注入)
  - 不启动 HTTP server(Local 只负责 URL 映射; 实际静态服务由外部 server 挂载)
  - 不自动创建 Bucket; 不修改真实 OSS ACL; 不打印真实 AK/SK/Bucket/Endpoint
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


class StorageSignError(Exception):
    """媒体访问签名 / 独立 evidence Bucket 失败（环境配置缺失或 OSS 签名异常）。

    D 组服务端层可将本异常映射为 HTTP 503（OSS 不可用 / 独立 evidence 桶未配置）。
    """


class EvidenceBucketNotConfiguredError(StorageSignError):
    """独立 evidence Bucket（OSS_EVIDENCE_BUCKET）未配置。失败关闭，不回退原桶。"""


class OSSStorageProvider(StorageProvider):
    """阿里云 OSS 存储提供者（真实实现）。

    上传: 通过 oss2 写对象; 图片/快照返回公网 URL, 音频返回短期签名 URL。
    证据(evidence)上传与签名: 使用**独立 evidence Bucket**（OSS_EVIDENCE_BUCKET），
    与 V4.3 图片/音频原桶分离; evidence 桶未配置时失败关闭, 不回退原桶。
    机密仅来自环境变量, 绝不写入 yaml / 不写死代码。
    """

    name = "aliyun_oss"

    EVIDENCE_PREFIX = "evidence"
    _EVIDENCE_BUCKET_ENV = "OSS_EVIDENCE_BUCKET"

    def __init__(self, config: StorageConfig):
        self.config = config

    def _credentials_for(self, prefix: str):
        """解析某前缀使用的 (ak, sk, endpoint, bucket_name)。

        - evidence 前缀必须使用独立桶 OSS_EVIDENCE_BUCKET；缺失即抛
          EvidenceBucketNotConfiguredError（失败关闭, 不回退原桶/默认桶）。
        - 其他前缀沿用原有逻辑: 桶取 env OSS_BUCKET, 缺失回退 config.bucket。
        - 机密(ak/sk)仅来自 env, 不回退 config。
        """
        ak = os.environ.get("OSS_ACCESS_KEY") or ""
        sk = os.environ.get("OSS_ACCESS_SECRET") or ""
        ep = os.environ.get("OSS_ENDPOINT") or (self.config.endpoint or "")
        if prefix == self.EVIDENCE_PREFIX:
            bucket_name = os.environ.get(self._EVIDENCE_BUCKET_ENV) or ""
            if not bucket_name:
                raise EvidenceBucketNotConfiguredError(
                    f"OSS evidence 桶未配置: 需 env {self._EVIDENCE_BUCKET_ENV}（不回退原桶/默认桶）")
        else:
            bucket_name = os.environ.get("OSS_BUCKET") or (self.config.bucket or "")
        return ak, sk, ep, bucket_name

    def upload(self, path: str, prefix: str = "snapshots") -> Dict:
        """把本地文件上传到阿里云 OSS, 返回可公网访问的 URL。

        默认对象前缀 snapshots/(图片/快照); prefix 可传 "audio"(ASR 音频) 或 "evidence"(取证视频)。
        evidence 前缀使用独立 evidence 桶(与签名同桶); 图片/音频继续使用原桶,
        不改变 V4.3 已有图片/音频上传行为。
        AK/SK 仅从环境变量读取(绝不写入 yaml / 不写死代码):
          OSS_ACCESS_KEY / OSS_ACCESS_SECRET (必填, env-only)
          OSS_BUCKET / OSS_ENDPOINT (env 优先, 缺失回退 config 非机密字段)
          OSS_EVIDENCE_BUCKET (evidence 前缀必填; 缺失失败关闭, 不回退原桶)
        """
        try:
            if not path or not Path(path).exists():
                return {"ok": False, "error": f"file not found: {path}"}
            ak, sk, ep, bucket_name = self._credentials_for(prefix)
            missing = []
            if not ak:
                missing.append("OSS_ACCESS_KEY")
            if not sk:
                missing.append("OSS_ACCESS_SECRET")
            if not ep:
                missing.append("OSS_ENDPOINT")
            if not bucket_name and prefix != self.EVIDENCE_PREFIX:
                missing.append("OSS_BUCKET")
            if missing:
                return {"ok": False,
                        "error": "OSS 配置缺失: " + ", ".join(missing)}
            import oss2  # 惰性导入, 避免 blob 依赖影响其它 provider
            auth = oss2.Auth(ak, sk)
            bucket = oss2.Bucket(auth, ep, bucket_name)
            filename = Path(path).name
            object_key = f"{prefix or 'snapshots'}/{filename}"
            # 上传头: 音频对象固定 Content-Type=audio/wav + Content-Disposition=inline,
            # 供 DashScope ASR 识别 URL(不倒逼下载为 attachment);
            # 图片/快照沿用 oss2 按扩展名推断, 保留既有行为。
            headers = {}
            if prefix == "audio":
                headers["Content-Type"] = "audio/wav"
                headers["Content-Disposition"] = "inline"
            bucket.put_object_from_file(object_key, path, headers=headers or None)
            # 输出 URL:
            #   audio: 用签名 URL(GET, 3600s), 无需桶公共读, 供 DashScope ASR 拉取;
            #   图片/快照: 沿用公网 https://{bucket}.{endpoint}/{object_key} 行为。
            if prefix == "audio":
                # slash_safe=True: sign_url 生成 URL 时保留 path 中的 "/"(audio/key),
                # 与对象真实 key 的斜杠层级一致; 若用默认(slash_safe=False)会把 "/"
                # 编成 %2F, OSS 按单段对象重建资源, 与签名不匹配 → SignatureDoesNotMatch。
                # 签名本就按斜杠形式计算, 故只在 URL path 层用 slash_safe, 不事后改 URL。
                url = bucket.sign_url("GET", object_key, expires=3600, slash_safe=True)
            else:
                url_ep = str(ep).rstrip("/")
                if "://" in url_ep:
                    url_ep = url_ep.split("://", 1)[1]
                url = f"https://{bucket_name}.{url_ep}/{object_key}"
            return {"ok": True, "url": url, "provider": self.name}
        except EvidenceBucketNotConfiguredError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def sign_get_url(self, object_key: str, ttl: int = 300) -> str:
        """为**独立 evidence 桶**中的对象生成短期 GET 签名 URL（供媒体授权访问）。

        - 只提供 GET；只用独立 evidence Bucket（与 evidence 上传同桶）。
        - TTL 默认 300 秒，可传自定义；必须是合理正整数。
        - object_key 非空；不接受客户端传 Bucket/Endpoint/URL（本方法内部定桶）。
        - 不把签名 URL 写入数据库。
        - 返回 URL 字符串；配置缺失或 OSS 失败抛 StorageSignError（D 组映射 503）。
        - 失败关闭：evidence 桶/凭据缺失时不回退原桶。
        """
        if not object_key or not isinstance(object_key, str):
            raise ValueError("object_key 必须为非空字符串")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise ValueError(f"ttl 必须为正整数, got {ttl!r}")
        try:
            ak, sk, ep, bucket_name = self._credentials_for(self.EVIDENCE_PREFIX)
            if not ak or not sk or not ep:
                raise StorageSignError("OSS evidence 配置缺失: OSS_ACCESS_KEY / "
                                       "OSS_ACCESS_SECRET / OSS_ENDPOINT")
            import oss2  # 惰性导入
            auth = oss2.Auth(ak, sk)
            bucket = oss2.Bucket(auth, ep, bucket_name)
            # 复用既有 slash_safe=True 语义（保留 path 中的 "/"），避免 SignatureDoesNotMatch。
            return bucket.sign_url("GET", object_key, expires=ttl, slash_safe=True)
        except StorageSignError:
            raise
        except Exception as exc:
            raise StorageSignError(f"OSS 签名失败: {exc}") from exc


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