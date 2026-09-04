# V0.1.5 Step5 — DRRM 风险评分重新标定报告

- 日期：2026-09-04
- 实验工具：`tests/calibrate_risk.py`（不改源码的公式复算实验台：真实 5 图 + 五状态合成证据网格 + 阈值方案对比）
- 验证工具：`tests/validate_risk.py`（标定后重跑，2a/2b/2c 全 PASS）
- 设计约束：`R = F1 × F6` 聚合框架不变；F1 状态先验表（Fall 0.95 / Lying 0.90 / Help 0.78 / Sitting 0.55 / Standing 0.35）不变；排序键与 STATE_ORDER 不变。

## 结论摘要

1. **F6 软化有效**：`f6 = 0.5 + 0.5×(det_conf×pose_vis)`（f6∈[0.5,1.0]）替代原纯乘性 `clip(det_conf×pose_vis, 0.25, 1)`，真实图风险分布从 **0.15~0.37（mean 0.266）** 拉开到 **0.27~0.49（mean 0.367）**。
2. **priority 阈值重标定为 HIGH≥0.60 / MEDIUM≥0.45**（原 0.75/0.50 在 F6 软化后分布下仍使全员 LOW：软化+旧阈值实测 0/5 进 MEDIUM）。
3. **标定后实测**：004 Help-Waving **0.485 MEDIUM**、005 Sitting 0.406 LOW、001 Standing 0.328/0.267 LOW、002（误判 Standing）0.350 LOW——主动求救者首次获得非 LOW 等级，其余标签符合救援语义。
4. **五状态合成验证**（代表场景 conf=0.65、中证据 raw=0.60，f6=0.80）：Fall 0.634 **HIGH**、Lying 0.608 **HIGH**、Help-Waving 0.546 **MEDIUM**、Sitting 0.426 LOW、Standing 0.322 LOW——与危险序 Fall>Lying>Help>Sitting>Standing 及救援语义完全一致。
5. 设计符合性回归：2a 先验单调 / 2b 等证据五状态序 / 2c 同分 tie-break 全部 PASS；危险序网格 12/12 无反转。

## 1. 实验设计

| 项 | 内容 |
|---|---|
| 实验 A（F6） | baseline：`f6 = clip(raw, w_min=0.25, 1)`；experiment：`f6 = 0.5 + 0.5×raw`（clip 上限 1，自然下界 0.5）。raw = det_conf × pose_vis。R = f1 × f6 不变，f1 = 0.5+(base−0.5)×conf 不变 |
| 实验 B（阈值） | S0 当前 0.75/0.50；S1 0.60/0.40；S2 0.65/0.45；**S3 0.60/0.45**（数据驱动新增） |
| 真实数据 | data/test_images 5 图 6 个有效 civilian 框（Fall/Lying 无真实样本：002 状态误判、003 检测漏检） |
| 合成数据 | 五状态 × conf{0.35, 0.65, 1.0} × 证据 raw{弱 0.45 / 中 0.60 / 清晰 0.75}，覆盖 Fall/Lying 高端 |

## 2. 修改前后风险分布（真实图片）

| 图片 | state | conf | raw | f6 基线 | R 基线 | f6 软化 | R 软化 | priority 基线+S0 | priority 软化+S0 | **priority 软化+S3** |
|---|---|---|---|---|---|---|---|---|---|---|
| 001_standing #1 | Standing | 0.80 | 0.726 | 0.726 | 0.276 | 0.863 | 0.328 | LOW | LOW | **LOW** |
| 001_standing #2 | Standing（重复框） | 0.79 | 0.399 | 0.399 | 0.152 | 0.699 | 0.267 | LOW | LOW | **LOW** |
| 002_fall | Standing ✗(GT=Fall) | 0.35 | 0.566 | 0.566 | 0.253 | 0.783 | 0.350 | LOW | LOW | **LOW** |
| 004_help | Help-Waving ✓ | 0.35 | 0.620 | 0.620 | 0.371 | 0.810 | **0.485** | LOW | LOW | **MEDIUM** |
| 005_multi_person | Sitting ✓ | 0.65 | 0.525 | 0.525 | 0.280 | 0.763 | 0.406 | LOW | LOW | **LOW** |

