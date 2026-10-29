"""X4/E6 独立 YOLO HTTP 服务端（`server/x4_predict.py`）。

独立 FastAPI 应用（**不 import / 不修改 `server/api.py`**；不注册进 `/api/v1/analyze`）。
`POST /predict`：multipart `file`(image/jpeg) + `model_id`(白名单别名)。
- 成功响应兼容 `src/x4/http_schema.parse_predict_response`：{model_id, detections[], latency_ms}，
  检测项 {class_id, class_name, confidence, bbox:[x1,y1,x2,y2] 像素}；无目标→200 + detections:[]。
- 错误响应统一 envelope {code, message, data:null, meta:{}}，**不泄 traceback/token/权重路径**。
- 鉴权：Bearer token 仅从环境变量 `X4_API_TOKEN` 读取；未配置 token → 拒绝远程绑定（仅回环）。
- 大小上限默认 10 MiB（有界读取并检测超限）→ 413。
- runner 可注入（测试用 FakeRunner）；默认按配置白名单懒加载 `ModelRunner`。
- 单进程、单 worker 考量；不实现 GPU 并发调度。
"""

from __future__ import annotations

import hmac
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import cv2
import yaml
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.x4.contracts import Detection, validate_detections
from src.x4.model_runner import ModelRunner, ModelRunnerError

DEFAULT_CONFIG = "config/x4_server.yaml"
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MiB
_CHUNK = 64 * 1024


class X4ServerConfigError(ValueError):
    """服务端配置缺失/非法。"""


# ----------------------------------------------------------------------
# 配置读取 + 校验
# ----------------------------------------------------------------------
def load_server_config(path: str = DEFAULT_CONFIG) -> Dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise X4ServerConfigError(f"无法读取配置: {path}") from exc
    root = raw.get("x4_server")
    if not isinstance(root, dict):
        raise X4ServerConfigError("x4_server 段缺失")
    auth = root.get("auth") or {}
    if not isinstance(auth, dict) or not isinstance(auth.get("token_env"), str) or not auth["token_env"].strip():
        raise X4ServerConfigError("auth.token_env 缺失或非法")
    limits = root.get("limits") or {}
    max_file = limits.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)
    if isinstance(max_file, bool) or not isinstance(max_file, int) or max_file <= 0:
        raise X4ServerConfigError(f"limits.max_file_bytes 非法: {max_file!r}")
    inf = root.get("inference") or {}
    conf = inf.get("conf", 0.25)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) \
            or not math.isfinite(float(conf)) or not (0.0 < float(conf) <= 1.0):
        raise X4ServerConfigError(f"inference.conf 非法: {conf!r}")
    imgsz = inf.get("imgsz", 640)
    if isinstance(imgsz, bool) or not isinstance(imgsz, int) or imgsz <= 0:
        raise X4ServerConfigError(f"inference.imgsz 非法: {imgsz!r}")
    models = root.get("models")
    if not isinstance(models, dict) or not models:
        raise X4ServerConfigError("models 白名单缺失或为空")
    for mid, wpath in models.items():
        if not isinstance(mid, str) or not mid.strip() or not isinstance(wpath, str) or not wpath.strip():
            raise X4ServerConfigError(f"models 项非法: {mid!r}={wpath!r}")
    return {
        "token_env": str(auth["token_env"]).strip(),
        "max_file_bytes": int(max_file),
        "conf": float(conf),
        "imgsz": int(imgsz),
        "models": {str(m).strip(): str(w).strip() for m, w in models.items()},
    }


def resolve_weights(models: Dict[str, str], model_id: str) -> Optional[str]:
    """按白名单 model_id 返回权重路径；仅允许 env 覆盖该别名对应路径，不接受客户端传入路径。"""
    w = models.get(model_id)
    if w is None:
        return None
    override = os.environ.get("X4_MODEL_PATH") or os.environ.get(f"X4_MODEL_PATH_{model_id.upper()}")
    return (override if override else w).strip()


# ----------------------------------------------------------------------
# 安全绑定
# ----------------------------------------------------------------------
def ensure_safe_bind(host: str, *, token_configured: bool) -> None:
    """未配置 token 时，仅允许回环绑定；远程绑定直接拒绝启动（不监听外部网络）。"""
    if token_configured:
        return
    loopback = {"127.0.0.1", "localhost", "::1"}
    if str(host).strip() not in loopback:
        raise RuntimeError("未配置鉴权 token，禁止远程绑定；请配置 X4_API_TOKEN 或仅绑定回环地址")


# ----------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------
def authorize(authorization: Optional[str], token: Optional[str]) -> None:
    if token is None:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")
    expected = f"Bearer {token}".encode("utf-8")
    if not hmac.compare_digest(authorization.encode("utf-8"), expected):
        raise HTTPException(status_code=403, detail="invalid bearer token")


# ----------------------------------------------------------------------
# 应答信封
# ----------------------------------------------------------------------
def _ok(data: Dict[str, Any]) -> JSONResponse:
    """成功响应直接返回业务 payload（`{model_id, detections, latency_ms}`），**不加统一信封**。

    契约见 `src/x4/http_schema.parse_predict_response`：成功时顶层即业务字段；统一信封仅用于错误。
    """
    return JSONResponse(data)


