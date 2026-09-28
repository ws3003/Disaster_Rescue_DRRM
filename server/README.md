# Disaster_Rescue_DRRM — Inference API Gateway (Member A)

使 **Raspberry Pi Edge Layer** 通过 HTTP 上传图片并调用 Member A 的 **VLM + LLM** 推理链路。

> 本目录是**增量**：不修改 `src/vlm`、`src/llm`、`schemas`、parser、prompts。
> API Key 复用环境变量 `VLM_API_KEY` / `LLM_API_KEY`（不创建 `.env`，不回写密钥）。

## 接口

### `POST /api/v1/analyze`（multipart/form-data）

树莓派与服务器不在同一文件系统，因此用 **multipart 上传图片**（而非 `image_path` 本机路径）。

**请求（multipart/form-data）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `image` | file (jpg/jpeg/png) | 灾害现场图片（必填） |
| `person_events` | JSON 字符串 | 人员事件数组（可选，默认空） |

```bash
curl -X POST http://<host>:8000/api/v1/analyze \
  -F "image=@camera_frame.jpg" \
  -F 'person_events=[{"person_id":1,"state":"Fall","risk_score":0.92,"priority":"HIGH"}]'
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
| `400` | 图片错误：空文件 / 非图片 / 无法读取 |
| `422` | 请求参数错误：person_events 非法 JSON 或字段不符 |
| `500` | 服务端配置错误（缺 API Key） |
| `502` | VLM / LLM / parser 失败 |

## 处理流程

```
multipart 请求
  → 校验图片 → 存临时文件 server/uploads/tmp_*.ext (自动创建, 请求结束删除)
  → VLMClient(provider="qwen").analyze(tmp_path)
  → src.vlm.parser.parse() → vlm_output
  → LLMClient(provider="qwen").infer(vlm_output, person_events)
  → src.llm.parser.parse() → llm_output
  → 返回 JSON
```

## 运行

```bash
# 安装依赖 (Server 侧)
venv/Scripts/python.exe -m pip install fastapi uvicorn python-multipart

# 设置 API Key (不写 .env, 直接注入环境)
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

`tests/p1/` 使用 **FakeTransport + FastAPI dependency_overrides** 注入假客户端，
**不调用真实 Qwen API、不需要真实 Key**；同样验证 multipart 上传、图片校验、临时文件清理。