- 分布：baseline **min 0.152 / max 0.371 / mean 0.266** → 软化 **min 0.267 / max 0.485 / mean 0.367**；
- priority 计数：基线+S0 = HIGH 0 / MEDIUM 0 / LOW 5；软化+S0 = 0/0/5（**仅软化不够**）；软化+**S3** = 0/1/4。
- 跨图虚拟榜（标定后）：#1 Help-Waving 0.485 MEDIUM > #2 Sitting 0.406 LOW > #3 002 Standing 0.350 LOW > #4 001#1 0.328 LOW > #5 001#2 0.267 LOW。序方向与危险序一致。

## 3. 五状态风险排序（合成证据网格，F6 软化 + S3）

代表场景 conf=0.65：

| 证据 | f6 | Fall | Lying | Help-Waving | Sitting | Standing |
|---|---|---|---|---|---|---|
| 弱 raw=0.45 | 0.725 | 0.575 M | 0.551 M | 0.494 M | 0.386 L | 0.292 L |
| 中 raw=0.60 | 0.800 | **0.634 H** | **0.608 H** | 0.546 M | 0.426 L | 0.322 L |
| 清晰 raw=0.75 | 0.875 | **0.693 H** | **0.665 H** | 0.597 M | 0.466 M | 0.352 L |

（H=HIGH / M=MEDIUM / L=LOW；完整 conf×证据网格见 calibrate_risk.py Part B 输出。）

分级可达性（conf=1，F6 软化，S3）：

| state | R 上限 | 清晰 raw=.75 | 中 raw=.60 | 弱 raw=.45 | 可达最高级 |
|---|---|---|---|---|---|
| Fall | 0.950 | 0.831 HIGH | 0.760 HIGH | 0.689 HIGH | **HIGH** |
| Lying | 0.900 | 0.787 HIGH | 0.720 HIGH | 0.652 HIGH | **HIGH** |
| Help-Waving | 0.780 | 0.682 HIGH | 0.624 HIGH | 0.566 MEDIUM | HIGH（仅 conf=1 且中证据以上） |
| Sitting | 0.550 | 0.481 MEDIUM | 0.440 LOW | 0.399 LOW | MEDIUM |
| Standing | 0.350 | 0.306 LOW | 0.280 LOW | 0.254 LOW | **LOW（永不可达 MEDIUM）** |

## 4. priority 阈值方案对比（为什么选 S3 0.60/0.45）

代表场景 conf=0.65、中证据、F6 软化：

| state | R | S0 (0.75/0.50) | S1 (0.60/0.40) | S2 (0.65/0.45) | **S3 (0.60/0.45)** | 语义期望 |
|---|---|---|---|---|---|---|
| Fall | 0.634 | MEDIUM | HIGH | MEDIUM | **HIGH** | HIGH/MEDIUM |
| Lying | 0.608 | MEDIUM | HIGH | MEDIUM | **HIGH** | HIGH/MEDIUM |
| Help-Waving | 0.546 | MEDIUM | MEDIUM | MEDIUM | **MEDIUM** | MEDIUM+ |
| Sitting | 0.426 | LOW | MEDIUM ✗ | LOW | **LOW** | LOW/MEDIUM |
| Standing | 0.322 | LOW | LOW | LOW | **LOW** | LOW |

- **S0**：Fall/Lying 典型证据仅 MEDIUM，Help 在真实数据（conf 0.35）下 0.485 <0.50 仍 LOW——等级表达不足；
- **S1**：MEDIUM 线 0.40 过低，Sitting 0.426 渗漏为 MEDIUM，与"低危坐姿"语义冲突；
- **S2**：HIGH 线 0.65 过严，典型证据 Fall 0.634 / Lying 0.608 都进不了 HIGH，清晰帧才 H——HIGH 档近乎虚设；
- **S3**：Fall/Lying 中证据即 HIGH（弱证据 0.575/0.551 保 MEDIUM，不漏判可能跌倒）、Help 稳定 MEDIUM、Sitting 仅在 conf=1 清晰帧才 MEDIUM（0.481）、Standing 恒 LOW。四个阈值方案中唯一全部命中语义期望。

