# P0 开发计划 — Disaster_Rescue_DRRM V4.0

> 多模态智能体灾害救援系统 · 城市灾害边缘哨兵
> 配套文档: `docs/architecture.md`(架构) · `docs/interface.md`(接口) · `docs/git_workflow.md`(协作)

## 1. P0 阶段目标

### 1.1 P0 要实现什么

在已有灾害场景感知能力之上(YOLO26n + MediaPipe + 人体状态识别), 打通「灾害现场输入 → VLM 场景理解 → LLM 语义推理 → Agent 任务规划 → Tools 工具调用 → Database 数据记录 → 救援任务输出」的**端到端智能辅助决策链路**。

### 1.2 当前实现范围(P0)

- 环境理解: VLM 分析灾害现场图片, 输出场景类型 / 严重度 / 危险因素。
- 语义推理: LLM 基于环境与人员风险, 输出风险解释 / 救援建议 / 任务提议。
- 任务执行: Agent 将 LLM 提议翻译为可执行任务, 经 Executor 调用 Tools。
- 数据落库: 事件 / 任务 / 结果写入 SQLite。
- 三层解耦: 模块间仅以 `schemas/*.json` 契约通信, 便于双人并行。

### 1.3 暂不实现范围(非 P0)

- 树莓派实时接入、视频流实时推理。
- 动态时序推理 / TSF / 多帧跟踪。
- 真实通信 / 地图 / 天气 API 完整接入(Demo 可用 Mock 或占位)。
- Web Dashboard、外部消息推送完整闭环。

### 1.4 最终验收目标

给定一张真实/模拟灾害现场图片 + 边缘端人员感知数据, 系统能输出:
1. 符合 `vlm_output` schema 的场景理解;
2. 符合 `llm_output` schema 的救援建议;
3. 符合 `agent_task` schema 的可执行任务并完成工具调用与落库;
4. 全程无敏感信息提交, Git 流程符合规范。

---

## 2. P0 系统流程

```
灾害现场输入(图片 + 边缘端感知)
        ↓
   VLM 场景理解        src/vlm/
        ↓
   LLM 语义推理        src/llm/
        ↓
   Agent 任务规划      src/agent/planner
        ↓
   Tools 工具调用       src/tools/
        ↓
   Database 数据记录    src/database/
        ↓
   救援任务输出
```

### 模块职责与接口

| 模块 | 输入数据 | 输出数据 | 依赖接口 |
|---|---|---|---|
| **感知层**(已有) | 灾害图片 / 视频流 | person_event(人员状态·风险·bbox) | 现有 detector/pose/state/risk/ranking |
| **VLM** (`src/vlm`) | 灾害图片 + 环境提示 | `vlm_output` | schemas/vlm_output.json |
| **LLM** (`src/llm`) | `vlm_output` + person_event | `llm_output` | schemas/vlm/llm_output.json |
| **Agent Planner** | `llm_output` + 记忆(降级走 rule-based) | `agent_task` | schemas/agent_task.json |
| **Agent Executor** | `agent_task` | 工具执行结果 | tools 注册表 |
| **Tools** | `agent_task.actions` | 动作结果 / 报警 / 落库 | data/mock(测试) |
| **Database** | 事件·任务·结果 | SQLite 持久化 | src/database/db.py |

**关键约束**: 各模块只依赖 schema 契约与配置, 不跨层 import 对方实现。

---

## 3. 团队成员分工

### 成员A: VLM + LLM 链路

**负责目录**: `src/vlm/`、`src/llm/`

**任务**:
1. VLM 客户端接口(client 抽象, 供应商可切换)
2. Prompt 设计(灾种枚举 + 证据注入)
3. 图片理解结果解析(SceneAnalysis 校验与兜底)
4. LLM 推理接口(决策解释 / 建议生成)
5. 输出结构化结果(符合 schema)

**输出契约**: `schemas/vlm_output.json`、`schemas/llm_output.json`
**测试数据**: `data/mock/vlm_output.json`、`data/mock/llm_output.json`

**验收标准**:
- 可以输入模拟灾害场景(经 mock 或占位 VLM);
- 可以生成符合 schema 的 `vlm_output` / `llm_output`。

### 成员B: Agent + Tools + Database 链路

**负责目录**: `src/agent/`、`src/tools/`、`src/database/`

**任务**:
1. Agent Planner(规则触发 + LLM 辅助 + 工具调用)
2. Agent Executor(工具注册表 + 分发 + audit)
3. Memory 管理(remember/digest/cooldown)
4. Tool 调用框架(base 基类 + 各工具)
5. 数据库存储(建表 + 读写)

