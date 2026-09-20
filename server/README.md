# Disaster_Rescue_DRRM — Inference API Gateway (Member A)

使 **Raspberry Pi Edge Layer** 通过 HTTP 调用 Member A 的 **VLM + LLM** 推理链路。

> 本目录是**增量**：不修改 `src/vlm`、`src/llm`、`schemas`、parser、prompts 的任何内容。
> API Key 复用环境变量 `VLM_API_KEY` / `LLM_API_KEY`（不创建 `.env`，不回写密钥）。

## 接口

### `POST /api/v1/analyze`

**请求体（JSON）**

```json
{
  "image_path": "/path/to/disaster.jpg",
  "person_events": [
    {
      "person_id": 1,
      "state": "Fall",
      "risk_score": 0.92,
      "priority": "HIGH"
    }
  ]
}
```

**成功响应**

```json
{
  "success": true,
  "vlm_output": { "scene_type": "...", "overall_severity": "...", ... },
  "llm_output": { "event_summary": "...", "task_proposals": [...], ... },
  "error": null
}
```

**失败响应**（统一结构）

```json
{ "success": false, "vlm_output": {}, "llm_output": {}, "error": "原因" }
```

### HTTP 状态码约定

| 码 | 含义 |
|---|---|
| `200` | 成功 |
| `404` | 图片不存在 / 不可读 |
| `422` | 请求体不符合 schema（FastAPI 自动） |
| `500` | 服务端配置错误（缺 API Key） |
| `502` | VLM / LLM / parser 失败 |

## 内部调用链

```
POST /api/v1/analyze
  → VLMClient(provider="qwen").analyze(image_path)
  → src.vlm.parser.parse() → vlm_output
  → LLMClient(provider="qwen").infer(vlm_output, person_events)
  → src.llm.parser.parse() → llm_output
  → 返回 JSON
```

## 运行

```bash
# 安装依赖（Server 侧）
venv/Scripts/python.exe -m pip install fastapi uvicorn

# 设置 API Key（不写 .env，直接注入环境）
export VLM_API_KEY=xxx
export LLM_API_KEY=xxx

# 启动
venv/Scripts/python.exe -m uvicorn server.api:app --host 0.0.0.0 --port 8000
```

交互式文档（Swagger UI）： `http://<host>:8000/docs`

## 测试

```bash
venv/Scripts/python.exe -m unittest discover -s tests -t tests
```

`tests/p1/test_api_gateway.py` 使用 **FakeTransport + FastAPI dependency_overrides** 注入假客户端，
**不调用真实 Qwen API、不需要真实 Key**。

> 说明：`fastapi` / `uvicorn` / `httpx` 为项目新增 Server 侧依赖，未写入
> `requirements.txt`（保持与历史阶段一致）；如需复现，按上文手动安装即可。