## 5. 救援语义符合性

| 语义要求 | 标定后表现 | 结论 |
|---|---|---|
| Fall/Lying 最危急，清晰帧应 HIGH | Fall/Lying 中证据即 HIGH（0.634/0.608），弱证据保 MEDIUM | ✓ |
| 主动挥手求救应显著高于普通人 | 004 实测 0.485 MEDIUM，为真实图最高；合成 Help 稳定 MEDIUM | ✓ |
| 坐姿/轮椅低危 | 005 实测 0.406 LOW；Sitting 上限 MEDIUM 不进 HIGH | ✓ |
| 站立安全 | Standing 理论上限 0.35，恒 LOW | ✓ |
| 危险序 Fall>Lying>Help>Sitting>Standing | 同证据网格 12/12 无反转；2a/2b/2c PASS | ✓ |
| 误判不放高危 | 002 误判 Standing（conf 0.35）0.350 LOW | ✓（误判的根本修复在状态层/检测层，见 Step3 报告） |

已知边界（如实记录）：
- Help-Waving 在 conf=1 + 中证据以上可达 HIGH（0.624）——理论可能，实测 conf 0.35 下为 MEDIUM；语义上"证据完美的主动求救"给 HIGH 可接受，不做额外压制以免再引入特例；
- F6 软化后 w_min=0.25 实际不再触发（f6 自然下界 0.5），保留为硬下限仅具配置兼容意义；
- pose_vis 双重惩罚（state confidence 与 F6 各一次）本次未动——软化已把 f6 抬到 0.5~1.0 区间，双重惩罚效应被大幅稀释，留待更多样本时复核。

## 6. 代码变更（最小改动）

| 文件 | 变更 |
|---|---|
| [src/risk.py](../src/risk.py) | F6 公式：`f6 = max(w_min, min(1.0, 0.5 + 0.5×clip(det_conf×pose_vis,0,1)))`；docstring 标注 Step5 标定依据。F1/聚合 R=f1×f6/state_risk 表不变 |
| [src/ranking.py](../src/ranking.py) | `_priority()` 阈值 0.75/0.50 → **0.60/0.45**（仅分级常量；排序键、STATE_ORDER、tie-break 全部不动），docstring 同步 |
| [src/main.py](../src/main.py) | `_risk_color()` 配色阈值同步 0.60/0.45（与 ranking 分级保持一致） |
| [tests/calibrate_risk.py](../tests/calibrate_risk.py) | 新增：标定实验台（Part A/B/C） |
| [tests/validate_risk.py](../tests/validate_risk.py) | 2e/Part3 阈值分析更新为标定后分级（0.60/0.45），反事实 f6 区间改为 [0.5,1.0] |

未改动：detector.py / pose.py / state.py / config.yaml / writer.py。未进入 V0.2、无 TSF。

## 7. 限制声明

- Fall/Lying 仍无真实样本进入风险层（002 状态误判、003 检测漏检），HIGH 档证据来自合成网格，需真实跌倒/躺卧图片回归；
- 阈值基于 5 图 demo 分布与合成证据标定，为 AIC 展示基线；F2–F5 时序因子 V0.2 接入后风险分布将再标定；
- 004 的 state confidence 仍仅 0.35（挥手裕度小，state 层话题），当前靠 F6 软化 + S3 使其进入 MEDIUM；后续若 state conf 提升，其 risk 自然升至 0.55~0.63。

## 附：复现命令

```powershell
venv\Scripts\python.exe tests\calibrate_risk.py    # 标定实验(修改前后分布/网格/方案对比)
venv\Scripts\python.exe tests\validate_risk.py     # 标定后设计符合性回归(2a/2b/2c PASS)
venv\Scripts\python.exe src\main.py --config config\config.yaml --input data\test_images
```
