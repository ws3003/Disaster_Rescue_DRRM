# V4.0 团队开发规范

> 城市灾害·边缘哨兵 — 多模态智能体灾害救援系统
> 本文档规定 V4.0 的多分支协作约定, 与 `docs/development.md`、`docs/interface.md` 配套使用。

## 1. 项目分支策略

```
main (发布基线, 稳定可部署)
 └─ v4.0-dev (开发集成分支, 本团队协作主干)
      ├─ feature/v4.0-a (成员A)
      └─ feature/v4.0-b (成员B)
```

| 分支 | 用途 | 谁维护 |
|---|---|---|
| `main` | 发布基线, 仅合并经过验证的成果 | 负责人 |
| `v4.0-dev` | 团队开发集成分支, 日常协作主干 | 全员 |
| `feature/v4.0-a` | 成员A的功能开发分支 | 成员A |
| `feature/v4.0-b` | 成员B的功能开发分支 | 成员B |

**规则**:
- 开发一律在 feature 分支进行, 不直接在 `v4.0-dev` / `main` 上开发。
- `v4.0-dev` 保持随时可集成、可运行的半成品基线。
- `main` 仅从 `v4.0-dev` 合并(release), 且需打标签(如 `v4.0-*`)。

## 2. 成员A职责

**范围**: 多模态智能分析层 `src/vlm/` + `src/llm/`

| 文件 | 产出接口 |
|---|---|
| `src/vlm/client.py` `prompts.py` `parser.py` | `vlm_output` (环境理解) |
| `src/llm/client.py` `prompts.py` | `llm_output` (风险推理) |

- 产出并维护 `schemas/vlm_output.json`、`schemas/llm_output.json`。
- 通过 `data/mock/*.json` 独立验证, 不依赖成员B代码。
- 禁止触碰 `src/agent`、`src/tools`、`src/database`。

## 3. 成员B职责

**范围**: Agent 智能体系统 `src/agent/` + `src/tools/` + `src/database/`

| 文件 | 消费/产出接口 |
|---|---|
| `src/agent/triggers.py` | 消费 `person_event` (边缘端记录) |
| `src/agent/planner.py` `executor.py` | 消费 `llm_output`, 产出 `agent_task` |
| `src/tools/*_tool.py` | 执行 `agent_task.actions` |
| `src/database/*` | 落库事件 |

- 维护 `schemas/agent_task.json`。
- 通过 `data/mock/person_event.json`、`llm_output.json` 独立验证。
- 禁止触碰 `src/vlm`、`src/llm`。

**共同红线**: 禁止修改现有感知层 (detector/pose/state/risk/ranking/writer/video/camera/main)。

## 4. commit 规范

- **格式**: `type(scope): <英文简述>`
- **type**: `feat` / `fix` / `refactor` / `docs` / `chore` / `test` / `style`
- **scope**: 模块名, 如 `vlm`, `llm`, `agent`, `tools`, `database`, `schemas`, `docs`, `gitignore`
- **只提交自己的目录**: 单次 commit 不改他人目录、不夹带无关文件。

示例:
```
feat(vlm): add environment understanding client skeleton
fix(agent): correct trigger threshold for HIGH risk
docs(interface): update llm_output schema field
```

## 5. schema 接口修改规则

- 接口文件集中管理于 `schemas/*.json`, 是成员间解耦契约。
- **改接口必须走协商**: 修改 `schemas/*.json` 前, 先在 PR 描述或讨论中知会对方。
- **向后兼容优先**: 优先以"增字段 + 默认兜底"方式扩展, 避免破坏已实现方。
- **同步文档**: 接口变更必须同步 `docs/interface.md`, 保持两处一致。
- 消费方约定: 对缺失字段做**兜底默认**, 不因缺字段崩溃。

## 6. Pull Request 流程

