# Disaster_Rescue_DRRM

灾害场景智能救援排序系统（Disaster Rescue Dynamic Risk Ranking Model）

根据灾难场景中人员的实时人体状态与动态风险，输出救援优先级排序，为应急救援决策提供依据。

## 当前版本

V0.1 — 单帧端到端演示版（V0.1.1 进入工程修复阶段）

## 项目目标

基于 YOLO26n + MediaPipe + DRRM 的灾害场景人员状态识别与救援优先级排序系统，对场景中的人员完成"检测 → 姿态 → 状态识别 → 动态风险评分 → 救援排序"的端到端链路，并以可视化形式展示。

## 技术栈

- **Ultralytics YOLO**：灾害场景目标检测
- **MediaPipe Pose**：人体姿态估计（33 关键点）
- **PyTorch**：推理后端
- **OpenCV**：图像读取与可视化

## 系统流程

```
config → YOLO检测 → MediaPipe姿态 → 状态识别 → DRRM风险评分 → 救援排序 → 可视化输出
```

## 五类人体状态

- Standing（站立）
- Sitting（坐姿）
- Lying（躺卧）
- Fall（跌倒）
- Help-Waving（挥手求救）

状态识别基于 33 关键点几何规则，并输出可解释特征（躯干倾角、膝角、手腕抬高等）与置信度。

## 如何运行

环境：Windows + Python 3.10（虚拟环境 `venv/`），CPU / CUDA GPU 均可（由 config 自动探测）。

```bash
venv\Scripts\python.exe src/main.py --config config/config.yaml --input data/test.jpg
```

输出：`outputs/v0.1_result.jpg`（叠加 bbox + 33 关键点骨架 + 状态 + 风险分数 + 救援排序榜）

## 目录结构

```
config/config.yaml    配置文件（模型、类别、姿态、DRRM、可视化参数）
data/                 测试输入图像
models/               模型权重 (disaster_best.pt)
src/
  detector.py         YOLO 目标检测
  pose.py             MediaPipe 人体姿态估计
  state.py            五类人体状态识别
  risk.py             DRRM 动态风险评分
  ranking.py          救援优先级排序
  main.py             端到端流程编排 + 可视化输出
outputs/              可视化结果输出目录
tests/                测试目录
requirements.txt      依赖清单
```

## 版本规划

| 版本 | 内容 |
|---|---|
| V0.1 | 单帧端到端演示（检测/姿态/状态/风险/排序/可视化）|
| V0.1.1 | 工程问题修复（不新增算法）|
| V0.1.5 | 展示增强：多图片测试、JSON 结果输出、Demo 优化 |
| V0.2 | 动态时序版本：视频输入、TSF 时序状态机、Static Lying、动态 DRRM |
| V1.0 | 边缘部署：Raspberry Pi 5、模型优化、实时推理 |
