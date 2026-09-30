"""src/vision_client.py — 边缘人员快照同步客户端 (V4.1 Phase7.3)。

职责: 仅把 edge 的 person_events 以 JSON POST 到 server 的 vision snapshot 接口。
  person_events
        ↓  POST {persons:[...]}
        ↓  server /api/v1/vision/snapshot
        ↓  vision_snapshot 存储 → Assistant 读取

不包含: 状态识别 / 风险计算 / Agent 逻辑 / 救援动作。
失败绝不抛异常(不影响 edge 主流程: camera/YOLO/MediaPipe/Agent/上传/云端分析)。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# 默认 transport(惰性: 在 upload 内 import requests, 避免模块/导入期网络依赖)
# self._post 若注入(测试用 FakeTransport), 则不使用 requests。


class VisionClient:
    """把 person_events 同步到 server vision snapshot 端点。"""

    def __init__(
        self,
        snapshot_url: Optional[str] = None,
        transport: Optional[Callable[..., Any]] = None,
        timeout: float = 3.0,
    ):
        self.snapshot_url = snapshot_url
        self._post = transport
        self.timeout = timeout

    def upload(self, person_events: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """POST person_events -> snapshot_url。成功/失败均不抛异常。"""
        url = (self.snapshot_url or "").strip()
        # 无 URL → 关闭同步: 不发 HTTP, 返回 ok:False
        if not url:
            return {"ok": False, "error": "snapshot_url not configured"}
        persons = person_events if isinstance(person_events, list) else []
        try:
            # 惰性加载 requests: 仅 real call 时才依赖网络库
            import requests  # noqa: PLC0415

            post_fn = self._post if self._post is not None else requests.post
            resp = post_fn(
                url,
                json={"persons": persons},
                timeout=self.timeout,
            )
            status = int(getattr(resp, "status_code", 0))
            if 200 <= status < 300:
                return {"ok": True, "status_code": status}
            return {"ok": False, "status_code": status,
                    "error": f"server returned {status}"}
        except Exception as exc:  # noqa: BLE001 — 网络失败等绝不外抛
            return {"ok": False, "error": "connection failed"}


if __name__ == "__main__":
    c = VisionClient(snapshot_url="")
    print("empty url:", c.upload([{"person_id": 1, "state": "Fall"}]))
    c2 = VisionClient(snapshot_url="http://127.0.0.1:9/vision")
    print("no server :", c2.upload([{"person_id": 1, "state": "Fall"}]))