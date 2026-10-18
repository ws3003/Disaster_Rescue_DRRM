# Disaster_Rescue_DRRM — 环境安装与运行手册

> 版本: V4.3
> 覆盖: Windows PC（开发/服务器）+ Raspberry Pi 5（边缘）两套完整环境。
> 依据源码实际 import 盘点，**清单不缺失**（含审计补出的 `oss2` / `jsonschema` / `pydantic`）。

---

## 0. 先读 —— 每端负责什么

| 运行端 | 承担模块 | 说明 |
|---|---|---|
| **Raspberry Pi 5**（边缘） | Picamera2 采集、YOLO26n 检测、MediaPipe 姿态、五状态识别、DRRM 风险 + 排序、录音/播放（可选） | 轻量感知终端，把 `image + person_events` 经 HTTP 上传到 PC |
| **Windows PC**（服务器） | FastAPI 网关、Qwen VL/LLM、Agent 决策、SQLite、钉钉报警、OSS 上传、TTS/ASR 综合 | 计算与云端出口所在，"服务器"指这台 PC，非上线云平台 |

通信方式：Pi → `POST /api/v1/analyze` → PC →（出网）阿里云百炼 / 钉钉 / OSS。

---

## 1. Windows PC 依赖

### 1.1 依赖清单（按用途分组）

| 分组 | 包 | 用途 |
|---|---|---|
| 视觉 / 感知 | `torch==2.5.1+cu124`（含 `torchvision` `torchaudio`）、`ultralytics==8.4.138`、`mediapipe==0.10.9`、`opencv-python==4.9.0.80`、`opencv-contrib-python==4.9.0.80`、`numpy==1.26.4`、`pillow==12.3.0`、`matplotlib==3.10.9` | YOLO 检测、MediaPipe 姿态、DRRM 可视化 |
| 服务器网关 | `fastapi==0.141.1`、`uvicorn==0.53.0`、`python-multipart==0.0.32`、`pydantic==2.13.5` | `/api/v1/analyze` 等 FastAPI 端点 |
| Agent / 工具 | `PyYAML==6.0.3`、`requests==2.34.2`、`psutil==7.2.2`、`jsonschema==4.26.0`、`polars==1.44.1` | 配置解析、HTTPS 调用、Agent 任务 schema 校验 |
| 云端 AI（阿里云百炼） | `dashscope==1.27.7` | Qwen 系列 VL / LLM / ASR / TTS |
| OSS 存储 | `oss2==2.19.1` ⬅️ 审计补齐 | 快照图 / 录音上传阿里云 OSS，取公网 URL 供钉钉图片报警 |
| 语音 | `sounddevice==0.5.6`、`edge-tts==7.2.8` | 录音采集；本地 TTS 合成（默认路径，走 `edge-tts` CLI） |
| 其它锁版本 | `requests`、`absl-py`、`attrs`、`certifi`、`cffi`、`charset-normalizer`、`contourpy`、`cycler`、`filelock`、`flatbuffers`、`fonttools`、`fsspec`、`idna`、`Jinja2`、`kiwisolver`、`MarkupSafe`、`mpmath`、`networkx`、`nvidia-ml-py`、`packaging`、`protobuf`、`pycparser`、`pyparsing`、`python-dateutil`、`six`、`sympy`、`typing_extensions`、`ultralytics-thop`、`urllib3` | 上述主包的传递依赖，`pip install -r requirements.txt` 会一并解析（见仓库根 `requirements.txt` 全量） |

> 注：`requirements.txt` 已按上述补齐 `oss2` / `jsonschema` / `pydantic`，并把 `edge-tts` 钉子版本。CUDA 版 torch 需 GPU（当前 RTX 4060）；无 GPU 用 CPU 装法见 §5 降级。

### 1.2 安装命令（PowerShell）

```powershell
cd "d:\Disaster_Rescue_DRRM（2）"

# 1) 创建 / 激活虚拟环境
python -m venv venv
.\venv\Scripts\Activate.ps1

# 2) 安装全部依赖（含 torch CUDA 版；需已配好 NVIDIA CUDA 运行时）
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

## 2. Raspberry Pi 5 依赖

### 2.1 系统包（apt，`picamera2` / `aplay` 不能 pip 装）

```bash
sudo apt update
sudo apt install -y python3-picamera2 alsa-utils
```

- `python3-picamera2`：CSI 摄像头驱动。
- `alsa-utils`：提供 `aplay` 用于语音播报。
- 可选 `python3-rpi.gpio`：接入实体 GPIO 通话按钮（当前 Demo 走模拟按钮，接口已预留）。

### 2.2 pip 依赖

Pi 端用 CPU 版 torch，且需 `--system-site-packages` 以感知系统装的 `picamera2`。

| 包 | 用途 |
|---|---|
| `torch` / `torchvision`（CPU 源） | YOLO 推理底座 |
| `ultralytics==8.4.138` | YOLO26n 检测 |
| `opencv-python==4.9.0.80` | 图像处理 / 预览 |
| `mediapipe==0.10.9` | 人体 33 关键点姿态 |
| `numpy>=1.24,<1.27` | 数值计算 |
| `Pillow>=10.0` | 图像 I/O |
| `requests>=2.31`、`PyYAML>=6.0` | 上传 + 配置 |
| `psutil>=5.9`、`python-dateutil>=2.8` | 运行时辅助 |
| `sounddevice`（可选） | 本地录音（语音链路用） |
| `edge-tts`（可选） | 本地 TTS 播报 |

### 2.3 安装命令（Pi 终端）

```bash
cd ~/Disaster_Rescue_DRRM

