# Raspberry Pi Edge Client (Stage 7-B)

在树莓派端采集摄像头关键帧，上传到 Windows 端的 **FastAPI Gateway**，触发
**Qwen-VLM → Qwen-LLM** 分析，返回结构化结果。

## 架构

```
   Pi Camera (Picamera2)
        ↓ 采集关键帧
   Edge Client  (src/edge_client.py)
        ↓ JPEG 编码 + multipart 上传
   FastAPI Gateway  (server/api.py, POST /api/v1/analyze)
        ↓
   Qwen-VLM → VLM Parser → vlm_output
        ↓
   Qwen-LLM → LLM Parser → llm_output
        ↓
   Edge Client 打印 Scene / Severity / Tasks
```

## 说明

- 与 `src/main.py` 及本地感知算法链**解耦**：Edge Client 不 import 感知模块。
- 不打开任何 cv2 窗口，支持 headless 树莓派。
- 上传间隔默认 10s（VLM 非视频流模型，无需每帧上传）。
- 本阶段 `person_events` 上传为空数组 `[]`；本地 YOLO 状态融合见后续 Stage 7-C。

## 运行

### 树莓派（Edge Client）

```bash
python src/edge_client.py
```

可选参数（覆盖 `config/edge.yaml`）：

```bash
python src/edge_client.py --server http://192.168.43.206:8000/api/v1/analyze
python src/edge_client.py --interval 15 --quality 80
python src/edge_client.py --once      # 只上传一帧后退出 (调试)
```

### Windows（FastAPI Gateway）

```bash
export VLM_API_KEY=xxx
export LLM_API_KEY=xxx
venv/Scripts/python.exe -m uvicorn server.api:app --host 0.0.0.0 --port 8000
```

> 依赖：树莓派 `sudo apt install python3-picamera2`；opencv-python / requests / PyYAML 由 pip 安装。

## 输出示例（成功）

```
[EdgeClient]
2026-09-21 10:20:30
uploaded: 1280x720
response: 200
================================
Edge AI Response
Scene:
collapse
Severity:
critical
Tasks:
alert, notify, log
================================
```

## 故障处理（不退出，继续下一轮）

| 情况 | 输出 | 行为 |
|---|---|---|
| 摄像头采集失败 | `camera capture failed` | 继续 |
| JPEG 编码失败 | `JPEG encode failed` | 继续 |
| 服务器不可达/网络异常 | `API connection failed` | 继续 |
| API 返回错误状态 | `server error: <CODE> <error>` | 继续 |

禁用静默失败——每次失败都会打印一条结构化日志。