**输出契约**: `schemas/agent_task.json`
**测试数据**: `data/mock/person_event.json`

**验收标准**:
- 可以接收 LLM 输出(`llm_output`);
- 可以生成救援任务(`agent_task`)并执行;
- 可以保存任务记录到 SQLite。

**共同红线**: 禁止触碰现有感知层 9 个模块。

---

## 4. 接口契约

- `schemas/` 是 A、B 双方**唯一通信协议**, 共 3 个:
  - `vlm_output.json` — 环境理解
  - `llm_output.json` — 风险推理
  - `agent_task.json` — 执行任务
- 规定:
  - **不允许随意修改字段**;
  - **修改需双方确认**(在 PR / 讨论中知会对方);
  - **增字段优先**: 以新增可选字段 + 默认兜底方式扩展;
  - **保持向后兼容**: 消费方对缺失字段兜底, 不崩溃。
- 契约变更必须同步 `docs/interface.md`。

---

## 5. P0 开发阶段划分

### 阶段1: Mock 数据链路打通
- **不调用真实 API**, 使用 `data/mock/`.
- **目标**: 验证 `VLM 输出 → LLM 输出 → Agent 任务` 全链路类型与字段贯通。
- 交付: 三段数据能按 schema 串行流通, 不依赖任何外部服务。

### 阶段2: 模块功能完善
- **成员A**: 接入真实 VLM / LLM 能力(测试 Key / 占位切换)。
- **成员B**: 完善 Agent 与 Tools 的实现(工具注册、执行、落库)。
- 各自用 real/mock 混合验证本侧链路。

### 阶段3: 系统联调
- 端到端运行: 输入灾害事件 → 输出救援任务并落库。
- 验证完整流程跑通, 输出符合三个 schema 契约。

---

## 6. Git 开发流程

遵循 `docs/git_workflow.md`:

- **成员A**: `feature/v4.0-a`
- **成员B**: `feature/v4.0-b`

```
feature/v4.0-<x>  --Push-->  PR  --(Review)-->  v4.0-dev
```

- Commit 规范: `type(scope): 英文简述`(如 `feat(vlm): ...`).
- 每次 push 前自查: 不含密钥、权重、outputs、他人目录文件.
- schema 变更先协商, 后提交.

---

## 7. P0 验收标准

### 代码层
- 模块可独立运行(Python 无语法错误, 入口可调);
- 输出接口符合 `schemas/*.json`;
- 无敏感信息 / 密钥 / 模型权重提交.

### 功能层
- 输入灾害事件(mock 或真实);
- 输出可执行救援任务并落库;
- 端到端 Demo 链路闭环.

### 工程层
- Git 流程正常(main/v4.0-dev/feature 分支管理到位);
- 双人代码经 PR 正确合并无破坏(目录隔离 + schema 契约);
- `v4.0-dev` 保持随时可集成、可运行.

---

## 8. 后续扩展方向(非 P0)

P0 之后, 按需逐项增强:

- 树莓派实时接入(实时摄像头 + 边缘推理)
- YOLO26n 人体检测融合(更稳定的人体定位)
- MediaPipe 状态融合(姿态 + 状态置信度整合)
- 动态风险推理(时序 / 多帧 / TSF)
- 通信 API(企业微信等报警触达)
- 地图 API(逆地理 / 位置)
- 天气 API(气象风险)
- Web Dashboard(数据可视化与指挥面板)

> 以上均为 P0 之后的方向, P0 阶段不作实现, 仅在架构上预留接口(经 Tools 扩展 / schema 扩充).

---

## 9. P0 具体任务矩阵

> 将成员 A/B 的工作拆成可直接执行的最小任务。每个任务的验收都可在**不接真实 API、只用 `data/mock/` 与 schema 校验**的前提下完成。

### 成员A — 任务矩阵

