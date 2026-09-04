# V0.1.5 Step4 — DRRM 风险模型验证报告

- 日期：2026-09-04
- 验证工具：`tests/validate_risk.py`（Part1 实测 pipeline 全链路 + Part2 合成证据单元检查 + Part3 反事实归因）
- 验证对象：`src/risk.py`（F1/F6 计算、聚合）、`src/ranking.py`（排序键、tie-break、priority 分级）
- 约束：只验证、只报告；**未修改** state.py / detector.py / pose.py / risk.py / ranking.py / main.py / config.yaml；不进 V0.2、无 TSF。

## 结论摘要（先行）

1. **排序逻辑本身符合设计**：先验单调性、等证据五状态序、同分 tie-break 三项检查全部 PASS；实测正确识别的三态风险分顺序为 Help-Waving(0.371) > Sitting(0.280) > Standing(0.276/0.152)，与危险序一致。
2. **风险分在当前证据质量下几乎全部塌缩在 LOW 段**：5 张图 6 个有效 civilian 框全部 `priority=LOW`，分数区间 0.15~0.37；HIGH/MEDIUM 在实测中从未出现。
3. **Help-Waving(004) 风险偏低，confidence 低不是主因，F6 乘性调制才是**：conf 0.35 使 f1 0.78→0.599（−23%），但即使 conf=1.0，固定实测 f6=0.620 时 risk 仅 0.483——仍为 LOW；当前证据下挥手者**无论状态多确定都进不了 MEDIUM**（需 conf≥1.10，不可达）。
4. **priority 分级阈值（0.75/0.5）与 F6 调制后的实际分数分布（典型 0.2~0.5）严重脱节**：Fall 进 HIGH 需 f6≥0.79、Help-Waving 需 f6≥0.96（实际 f6 实测 0.40~0.73）；Sitting 理论上限 0.55，**永远不可能 HIGH**，MEDIUM 需 f6≥0.91。
5. 002（Fall 误判为 Standing，conf 0.35）因置信度回归机制 f1=0.448，反而**高于**自信站立者的 0.380——误判者在榜上排 #4，风险分对状态误判没有自救能力（这属于状态层问题在风险层的投影）。

## 1. Baseline：状态→风险映射表（config.yaml `drrm` + risk.py/ranking.py 源码核实）

| state | F1 先验 base | STATE_ORDER（越小越危险） | risk 理论上限 (conf=1, f6=1) |
|---|---|---|---|
| Fall | 0.95 | 0 | 0.950 |
| Lying | 0.90 | 1 | 0.900 |
| Help-Waving | 0.78 | 2 | 0.780 |
| Sitting | 0.55 | 3 | 0.550 |
| Standing | 0.35 | 4 | 0.350 |

