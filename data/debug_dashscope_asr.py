"""最小复现 v2: 对比三种 OSS URL 的 DashScope ASR 结果(阶段2/3)。

在 Pi 跑(需要真实 ASR_API_KEY + OSS 凭据):
    PYTHONPATH=. python data/debug_dashscope_asr.py data/audio/asr_content_test.wav

对同一条上传后的对象生成三种 URL 分别调 DashScope:
  A) 原样 http  签名 URL
  B) http -> https 签名 URL
  C) 去除签名参数(若失败记录)
每次打印 URL / status_code / body。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402
from src.storage import upload_snapshot  # noqa: E402

ASR_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/recognition"


def call(url: str, key: str, model: str, tag: str) -> int:
    payload = {
        "model": model,
        "input": {"file_urls": [url]},
        "parameters": {"format": "wav", "sample_rate": 16000},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    print(f"\n===== [{tag}] file_url = {url}")
    try:
        resp = requests.post(ASR_URL, json=payload, headers=headers, timeout=30)
        print(f"[{tag}] status: {resp.status_code}")
        print(f"[{tag}] body: {resp.text[:1200]}")
        return resp.status_code
    except Exception as exc:  # noqa: BLE001
        print(f"[{tag}] exception: {type(exc).__name__}: {exc}")
        return -1


def main(wav: str) -> int:
    if not wav or not os.path.exists(wav):
        print("usage: python data/debug_dashscope_asr.py <wav>")
        return 2
    key = os.environ.get("ASR_API_KEY", "").strip()
    model = os.environ.get("ASR_MODEL", "").strip() or "paraformer-v2"
    if not key:
        print("[DEBUG] missing ASR_API_KEY")
        return 2

    up = upload_snapshot(wav, prefix="audio")
    print("[DEBUG] upload:", up)
    if not up.get("ok"):
        print("[DEBUG] upload failed:", up.get("error"))
        return 1
    http_url = up["url"]

    # A: 原样(http 签名)
    call(http_url, key, model, "A-http-signed")
    # B: 仅把 scheme 换成 https(签名/data 不变, scheme 不参与签名)
    https_url = ("https://" + http_url[len("http://"):]) if http_url.startswith("http://") else http_url
    call(https_url, key, model, "B-https-signed")
    # C: 去掉签名参数(query), 仅保留桶公共读路径(失败也记录)
    if "?" in http_url:
        no_sig = http_url.split("?", 1)[0]
        call(no_sig, key, model, "C-no-sig")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))