| 编号 | 负责人 | 文件/目录 | 输入 | 输出 | 依赖 schema | 是否依赖对方 | 测试方法 | 验收标准 |
|---|---|---|---|---|---|---|---|---|
| A1 VLM Client | A | `src/vlm/client.py` | mock 灾害图片路径 | VLM 请求封装(厂商可切换) | 无(可后接 A4) | 否 | 用 mock 图片构造 client 调用, 断言请求结构 | client 可构造、请求体含图片与既定参数 |
| A2 VLM Prompt | A | `src/vlm/prompts.py` | 灾种枚举 + 证据注入模板 | prompt 模板函数 | 无 | 否 | 单元断言模板输出含 `scene_type` 提示 | 模板可生成结构化指令 |
| A3 VLM Parser | A | `src/vlm/parser.py` | VLM 原始文本/JSON | `SceneAnalysis` 校验+兜底 | `vlm_output` | 否 | 喂合法/非法 mock 文本, 断言兜底 | parser 输出符合 schema, 缺字段有兜底 |
| A4 VLM Schema 校验 | A | `tests/`(A 侧) | `schemas/vlm_output.json` + mock | 校验结果 | `vlm_output.json` | 否 | 用 mock `vlm_output` 校验通过/破坏断言报错 | mock 能通过该校验器 |
| A5 LLM Client | A | `src/llm/client.py` | `vlm_output` + 人员风险 | LLM 请求封装 | 可后接 A8 | 否 | mock 数据构造调用, 断言请求结构 | client 可构造、请求体符合预期 |
| A6 LLM Prompt | A | `src/llm/prompts.py` | 参谋 system prompt 模板 | prompt 模板 | 无 | 否 | 单元断言模板含决策解释/建议引导 | 模板可生成参谋指令 |
| A7 LLM Parser | A | `src/llm/`(parser) | LLM 原始输出 | `LLMOutput` 校验+兜底 | `llm_output.json` | 否 | 合法/非法 mock 文本断言 | parser 输出符合 schema, 兜底正确 |
| A8 LLM Schema 校验 | A | `tests/`(A 侧) | `schemas/llm_output.json` + mock | 校验结果 | `llm_output.json` | 否 | 用 mock `llm_output` 校验 | mock 通过校验器 |

### 成员B — 任务矩阵

| 编号 | 负责人 | 文件/目录 | 输入 | 输出 | 依赖 schema | 是否依赖对方 | 测试方法 | 验收标准 |
|---|---|---|---|---|---|---|---|---|
| B1 Agent Planner | B | `src/agent/planner.py` | `llm_output`(或 rule-based 触发) | `agent_task` | `agent_task.json` | **是**(依赖 A 的 `llm_output`) | 喂 mock `llm_output`, 断言生成 task | planner 生成符合 schema 的 `agent_task`, 空提议走 rule-based |
| B2 Agent Memory | B | `src/agent/memory.py` | agent 事件/mock | 记忆读写结果 | 无 | 否 | 单元调用 remember/digest/cooldown | 记忆可写可读, 有冷却逻辑 |
| B3 Agent Executor | B | `src/agent/executor.py` | `agent_task` + 工具注册表 | 任务执行 + audit 日志 | `agent_task.json` | 否 | mock `agent_task` 喂 executor, mock 工具执行 | executor 能分发动作并回填结果/status |
| B4 Tool Base 接口 | B | `src/tools/base.py` | 工具注册表定义 | Tool 基类 + Registry | 无 | 否 | 注册/查找工具单测 | 工具可注册、dispatch 可解析 |
| B5 Database Tool | B | `src/tools/database_tool.py` + `database/db.py` | 事件/任务记录 | SQLite 落库 | `agent_task.json` | 否 | 写一条记录后读回 | 数据可持久化到 `data/drrm.db` 并可查回 |
| B6 Vision Tool | B | `src/tools/vision_tool.py` | 图片 + 感知证据 | 视觉快照结果 | `person_event.json` | 否(复用感知层) | mock 图片/证据构造调用 | 工具能返回结构化视觉快照 |
| B7 Communication 接口/Mock | B | `src/tools/communication_tool.py` | 报警对象/消息 | Mock 发送结果 + 待发队列 | `agent_task.json` | 否 | 无真实通道下断言 mock 行为 | 处于待发队列, 不真实外发, 可读回 |
| B8 数据库 Schema 与落库 | B | `src/database/schema.sql` | DDL | 建表 + ORM 写入 | `agent_task.json` | 否 | 初始化库后落一条任务记录 | 表存在、插入符合 Schema、可查询 |
| B9 Agent Task Schema 校验 | B | `tests/`(B 侧) | `schemas/agent_task.json` + mock | 校验结果 | `agent_task.json` | 否 | 用 mock `llm_output`→planner→task 校验 | planner 产出的 task 能通过其校验器 |

---

## 10. P0 任务依赖关系

### 可并行(完全独立)
- **成员A 内部**: A1–A4 之间、A5–A8 之间可并行;A 段(VLM, A1–A4)与 A 段(LLM, A5–A8)也可并行。
- **成员B 内部**: B2/B4/B5/B6 互不阻塞;B8 可与 B2/B5 并行。
- **A/B 之间完全独立开发**: 全部 A 任务 + 除 B1 外的 B 任务都可与对方并行。

