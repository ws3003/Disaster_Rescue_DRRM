# V4.0 多人协作开发说明

> 面向成员 A(多模态智能分析)、成员 B(Agent 智能体系统)的职责划分与 Git 协作约定。

## 1. 成员职责划分

### 成员 A — 多模态智能分析层

| 目录/文件 | 职责 |
|---|---|
| `src/vlm/client.py` | VLM OpenAI 兼容客户端 (供应商可切换) |
| `src/vlm/prompts.py` | 灾种枚举 + 证据注入模板 |
| `src/vlm/parser.py` | SceneAnalysis 校验与兜底 |
| `src/llm/client.py` | LLM OpenAI 兼容客户端 |
| `src/llm/prompts.py` | 参谋 system prompt |

**A 产出接口**: `schemas/vlm_output.json`, `schemas/llm_output.json`
**A 独立测试**: `data/mock/vlm_output.json`, `data/mock/llm_output.json`

### 成员 B — Agent 智能体系统

| 目录/文件 | 职责 |
|---|---|
| `src/agent/triggers.py` | 事件触发判定 (消费 writer 的 JSONL) |
| `src/agent/memory.py` | 历史记忆 (remember/digest/cooldown) |
| `src/agent/planner.py` | 轻量三段式: 规则触发 + LLM 辅助 + 工具调用 |
| `src/agent/executor.py` | 工具注册表 + 分发 + audit |
| `src/tools/base.py` | Tool 抽象基类 + ToolRegistry |
| `src/tools/*_tool.py` | 视觉/报警/落库等工具 |
| `src/database/schema.sql` | MVP 四表 DDL |
| `src/database/db.py` | Database 类 |

**B 消费/产出接口**: `schemas/agent_task.json` (消费 `llm_output`, 产出 `agent_task`)
**B 独立测试**: `data/mock/person_event.json`

### 边界约定
- **A 不触碰** `src/agent`、`src/tools`、`src/database`。
- **B 不触碰** `src/vlm`、`src/llm`。
- 双方只通过 `schemas/*.json` 对齐接口, **不直接 import 对方代码**。
- 禁止修改现有感知层 9 个模块 (`detector/pose/state/risk/ranking/writer/video/camera/main`)。

---

## 2. Git 协作方式

### 分支模型: feature branch 开发 + Pull Request 合并

```
  main (稳定基线, 可发布)
   └─ feature/v4.0-a        成员 A 的 feature 分支
   └─ feature/v4.0-b        成员 B 的 feature 分支
        Each merges back to main via Pull Request
```

| 角色 | 分支 | 基准 |
|---|---|---|
| 当前工作基线 | `V0.1.5` (主分支为 `main`) | — |
| 成员 A | `feature/v4.0-a` | 从 `main` 切出 |
| 成员 B | `feature/v4.0-b` | 从 `main` 切出 |

### 协作流程
1. **各自从 `main` 切 feature 分支**, 避免直接在 main 上提交。
2. 改自己的目录, 提交粒度按模块 (例: `feat(vlm): add client abstraction`)。
3. 完成一个接口后, 更新对应 `schemas/*.json` (若接口变更须同步文档 `docs/interface.md`)。
4. 通过 **Pull Request** 将 feature 分支合并回 `main`, 合并前由另一成员 Review。
5. Review 重点: 是否符合 Schema、是否误改他人目录、是否动了感知层。

### 常用命令参考
```bash
# 各自切分支 (示例, 成员 A)
git checkout -b feature/v4.0-a

# 提交 (简洁英文, 模块前缀)
git add src/vlm
git commit -m "feat(vlm): add env understanding client skeleton"

# 推送并开 PR
git push -u origin feature/v4.0-a
# 在远程平台创建 Pull Request → main
```

### 冲突与避免原则
- 目录完全隔离 (`vlm+llm` vs `agent+tools+database`), 冲突概率极低。
- 唯一共享文件是 `schemas/*.json` 与 `docs/*.md`, 接口变更须及时同步并知会对方。
- 合并顺序: 建议先合并接口定义 (schemas), 再合并各自实现。

---

## 3. 联调方式 (不依赖对方代码)

1. A、B 各自用 `data/mock/` 数据验证自己的模块。
2. B 可用 mock 的 `llm_output.json` 直接测 `planner` → `agent_task`, 无需等 A 的真实 LLM。
3. A 可用 mock 的 `vlm_output.json` 验证 `parser` 兜底逻辑, 无需等真实模型。
4. 端到端联调留到最后, 此时接口已由 Schema 锁定。