# 1) 系统包
sudo apt update
sudo apt install -y python3-picamera2 alsa-utils python3-rpi.gpio

# 2) venv（必须 --system-site-packages，才能 import 系统级 picamera2）
python3 -m venv --system-site-packages venv
source venv/bin/activate

# 3) 先装 CPU 版 torch（再装 ultralytics，它会自动评估 torch）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# 4) 其余边缘依赖 + 可选语音
pip install -r requirements_pi_v4_demo.txt
pip install sounddevice edge-tts   # 可选：启用本地语音
```

---

## 3. 环境变量与配置

### 3.1 PC 密钥注入（PowerShell；项目只读进程环境变量，不自动加载 .env。全部按 `config/.env.example` 对照）

```powershell
# —— VLM / LLM（Qwen 云端）——
$env:VLM_PROVIDER="qwen"
$env:VLM_API_KEY="<百炼VLM Key>"
$env:VLM_MODEL="qwen3-vl-plus"
$env:LLM_PROVIDER="qwen"
$env:LLM_API_KEY="<百炼LLM Key>"
$env:LLM_MODEL="qwen-plus"

# —— 报警通道 ——
$env:COMMUNICATION_CHANNEL="dingtalk"     # mock | wecom | dingtalk
$env:DINGTALK_WEBHOOK="<钉钉机器人 webhook>"

# —— OSS 存储（图/音频证据上传）——
$env:OSS_BUCKET="disaster-rescue-images"
$env:OSS_ENDPOINT="oss-cn-hangzhou.aliyuncs.com"
$env:OSS_ACCESS_KEY="<OSS AK>"
$env:OSS_ACCESS_SECRET="<OSS SK>"

# —— 可选：语音 / 天气 / 地图（key 具体见 .env.example）——
$env:VOICE_PROVIDER="mock"        # mock 零网络
# $env:ASR_API_KEY=...  $env:TTS_API_KEY=...
# $env:WEATHER_API_KEY=...  $env:MAP_API_KEY=...
```

> 密钥只放环境变量，绝不进 `config/*.yaml`（红线）。

### 3.2 Pi 指向 PC 网关（`config/edge.yaml`）

```yaml
server:
  api_url: "http://<PC局域网IP>:8000/api/v1/analyze"   # 改成你 PC 在同一网段的 IP
assistant:
  vision_snapshot_url: "http://<PC局域网IP>:8000/api/v1/vision/snapshot"
upload:
  interval: 10      # 秒，帧上传周期
```

---

## 4. 运行

> 顺序：**先 PC 服务器，再 Pi 边缘**。

### 4.1 PC 启动服务器

```powershell
# 在激活的 venv 下 + 已注入 §3.1 环境变量
python -m uvicorn server.api:app --host 0.0.0.0 --port 8000
```

### 4.2 Pi 启动边缘终端

```bash
cd ~/Disaster_Rescue_DRRM/src
python edge_client.py --preview     # 实时预览 + 感知 + 上传 + 报警
# headless（无桌面）用：python edge_client.py          （按 q 不可用，Ctrl+C 退出）
```

清理演示状态：`scripts/demo_reset.sh`。

### 4.3 单机直测（不经服务器）

```powershell
python src/main.py          # 单图/目录/视频本地闭环：检测→姿态→状态→风险→排序→可视化
python src/main.py --camera # Pi 5 Picamera2 实时流（PC 上无 picamera2 则不可用）
```

---

## 5. 离线 / 降级模式（无密钥、无网络也能跑通链路）

| Provider | 设为 | 效果 |
|---|---|---|
| `VLM_PROVIDER` / `LLM_PROVIDER` | `mock` | 内建 mock 结果，不触达百炼 |
| `COMMUNICATION_CHANNEL` | `mock` | 报警只写 jsonl，不推钉钉 |
| `STORAGE`（`config/storage.yaml`） | `local` | 快照走本地 URL，不上传 OSS |
| `VOICE_PROVIDER` / `WEATHER_PROVIDER` / `MAP_PROVIDER` | `mock` | 零网络占位 |
| 感知降级 | — | 用 `data/mock/*.json` 模拟输入，先跑通决策链路 |
| 无 GPU（CPU torch） | — | 用 CPU 版 torch 源安装（见 §2.2），推理变慢但可跑 |

---

## 6. 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `ModuleNotFoundError: No module named 'oss2'` | 旧环境缺声明；`pip install oss2==2.19.1`（已补进 requirements.txt） |
| `ModuleNotFoundError: No module named 'jsonschema'` / `pydantic` | 同上，`pip install jsonschema pydantic` |
| Pi 上 `import picamera2` 失败 | 未装系统包或 venv 未加 `--system-site-packages`；重见 §2.3 |
| Pi 播报无声 | 未装 `alsa-utils`；或检查 `aplay -l` 输出设备（`config` 里 `plughw:2,0`） |
| 钉钉图片报警不显示图 | OSS 上传失败回退本地私网 URL（localhost/192.168.x）钉钉不可达；检查 `OSS_*` 环境变量 |
| `edge-tts` CLI not found | `pip install edge-tts`（Pi 实测 Python API 连不上，代码走 CLI subprocess） |
| 服务器启动 420/500 | `VLM_API_KEY` / `LLM_API_KEY` 未注入或百炼额度问题 |

---

## 7. 依赖清单变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| V4.3 → 本文 | 补 `oss2==2.19.1`、`jsonschema==4.26.0`、`pydantic==2.13.5` 至 `requirements.txt`；`edge-tts` 钉为 `==7.2.8` | 审计发现代码已 import 但缺声明，重建环境会丢；OSS 实际在用 |