### 依赖对方的任务
- **B1(Planner)依赖 A 的 `llm_output`**: 但仅依赖其 **schema**, 可用 mock `llm_output` 先行开发, 不阻塞。

### 只依赖 `schemas/` + `data/mock/` 即可开工(不依赖对方实现代码)
- B1(用 mock `llm_output`)、B7(用 mock event/task)、A3/A4/A7/A8(只用 mock + schema)。
- **关键点**: 所有任务均以 `data/mock/*.json` + `schemas/*.json` 为外部输入, 因此**彼此之间无需对方源码**即可开发验收。

### 必须在阶段3联调
- 端到端: 感知证据 → VLM → LLM → Agent → Tool → Database 串接, 校验真实产物(而非 mock 直喂)跨模块流通时字段一致。
- A4→A8→B1→B3→B5/B7 的真实链路贯通。

---

## 11. P0 Mock 数据使用规范

> 优先用 Mock 数据完成模块测试, **不因真实 API 未接入而阻塞 Agent / Tools / Database 开发**。

| Mock 文件 | 实际用途 | 测试的对象 |
|---|---|---|
| `data/mock/person_event.json` | 模拟边缘端对单人员输出(person_id/state/risk/priority/bbox) | B: triggers、vision_tool; 作 Agent 触发/视觉快照输入 |
| `data/mock/vlm_output.json` | 模拟 VLM 对建筑坍塌场景的理解输出 | A: A3/A4 parser 与校验; 供 A7/ LLM 作证据 |
| `data/mock/llm_output.json` | 模拟 LLM 推理生成的救援建议与任务提议 | A: A7/A8;B: B1 planner → agent_task |

**规范要点**:
- 每个模块验收都以对应 mock 作为"真实消费方的替身"(stub)。
- Mock 必须严格遵循 `schemas/*.json` 字段定义(当前 3 个 mock 已对齐)。
- 若 mock 因 schema 变更不符, 应先更新 mock 以匹配 schema, 再调模块。
- 真实 API 接入前, Mock 是唯一的联调数据源, 因此其结构即契约, **不得随意改动字段**(改动即视为接口变更, 走协商)。

---

## 12. P0 外部 API 边界

| 类别 | 范围 | 说明 |
|---|---|---|
| **P0 必须实现** | VLM/LLM 客户端接口、Agent、Tools 框架、Database、JSON Schema 校验、Mock 端到端链路 | 不含真实调用, 以接口封装 + mock 打通 |
| **P0 只保留接口/Mock** | `communication_tool` | 只实现接口与 Mock 通道(待发队列), 不真实外发 |
| **P0 明确不接入** | 真实天气 API、真实地图 API、真实通信服务、Web Dashboard、树莓派实时链路 | 见第 8 节扩展方向, 非 P0 |

> 严守此边界, 不自行增加新的 P0 功能。外部能力一律以"接口预留 + Mock 替身"处理, 不阻塞主链。

---

## 13. P0 最终端到端验收

> 一条完整测试操作, 逐步应产生的结果, 以及最终 "P0完成/未完成" 判定。

### 步骤与预期产物

| 步 | 操作 | 所用数据 | 预期结果 |
|---|---|---|---|
| 1 | 读取灾害现场输入 | mock 图片 / `person_event.json` | 得到现场原始证据(人员+风险) |
| 2 | VLM 场景理解 | `data/mock/vlm_output.json`(或 mock 图片经 A1/A3) | 产出**符合 `vlm_output`** 的场景对象 |
| 3 | LLM 语义推理 | 上步 `vlm_output` + person_event(经 A5/A7) | 产出**符合 `llm_output`** 的建议与任务提议 |
| 4 | Agent Planner | 上步 `llm_output` → B1 | 产出**符合 `agent_task`** 的任务(pending) |
| 5 | Agent Executor | `agent_task` → B3 | 任务按 actions 分发, 状态推进 |
| 6 | Tool 调用 | B6/B7/B5 按 actions 执行 | jobs: 视觉快照 / mock 报警入队 / 数据待写 |
| 7 | Database | B5/B8 | 事件+任务落库 `data/drrm.db` |
| 8 | 输出 | 汇总 | 最终 `agent_task.status=done` + 落库记录可回读 |

### 判定标准
```
P0完成  ⇔  步骤2–8 全部通过 AND
           ① 每个中间对象经对应 schema 校验器通过
           ② 模块间用 mock 即可贯通(真实 API 未接也不阻塞)
           ③ 除 communication_tool 外无真实外部调用
           ④ Git 流程符合规范、无敏感提交
否则   ⇒  P0未完成(报告未通过步骤与原因)
```