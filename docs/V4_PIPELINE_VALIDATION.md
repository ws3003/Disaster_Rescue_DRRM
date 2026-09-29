# V4.0 Pipeline 验证文档

> 阶段一（离线安全验证）记录：**API → VLM → LLM → Agent.ingest_llm_output → Planner → Executor → Tools(mock) → SQLite**
> 状态：✅ 已通过（2026-09-29，commit 见下）

## 1. 目标链路

```
Pi Edge
    ↓
Edge Client            ← agent.ingest_llm_output(llm_output) [路径B]
    ↓
FastAPI Gateway        ← server/api.py  POST /api/v1/analyze
    ↓
VLM(图像理解)           ← src/vlm qwen3-vl-plus
    ↓
LLM(任务规划)           ← src/llm qwen-plus
    ↓
Agent.ingest_llm_output()
    ↓
Planner                ← llm_output → task (trigger.source="llm")
    ↓
Executor
    ↓
Tools                  ← communication_tool(mock) / database_tool / vision_tool
    ↓
DingTalk(SQLite)       ← 阶段一仅 mock, 不触达真实 DingTalk
```

> 规则路径 A 与 LLM 路径 B 并存，互不覆盖：
> - 路径 A（边缘规则）：YOLO → MediaPipe → state.py → `agent.process_frame`
> - 路径 B（云端智能）：Image → VLM → LLM → `agent.ingest_llm_output`

## 2. 阶段一：离线安全验证（禁用真实 DashScope / DingTalk）

### 2.1 验证方法
- 不调用真实 Qwen API、不需要真实 Key。
- 通过 `app.dependency_overrides[get_clients]` 注入带 `FakeTransport` 的 VLM/LLM 客户端（`provider="qwen"`，走 server/api.py 原有编排逻辑，**不绕过 api.py、不修改核心业务逻辑**）。
- `COMMUNICATION_CHANNEL=mock`（默认）+ 不设 `DINGTALK_WEBHOOK` → 通信 100% dry-run（副本见 `data/_phase1_verify.py`，该脚本与生成数据均被 `.gitignore` 忽略）。

### 2.2 测试命令
```bash
python data/_phase1_verify.py
```
辅助回归（Agent 侧，mock 通信）：
```bash
python -m unittest tests.agent.test_agent_llm_entry   # Ran 7 tests, OK
```

### 2.3 请求流程（真实 HTTP）
```
uvicorn 127.0.0.1:8765 (server/api.py)   # 注入 overrides 后
  requests POST /api/v1/analyze
      files={"image": data/test_images/002_fall.jpg}
      data={"person_events": [person1 Fall 0.92]}
  → VLM(qwen fake) → LLM(qwen fake) → _assemble_contract → 权威 llm_output
  → Agent(config=cfg).ingest_llm_output(llm_output)
```

### 2.4 输出结果（关键证据）
| 检查项 | 结果 |
|---|---|
| HTTP status | `200` |
| Contract 生成 | `task_proposals[0].action == "send_alert"`；`contract_version == "0.1-observation-v1"`；observation / persons 均在 |
| 日志 | `[LLM->Agent] ingest_llm_output triggered`、`[LLM->Agent] result:` 均出现 |
| trigger.source | `== "llm"` |
| Executor 生成任务 | `save_task` ok → `T-20260929-101141-9a6349` |
| Tools | communication_tool `channel=LOCAL_MOCK status=SENT`（未发真实 DingTalk）；database_tool `save_event` |
| SQLite | `tasks == 1`，`memory_alert == 1` |

### 2.5 判定
**PASS** — 真实 HTTP → api.py → Contract → `Agent.ingest_llm_output` → Planner → Executor → Tools(mock) → SQLite，`source=="llm"`，链路成立。

## 3. 说明 / 限制
- 中文在终端显示乱码为 Windows 控制台 GBK 编码问题，仅显示；JSON 内数据正确。
- EdgeCam 依赖 `picamera2`（树莓派专用），本 Windows 开发机无相机，阶段一用同一 `Agent` 类 + edge_client 路径 B 相同日志复刻驱动。edge_client run_loop 全流程需树莓派环境。
- 阶段一产生的 `data/_phase1_*.py / .db / .jsonl` 均被 `.gitignore` 忽略，不进入仓库。

## 4. 本次变更
- `src/edge_client.py`：路径 B 增加 `[LLM->Agent]` 成功/异常日志（仅日志，不改逻辑）。
- `docs/V4_PIPELINE_VALIDATION.md`：本文档。

## 5. 下一阶段建议
阶段二：恢复真实通信 → 设 `DINGTALK_WEBHOOK`（真实便签 webhook）重测 DingTalk 发送 + SQLite 记录，确认后收口提交。