def _err(status: int, code: str, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": msg, "data": None, "meta": {}})


# ----------------------------------------------------------------------
# 应用工厂
# ----------------------------------------------------------------------
def build_app(
    *,
    config: Optional[Dict[str, Any]] = None,
    runner_factory: Optional[Callable[[str], Any]] = None,
    token: Optional[str] = None,
    max_file_bytes: Optional[int] = None,
    host: str = "127.0.0.1",
) -> FastAPI:
    """构造独立 FastAPI app（不加载真实模型；普通测试经 runner_factory 注入 Fake，不导入 .pt）。"""
    cfg = config if config is not None else load_server_config()
    cfg_token = token if token is not None else (os.environ.get(cfg["token_env"], "") or None)
    cfg_max = int(max_file_bytes) if max_file_bytes is not None else int(cfg["max_file_bytes"])
    if _is_invalid_size(cfg_max):
        raise X4ServerConfigError(f"max_file_bytes 非法: {cfg_max!r}")
    ensure_safe_bind(host, token_configured=bool(cfg_token))   # 未配 token + 远程绑定 → 拒绝启动

    runner_cache: Dict[str, Any] = {}

    def _runner_for(model_id: str) -> Any:
        if runner_factory is not None:
            return runner_factory(model_id)
        if model_id in runner_cache:
            return runner_cache[model_id]
        wpath = resolve_weights(cfg["models"], model_id)
        r = ModelRunner(wpath, conf=cfg["conf"], imgsz=cfg["imgsz"])
        runner_cache[model_id] = r
        return r

    app = FastAPI(title="X4 YOLO Predict", version="0.1.0")

    def _auth_dep(authorization: Optional[str] = Header(default=None)) -> None:
        authorize(authorization, cfg_token)

    @app.post("/predict", dependencies=[Depends(_auth_dep)])
    async def predict(file: Any = File(None), model_id: str = Form(...)):  # noqa: B008
        # 0) 缺少 file 字段 → 显式 400 + 统一错误信封（不再由框架 422）
        if file is None:
            return _err(400, "INVALID_IMAGE", "missing file")
        # 1) model_id 必须命中白名单（未知→404）
        if model_id not in cfg["models"]:
            return _err(404, "INVALID_MODEL", "unknown model_id")
        # 2) 有界读取文件（不依赖不可信的 Content-Length）
        data = b""
        try:
            while True:
                chunk = file.file.read(_CHUNK)
                if not chunk:
                    break
                data += chunk
                if len(data) > cfg_max:
                    file.file.close()
                    return _err(413, "PAYLOAD_TOO_LARGE", "file too large")
        except Exception:  # noqa: BLE001
            try:
                file.file.close()
            except Exception:  # noqa: BLE001
                pass
            return _err(400, "INVALID_IMAGE", "failed to read upload")
        finally:
            try:
                file.file.close()
            except Exception:  # noqa: BLE001
                pass
        if not data:
            return _err(400, "INVALID_IMAGE", "empty file")
        # 3) 解码图像
        image_bgr = cv2.imdecode(np_from_bytes(data), cv2.IMREAD_COLOR)
        if image_bgr is None:
            return _err(400, "INVALID_IMAGE", "unable to decode image")
        # 4) 推理
        try:
            runner = _runner_for(model_id)
            t0 = time.monotonic()
            dets_raw = runner.run(image_bgr)
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
        except ModelRunnerError:
            return _err(500, "INTERNAL_ERROR", "inference failed")
        except Exception:  # noqa: BLE001
            return _err(500, "INTERNAL_ERROR", "inference failed")
        # 5) 严格验证 runner 输出（非法输出 → 失败，不得转空检测）
        try:
            dets = validate_detections(list(dets_raw))
        except ValueError:
            return _err(500, "INTERNAL_ERROR", "invalid detector output")
        payload = {
            "model_id": model_id,
            "detections": [
                {
                    "class_id": int(d.class_id),
                    "class_name": d.class_name,
                    "confidence": float(d.confidence),
                    "bbox": [float(v) for v in d.bbox],
                }
                for d in dets
            ],
            "latency_ms": latency_ms,
        }
        return _ok(payload)

    return app


def _is_invalid_size(v: int) -> bool:
    return isinstance(v, bool) or not isinstance(v, int) or v <= 0


def np_from_bytes(b: bytes):
    import numpy as np
    return np.frombuffer(b, dtype=np.uint8)


def main() -> None:  # pragma: no cover - 部署入口（受控启动；不依赖模块导入期的默认 host）
    """受控 Uvicorn 启动入口。

    - 解析实际监听 host（`X4_BIND_HOST`，默认回环）；**同一个 host 值**同时用于安全校验与实际启动。
    - 无 Token 时非回环 host → `ensure_safe_bind` 在监听前抛 `RuntimeError`，Uvicorn 不启动。
    - `build_app(host=host)` 以真实 host 再校验一次，避免"导入期默认 host 通过、CLI 实际监听远程"的绕过。
    - 以 app **对象**传给 `uvicorn.run`，不依赖模块全局 `app`（消除 `uvicorn :app` CLI 直启入口）。
    """
    import uvicorn
    cfg = load_server_config()
    host = (os.environ.get("X4_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    token = os.environ.get(cfg["token_env"], "") or None
    ensure_safe_bind(host, token_configured=bool(token))   # 监听前执行，失败→不启动
    app = build_app(config=cfg, host=host)                 # 用真实 host 再校验一次
    uvicorn.run(app, host=host, port=8316, workers=1)      # 传对象；host 与导处校验一致


if __name__ == "__main__":
    main()