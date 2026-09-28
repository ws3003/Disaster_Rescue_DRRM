"""
server/snapshot_server.py — Snapshot 静态访问服务 (Phase 2.7)
============================================================
目的: 把 data/snapshots/ 目录暴露为 HTTP 静态访问, 供 DingTalk picURL
      及后续 VLM/LLM 拉取现场图片。

端点:
    GET /snapshots/<filename>    -> 返回图片文件 (仅 jpg/jpeg/png)

运行:
    uvicorn server.snapshot_server:app --host 0.0.0.0 --port 8000

约束:
    - 仅允许 jpg/jpeg/png 扩展名(白名单)
    - 不修改任何现有业务代码(独立 FastAPI 应用)
    - storage.yaml base_url 默认 http://localhost:8000, 与本服务端口一致
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# 图片扩展名白名单
_ALLOWED_EXT = {".jpg", ".jpeg", ".png"}
# snapshot 根目录 (相对 repo 根; 运行 uvicorn 时 cwd 应为 repo 根)
SNAP_DIR = Path(__file__).resolve().parents[1] / "data" / "snapshots"

app = FastAPI(title="Snapshot Static Server", version="0.1.0")


@app.get("/snapshots/{filename}")
def get_snapshot(filename: str) -> FileResponse:
    """返回一张 snapshot 图片; 校验扩展名白名单 + 存在性。"""
    # 安全: 仅允许白名单扩展名, 拒绝路径穿越
    if not filename or Path(filename).suffix.lower() not in _ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="仅允许 jpg/jpeg/png 图片")
    # 文件名转义(防 path traversal): 只允许 字母数字 _ - .
    if not re.fullmatch(r"[\w\-.]+", filename, flags=re.ASCII):
        raise HTTPException(status_code=400, detail="非法文件名")
    fpath = SNAP_DIR / filename
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="snapshot 不存在")
    return FileResponse(fpath, media_type="image/jpeg")


# 兼容(可选): 若服务 data/ 下 SSOT, StaticFiles 挂载方式留作注释参考
# app.mount("/snapshots", StaticFiles(directory=str(SNAP_DIR)), name="snapshots")


def _ensure_dir() -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)


_ensure_dir()