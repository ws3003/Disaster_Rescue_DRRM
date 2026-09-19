# V4.0 系统架构说明

> 城市灾害·边缘哨兵 — 多模态智能体灾害救援系统
> 本文档说明分层架构、模块职责与本系统整体数据流。

## 1. 分层架构总览

系统分为两层:**已有边缘感知层**(稳定基线)+ **新增多模态智能分析层 / Agent 智能体层 / Tools 工具层**(V4.0 增量)。

```
┌─────────────────────────────────────────────────────────────┐
│  Tools 工具层           vision / communication / database …  │
├─────────────────────────────────────────────────────────────┤
│  Agent 智能体层         triggers · memory · planner · executor│
├─────────────────────────────────────────────────────────────┤
│  多模态智能分析层       VLM(环境理解) · LLM(决策解释)          │
├═════════════════════════════════════════════════════════════┤
│  边缘感知层(已有·稳定)   YOLO26n · MediaPipe · DRRM          │
└─────────────────────────────────────────────────────────────┘
```

## 2. 已有边缘感知层(稳定基线, 禁止修改)

| 模块 | 技术 | 职责 | 文件 |
|---|---|---|---|
| 人员检测 | YOLO26n | 灾害目标检测, 区分 person / 非人员 | `src/detector.py` |
| 姿态估计 | MediaPipe | 33 关键点人体姿态 | `src/pose.py` |
| 状态识别 | 几何规则 | Standing / Sitting / Lying / Fall / Help-Waving | `src/state.py` |
| 风险评分 | DRRM | 单帧风险评分 (F1 × F6) | `src/risk.py` |
| 救援排序 | DRRM | 救援优先级排序 | `src/ranking.py` |
| 输入适配 | — | 图片 / 目录 / 视频 / 摄像头 | `src/video.py` `src/camera.py` |
| 结果落盘 | — | JSON / JSONL 特征流 | `src/writer.py` |
| 流程编排 | — | 感知 pipeline 入口 | `src/main.py` |

**设计红线**:仅通过 import / 配置消费该层能力, **不重构已有九个模块**。

## 3. 新增 V4.0 模块

| 层 | 模块 | 目录 | 职责 |
|---|---|---|---|
| 智能分析 | VLM | `src/vlm/` | OpenAI 兼容 client, 环境理解 (scene/hazards/severity) |
| 智能分析 | LLM | `src/llm/` | 决策解释与救援建议生成 |
| Agent | triggers | `src/agent/triggers.py` | 事件触发判定 (消费 writer 的 JSONL) |
| Agent | memory | `src/agent/memory.py` | 历史记忆 (remember/digest/cooldown) |
| Agent | planner | `src/agent/planner.py` | 轻量三段式: 规则触发 + LLM 辅助 + 工具调用 |
| Agent | executor | `src/agent/executor.py` | 工具注册表 + 分发 + audit |
| Tools | base | `src/tools/base.py` | Tool 抽象基类 + ToolRegistry |
| Tools | 各工具 | `src/tools/*_tool.py` | 视觉快照 / 风险排序 / 报警 / 落库 等 |
| Data | database | `src/database/` | SQLite Schema (schema.sql) + Database 类 (db.py) |

## 4. 整体数据流

```
        ┌──────────────┐
        │ 边缘感知层     │  YOLO26n→MediaPipe→state→risk→ranking
        │ (已有·稳定)    │  writer 输出 JSON / JSONL
        └──────┬───────┘
               │ person_event (person_id/state/risk_score/priority/bbox)
               ▼
        ┌──────────────┐
        │ VLM client    │─── vlm_output ───►  llm client
        │ (环境理解)     │  (scene/hazards)      (决策解释)
        └──────────────┘                        │ llm_output
                                               ▼
        ┌──────────────┐
        │ Agent planner │──── agent_task ────►  executor ──► tools
        │ (规则+LLM+工具)│  (task_id/trigger/     (注册表+分发)
        └──────┬───────┘      actions/status)          │
               │ events        └─► vision/communication│
               ▼                  database_tool        ▼
        ┌──────────────┐                          SQLite (drrm.db)
        │ Agent memory  │──────── acts on ─────────► 现场处置/报警
        └──────────────┘
```

关键接缝:**writer.py 的 JSONL 特征流**是感知层与 Agent 层之间的天然解耦接口; 各层仅以 JSON 数据通信, 不直接 import 跨层代码。

## 5. 降级策略

- **感知层不可用**(摄像头/模型缺失): 使用 `data/mock/*.json` 模拟输入先跑通决策链路。
- **LLM / VLM 不可用**: Rule-based 触发仍可独立工作, 不阻塞高危报警。
- **工具无真实通道**(微信/地图/天气): 以 Mock Channel 实现, 保展示逻辑。