1. 从 `v4.0-dev` 切 feature 分支 → 完成后 push 到 origin。
2. 发起 PR → `feature/v4.0-*` 合入 `v4.0-dev`。
3. **对方 Review**: 核对是否误改他人目录、是否动了感知层、是否符合 schema。
4. 通过后由 PR 作者合入并删除远程 feature 分支。
5. 大版本 / 发布时: `v4.0-dev` → `main` 走一次 release PR + 打标签。

## 7. Git 冲突处理原则

- 成员目录隔离 (`vlm+llm` vs `agent+tools+database`), 冲突概率低。
- **共享冲突面**: `schemas/*.json`、`docs/*.md` —— 属正常交集。
- 处理顺序: 先 `git pull --rebase` 拉最新 `v4.0-dev` 到本地, 再 `git rebase` 己方 commits。
- 冲突解决按文件归属: 各自目录冲突由该成员负责; 共享的 schema 冲突双方共同确认语义。
- 禁止 `--force push` 覆盖他人 / 远程历史; 远程分支已启用则用 `git pull --rebase` 而非 revert。
- 无法解决或影响接口语义的冲突, 及时在 PR 或群内提出, 不私自裁定接口行为。

## 8. 环境与敏感信息管理

### 模型权重禁止提交
- `models/` 下权重(`*.pt`、`*.task`)禁止提交, 已由 `.gitignore` 忽略。
- 权重属大体积二进制, 不入 Git; 需重新下载时记录来源与版本到 `docs/` 或 README, 而非提交文件。
- 任何人不得通过 `git add -f` 强制忽略文件入版本库。

### API Key 禁止提交
- 一切第三方 API Key (DashScope/OpenAI、企业微信、高德、和风) 禁止写入代码或被 git 跟踪。
- 密钥一律放本地 `.env`(已由 gitignore 忽略), 经 `python-dotenv` 加载。
- 提供 `.env.example` 作为模板（键名 + 占位符），**不含真实值**。
- 若密钥被误提交到历史, 立即视为泄密并到平台轮换密钥, 不在分支上保留。

### .env 配置管理
- `.env` → 加入 `.gitignore`, 永不入库, 内容含真实 Secret。
- `.env.example` → 可入库, 仅含键名与本地默认/占位(如空串或 `changeme`)。
- 新成员拉取后 `cp .env.example .env` 自行填写本地密钥。

### requirements 依赖同步
- 新增第三方依赖必须同步更新根 `requirements.txt`, 保持可复现。
- 标注可选依赖(Pi 专用如 gpiozero/luma.oled 可标注或另列 `requirements-pi.txt`), 避免在非 Pi 环境强制安装。
- 依赖变更随对应功能 commit 一起提交, 不得滞后。

## 9. 日常开发同步流程

### 开发前同步
```
git checkout v4.0-dev && git pull --rebase origin v4.0-dev
git checkout -b feature/v4.0-<x>     # 仅首次; 已在分支则 pull 最新
```
- 开工前确保本地 feature 分支基于最新 `v4.0-dev`, 减少冲突。

### 提交前检查
- `git status`: 确认暂存区只含**自己这次要提交的文件**。
- 自查勾选: 不含 `.env`/密钥、不含 `*.pt`/`*.task`、不含 `outputs/`、不含他人目录文件。
- `git diff --name-only` 复核实际将提交的路径清单。

### commit 流程
```
git add <自己的模块/文件>
git commit -m "type(scope): <英文简述>"
git push -u origin feature/v4.0-<x>
```
- 小步提交: 一个逻辑改动一个 commit, 便于 Review 与回滚。

### PR Review 流程
1. 发起 PR → `feature/v4.0-<x>` 合入 `v4.0-dev`, 描述注明改动模块与接口影响。
2. Review 核对清单:
   - 是否误改他人目录 / 现有感知层走代码
   - 是否符合 `schemas/*.json` 契约、是否同步 `docs/interface.md`
   - 是否夹带敏感信息 / 模型权重 / 临时文件
3. 通过后合并并删除远程 feature 分支。
4. 合入后各自 `git pull --rebase` 更新到最新 `v4.0-dev`。