公式（[risk.py:64-76](../src/risk.py#L64-L76)）：

- `f1 = 0.5 + (base − 0.5) × state_confidence`（低置信向中性值 0.5 回归，[risk.py:25](../src/risk.py#L25) `NEUTRAL_RISK`）
- `f6 = clip(det_conf × pose_vis, w_min=0.25, 1.0)`
- `risk_score = f1 × f6`
- priority（[ranking.py:58-63](../src/ranking.py#L58-L63)）：≥0.75 HIGH / ≥0.5 MEDIUM / 余 LOW
- 排序键（[ranking.py:66-73](../src/ranking.py#L66-L73)）：risk 降序 → STATE_ORDER → det_conf 降序 → pose_vis 降序

## 2. 实测结果（Part 1，data/test_images 全链路）

| 图片 | GT | pid | 预测 state | conf | det_conf | pose_vis | f1 | f6 | risk | priority | rank |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 001_standing | Standing | 1 | Standing | 0.80 | 0.751 | 0.967 | 0.380 | 0.726 | **0.276** | LOW | 1 |
| 001_standing | Standing | 2 | Standing | 0.79 | 0.415 | 0.960 | 0.381 | 0.399 | **0.152** | LOW | 2 |
| 002_fall | Fall | 1 | Standing ✗ | 0.35 | 0.595 | 0.951 | 0.448 | 0.566 | **0.253** | LOW | 1 |
| 003_lying | Lying | — | （YOLO 零检测，无风险结果） | | | | | | | | |
| 004_help | Help-Waving | 1 | Help-Waving ✓ | 0.35 | 0.760 | 0.815 | 0.599 | 0.620 | **0.371** | LOW | 1 |
| 005_multi_person | Sitting | 4 | Sitting ✓（4 名 rescuer 不参与） | 0.65 | 0.676 | 0.777 | 0.532 | 0.525 | **0.280** | LOW | 1 |

**跨图虚拟榜**（全部实测 civilian 合并为同一场景排序）：

| rank | 来源 | state | risk | priority |
|---|---|---|---|---|
| 1 | 004#pid1 | Help-Waving | 0.371 | LOW |
| 2 | 005#pid4 | Sitting | 0.280 | LOW |
| 3 | 001#pid1 | Standing | 0.276 | LOW |
| 4 | 002#pid1 | Standing（GT=Fall） | 0.253 | LOW |
| 5 | 001#pid2 | Standing | 0.152 | LOW |

- 正确识别状态间的相对顺序（Help > Sitting > Standing）**符合设计危险序**；
- 但全员 LOW、相邻名次分差仅 0.02~0.09，排序榜在视觉/决策上几乎不区分危险等级；
- rescuer（005 中 4 人）全部不进风险/排序，仅蓝框标注——符合稳定化设计。
- 对账：`src/main.py` 重跑输出与 `outputs/json/*.json` 的 f1/f6/risk/rank 与本表完全一致（如 004 JSON: risk=0.3713, f1=0.599, f6=0.62）。

## 3. 设计符合性检查（Part 2，合成证据）

| 检查 | 内容 | 结果 |
|---|---|---|
| 2a | 先验危险度单调 Fall>Lying>Help>Sitting>Standing（0.95/0.90/0.78/0.55/0.35） | **PASS** |
| 2b | 等证据（conf=1, f6=1）五状态排序顺序 | **PASS** |
| 2c | risk 同分时 STATE_ORDER tie-break（故意打乱输入） | **PASS** |
| 2d | conf×f6 网格（conf∈{0.35,0.65,1.0} × f6∈{0.25,0.5,0.7,1.0}，同帧同证据） | **12/12 保持设计序，0 反转** |
| 2e | priority 分级可达性 | 见下表，HIGH 实际不可达 |

**2e 分级可达性（conf=1 时各 f6 水平下的 risk）**：

| state | base | f6=0.70 | f6=0.60 | f6=0.50 | 进 MEDIUM 需 f6≥ | 进 HIGH 需 f6≥ |
|---|---|---|---|---|---|---|
| Fall | 0.95 | 0.665 MEDIUM | 0.570 MEDIUM | 0.475 LOW | 0.53 | 0.79 |
| Lying | 0.90 | 0.630 MEDIUM | 0.540 MEDIUM | 0.450 LOW | 0.56 | 0.83 |
| Help-Waving | 0.78 | 0.546 MEDIUM | 0.468 LOW | 0.390 LOW | **0.64** | **0.96** |
| Sitting | 0.55 | 0.385 LOW | 0.330 LOW | 0.275 LOW | 0.91 | 不可达（上限 0.55） |
| Standing | 0.35 | 0.245 LOW | 0.210 LOW | 0.175 LOW | 不可达 | 不可达 |

实测 f6 区间 0.40~0.73（均值约 0.57）：此区间内 Fall/Lying 可勉强进 MEDIUM，Help/Sitting/Standing 必为 LOW。

**反转边界补充**（跨人证据不对称，网格外对照）：同帧同证据下危险序 12/12 不反转（f6 是共通乘子、f1 对 base 单调）；但证据质量**跨人悬殊**时序可反转——Fall 最差证据（conf 0.35, f6 0.25）risk=0.164 < Standing 最好证据（conf 1, f6 1）risk=0.350。STATE_ORDER 次键仅在 risk 完全相等（1e-9）时触发，不阻止此类反转。同帧真实画面中证据差异通常较小，属理论边界，但设计上"危险序"并非硬保证。

## 4. 问题分析

### 4.1 当前风险分能否体现危险等级？

- **等证据条件下可以**：2a/2b/2d 证明序关系正确；实测 Help(0.371) > Sitting(0.280) > Standing(0.276) 方向正确。
- **但绝对值失去等级含义**：全员 LOW，分数挤在 0.15~0.37。原因链条：state confidence 回归把 f1 拉向 0.5（实测 f1 0.38~0.60），f6 再乘 0.40~0.73，两次压缩相乘使动态范围从设计的 [0.35, 0.95] 塌缩到实测 [0.15, 0.37]。
- **高端尺度无真实样本 exercise**：Fall（002）误判为 Standing、Lying（003）检测漏检，风险层从未收到过 Fall/Lying 输入——先验表中 0.90/0.95 两档在真实数据上未被验证过。
- **风险层对状态误判无自救**：002 被误判 Standing 且 conf=0.35，f1 按回归机制得 0.448，**高于**自信站立者的 0.380（低置信向 0.5 回归对 Standing 这种 base<0.5 的状态是向上抬）。误判者在跨图榜排 #4，高于真实 Standing。这是状态层错误的投影，风险层无法也不应在单帧内纠正。

### 4.2 Help-Waving 是否因 confidence 过低导致风险偏低？

**confidence 是因素之一，但不是绑定约束；F6 才是。** 004 分解：

| 反事实 | f1 | risk | priority |
|---|---|---|---|
| 实测（conf=0.35, f6=0.620） | 0.599 | 0.371 | LOW |
| conf 升到 0.65（f6 不变） | 0.682 | 0.423 | LOW |
| conf 升到 1.00（f6 不变） | 0.780 | **0.483** | **仍 LOW** |
| conf=1.00 且 f6=0.70 | 0.780 | 0.546 | MEDIUM |
| conf=1.00 且 f6=1.00 | 0.780 | 0.780 | HIGH |

- conf 0.35→1.00 只把 risk 从 0.371 抬到 0.483，**差 0.11，仍进不了 MEDIUM**；
- 在实测 f6=0.620 下，进 MEDIUM 需 conf≥1.10——**数学上不可达**；
- conf=1 时进 MEDIUM 需 f6≥0.641、进 HIGH 需 f6≥0.962；而 f6=det_conf×pose_vis，两个都是 [0,1] 量，清晰照片典型值也就 0.6~0.75。
- 根因：state confidence 低（挥手裕度小：wrist_ratio 0.27 vs 阈值 0.25，margin≈0.07）与 F6 乘性调制是**两条独立压制路径**，修 state 层（让挥手 conf 更高）单独不能把挥手者送出 LOW 段。

### 4.3 是否存在"状态正确但排序不合理"？

存在，且是当前最主要的展示问题：

1. **004 主动挥手求救者 priority=LOW**——设计叙事中 Help-Waving 是第 3 紧急状态（base 0.78），实际输出与"无明确危险信号的站立者"同档；
2. **分差无决策意义**：挥手者 0.371 vs 坐轮椅老人 0.280 vs 站立者 0.276，三态分差 0.09/0.004 量级，priority 标签全是 LOW，救援人员从榜上读不到等级差异；
3. **Sitting 永远 LOW**（理论上限 0.55，MEDIUM 需 f6≥0.91）；**HIGH 全员不可达**——priority 三级分级在当前参数下退化为"LOW/MEDIUM 两级且 MEDIUM 仅 Fall/Lying 在清晰画面下偶尔出现"；
4. **可见度双重惩罚**：pose_vis 既参与 state confidence（state.py `_confidence` 里 0.4+0.6·vis），又作为 f6 乘一次——遮挡导致的不确定性被扣了两遍。

### 4.4 ranking.py 本身有无问题？

无。排序键（risk 降序 → STATE_ORDER → det_conf → pose_vis）、tie-break、STATE_ORDER 表均与 PRD 单帧退化版一致，2a/2b/2c 全 PASS。实测榜序与 f1/f6 可手工复算一致。**问题集中在 risk.py 的分数标定与 priority 阈值，不在排序层。**

## 5. 结论：是否需要调整 risk.py 参数

**需要。ranking.py 不需要动；risk.py 的 F6 调制方式与 priority 阈值存在结构性标定问题，建议在 V0.1.5 内调整（本步按要求未实施）。** 候选方案（按性价比排序）：

| # | 方案 | 改动点 | 预期效果 | 影响面/代价 |
|---|---|---|---|---|
| ① | **priority 阈值按实际分布重定基线**：HIGH≥0.60 / MEDIUM≥0.40（或按分位数） | ranking.py `_priority` 阈值（**本步约束不动 ranking.py，留待下一步决策**） | Fall/Lying 清晰帧→HIGH/MEDIUM，Help→MEDIUM，榜恢复三级区分 | 阈值是展示语义，不动分数；需同步 main.py 配色阈值 `_risk_color`（两处重复定义） |
| ② | **软化 F6 调制**：`f6 = clip(0.5 + 0.5·det_conf·vis, w_min, 1.0)`（或 sqrt 后 floor） | risk.py f6 公式 | 典型证据 0.62→0.81，动态范围从 0.15~0.37 拉开到 ~0.3~0.75；证据差仍降权但不腰斩 | 改变 PRD F6 语义强度，需在报告/注释中说明这是"标定阶段一"调参 |
| ③ | **去除可见度双重惩罚**：f6 只用 det_conf，pose_vis 仅留在 state confidence 中 | risk.py f6 公式 | f6 典型 0.6~0.76→0.6~0.76（det_conf 单独），遮挡不再扣两遍 | 需确认 det_conf 单独能否承载证据调制语义 |
| ④ | Help-Waving 等主动求救状态置信度地板 | state.py（**本步约束不动**） | 挥手 conf 不再被小裕度压到 0.35 | 根因在 state 层挥手 margin 过小（0.27 vs 0.25）；记录为后续 state 话题，且单独做此条不解决 LOW 问题（见 4.2） |

建议组合：**①+②**（阈值重基线 + F6 软化），改动均在 risk/ranking 两处公式级、无架构变更；④留待 state 层下次标定时一并处理。

**不调整的代价**：AIC 演示中所有救援对象均为 LOW、排序榜分差 0.0x 级，视觉与决策上都无法体现"Fall/Lying 高危、挥手求救紧急"的系统叙事——风险层在展示版本中近乎失效。

## 6. 限制声明

- 样本仅 5 图、每图 1 个 civilian（001 为同一人重复框）；Fall/Lying 无任何真实样本进入风险层，高端先验（0.90/0.95）未被实测 exercise；
- 2d/2e 为合成证据分析，用于界定边界，不代表真实画面分布；
- 本报告所有阈值建议为 demo 基线调参依据，正式标定需更多标注样本与视频帧（F2–F5 时序因子在 V0.2 接入后风险分布还会再变）。

## 附：复现命令

```powershell
venv\Scripts\python.exe tests\validate_risk.py     # 本验证(实测+合成检查+归因)
venv\Scripts\python.exe src/main.py --config config\config.yaml --input data\test_images  # 全链路回归, 对 outputs/json 数字
```
