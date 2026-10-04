# Disaster_Rescue_DRRM V4.3

灾害场景智能救援排序系统（Disaster Rescue Dynamic Risk Ranking Model, DRRM）

边缘感知 + 多模态 AI 理解 + Agent 决策 + 人机交互的**灾害救援辅助系统**。

> 定位：为应急救援提供信息与决策辅助，**不是**完全自动救援系统。

## 1. 项目简介

基于树莓派边缘摄像头完成"检测 → 姿态 → 人体状态 → DRRM 风险 → 救援排序"，结合云端多模态 AI（Qwen-VL 场景理解 + Qwen LLM 决策建议）与 Agent 决策，综合"人员风险 + 环境风险"生成两类救援报警并通过钉钉通知，并提供按键/语音现场问答。系统的目标是辅助指挥决策，最终处置由人工决定。

## 2. 系统架构

### Raspberry Pi Edge
```
Camera
   ↓
YOLO26n Detection
   ↓
MediaPipe Pose (33 关键点)
   ↓
State Recognition (Standing/Sitting/Lying/Fall/Help-Waving)
   ↓
DRRM Risk + Ranking
   ↓
person_events[{person_id,state,risk_score,priority,bbox}]
```

### Server / PC Server
```
FastAPI (/api/v1/analyze)
   ↓
Qwen-VL (场景理解 → vlm_output)
   ↓
Qwen LLM (决策建议 → llm_output)
   ↓
Agent (Planner→Executor→Tools)
   ↓
DingTalk / SQLite / OSS
```

### Voice（语音）
```
(simulated button / GPIO interface 预留)
   ↓
Recorder
   ↓
ASR
   ↓
VoiceAssistant
   ↓
TTS
   ↓
Speaker
```

> 注：实体 GPIO 按钮目前**没有接线**；当前 Demo 使用**模拟按钮**触发完整语音链路。

## 3. Edge-Cloud / Server 协同设计

**Raspberry Pi（边缘）负责**：图像采集、YOLO 检测、MediaPipe 姿态、五状态识别、DRRM 风险计算、救援排序、语音输入输出接口（录音/播放）。

**Server / PC Server 负责**：VLM 场景理解、LLM 决策建议、Agent 规划与执行、SQLite 持久化、DingTalk 报警、OSS 图片。

两者经 HTTP `POST /api/v1/analyze` 交换：Pi 上传 JPEG + person_events；Server 返回 llm_output + snapshot_url + environment_severity。
（"Server"指本项目内的 PC 服务器，非已上线云平台。）

## 4. 核心功能

### Vision Perception
- **YOLO26n**：对人员/救援员检测（`models/disaster_best.pt`），输出 bbox 与类别。
- **MediaPipe Pose**：人体 33 关键点。
- **State Recognition**：五状态识别 + 确定性仲裁。

### DRRM Risk Assessment
- `risk_score`（0~1），`priority`（HIGH / MEDIUM / LOW），及救援 `ranking`。
- 人员状态→优先级：Fall/Help-Waving→HIGH、Lying→MEDIUM、Sitting/Standing→LOW。

### VLM（Qwen-VL）
- 状态：**已完成真实 API 接入**（Provider=`qwen`，DashScope compatible-mode，`VLM_API_KEY`）。
- 输入图片 → 输出 `vlm_output`（scene_type / overall_severity / hazards）。
- `mock` 仅用于测试/离线验证。

### LLM（Qwen）
- 状态：**已完成真实 API 接入**（Provider=`qwen`，`LLM_API_KEY`）。
- 输入 vlm_output + person_events → 输出 `llm_output`（event_summary / rescue_suggestions / task_proposals），并提供 `ask` 对话。
- **LLM 输出"建议"，不直接控制报警**；最终动作由 Agent 规则约束落地。
- `mock` 仅用于测试/离线验证。

### Agent
- Planner / Executor / Memory / Alarm Fusion（`_apply_alarm_fusion`）。
- 说明：Agent 综合"人员风险 + 环境风险"（person/environment 两类、带 `alert_type`），通过**规则约束**执行最终动作（报警/记录/二次确认/冷却），不直接执行危险决策。

### Communication
- DingTalk webhook 报警（卡片标题随 alert_type：person→🚨灾害报警 / environment→🚨环境高风险报警）。
- OSS 图片（含私网 /snapshots/* 图下载后上传）；person/environment `alert_type` 字段隔离。

### Voice Interaction
- 软件链路已完成：button interface（模拟）→ recorder → ASR → VoiceAssistant → TTS → Speaker。
- 注：当前 Demo 用**模拟按钮**触发；**真实 GPIO 按钮后续接入**（接口已预留）。

## 5. 数据流程
```
Camera → YOLO → State → Risk → person_events
   ↓ (POST /api/v1/analyze)
VLM(Qwen-VL) → vlm_output
   ↓
LLM(Qwen) → llm_output
   ↓
Agent (推理) → Alarm Fusion(person/environment)
   ↓
DingTalk 报警 + SQLite 落库
```
语音：按钮 → 录音 → ASR → VoiceAssistant → 回复文本 → TTS → Speaker。

## 6. 部署方式
- **先启动 Server / PC Server**：
  ```
  python -m uvicorn server.api:app --host 0.0.0.0 --port 8000
  ```
- **后启动 Raspberry Pi Edge**：
  ```
  python src/edge_client.py
  ```
- 演示重置脚本：`scripts/demo_reset.sh`。

## 7. 环境变量
按 `config/.env.example` 配置（项目**只读取进程环境变量**，不自动加载 .env；密钥不提交 git）：
- `VLM_API_KEY`（Qwen-VL）、`LLM_API_KEY`（Qwen）
- `VLM_PROVIDER` / `LLM_PROVIDER`（qwen）及 `VLM_BASE_URL` / `LLM_BASE_URL` / 模型名
- `DINGTALK_WEBHOOK`、`COMMUNICATION_CHANNEL`
- `STORAGE_BASE_URL`（snapshot 访问前缀）
- ASR/TTS 相关：`VOICE_PROVIDER` / `VOICE_ASR_PROVIDER` / `VOICE_TTS_PROVIDER`、`ASR_API_KEY`/`ASR_BASE_URL`、`TTS_API_KEY` 等

## 8. 当前实现状态

### ✅ 已实现
- YOLO26n 检测
- MediaPipe Pose（33 关键点）
- 五状态识别（Standing/Sitting/Lying/Fall/Help-Waving）
- DRRM 风险评估 + Ranking
- Edge 感知管线（edge_perception / edge_client）
- FastAPI Server（/api/v1/analyze 等）
- Qwen-VL 真实 API 接入
- Qwen LLM 真实 API 接入
- Agent / Planner / Executor / Memory / Alarm Fusion（person/environment）
- DingTalk 报警 + OSS 图片
- SQLite（events/tasks/persons/agent_logs）
- 语音软件链路（button 接口/recorder/ASR/Assistant/TTS/Speaker）

### ⚠️ 部分实现
- GPIO 实体按钮接入（当前 Demo 用**模拟按钮**；接口已预留，后续可直接接物理按键）
- ASR/TTS `live` 依赖（需 API key/联网）
- map/weather 在线服务（需 key/联网）

### 🔮 Future Work
- GPS / 高德定位
- Static Lying（时序）
- 时序状态机
- 多机协同
- SceneContext 增强

## 9. 限制说明
- 大模型输出需经过 Agent 规则融合（Alarm Fusion / 二次确认 / 冷却），**不直接自动执行危险决策**。
- 当前定位**不是 GPS 系统**（坐标/地址为 mock 或占位）。
- 当前按钮**不是实体硬件接入**（Demo 用模拟按钮）。
- 真机 ASR/TTS、map/weather 等在线能力依赖对应 API key 与网络。