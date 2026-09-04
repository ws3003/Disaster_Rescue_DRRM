# V0.1.5 Step3 状态识别规则标定实验记录

- 日期：2026-09-04
- 实验工具：`tests/calibrate_states.py`（monkeypatch state.py 模块常量，实验阶段不碰源码）
- 评估工具：`tests/evaluate_states.py` + `tests/test_labels.json`（5 张人工标注图）
- 约束：不重构、不加模型；仅调整几何阈值/新增辅助几何特征；003 检测漏检不属状态层问题

## 结论摘要

| 阶段 | 准确率 | 召回 |
|---|---|---|
| Baseline（标定前） | **1/5 = 20%** | Standing 1/1，其余全 0 |
| 标定后 | **3/5 = 60%**（可检出图片 3/4 = 75%） | Standing 1/1，Help-Waving 1/1，Sitting 1/1 |

- 已修复：004 Help-Waving（`WAVE_RATIO 0.45→0.25`）、005 Sitting（新增垂直压缩特征 `SIT_LEG_RATIO=1.1`）
- 未修复：002 Fall（单帧几何不可分，属 V0.2 TSF）、003 Lying（YOLO 检测漏检，属检测层）
- 001 Standing 全程无回归。

## 1. 标定前规则表（state.py 原始）

分支优先级（自上而下）：

| 优先级 | 条件 | 判定 | 依赖关键点 |
|---|---|---|---|
| 1 | `body_angle >= LYING_ANGLE(55°)` 且 `leg_extended`（`knee≥FALL_KNEE(150°)` 且 `bodylen≥FALL_BODYLEN(2.2)`） | Fall | 肩11/12 髋23/24 膝25/26 踝27/28 |
| 1b | `body_angle >= 55°` 非 leg_extended | Lying | 同上 |
| 2 | `wrist_ratio >= WAVE_RATIO(0.45)`（手腕高于同侧肩 / 躯干长） | Help-Waving | 肩11/12 腕15/16 髋23/24 |
| 3 | `knee_avg <= SIT_KNEE(125°)` | Sitting | 肩 髋 膝25/26 踝27/28 |
| 4 | 其余 | Standing | 肩 髋 膝 |

## 2. Part 0 — 基线几何特征 dump（实测数值）

| 图片 | GT | body_angle | knee | wrist_ratio | leg_ext | vis | 预测 |
|---|---|---|---|---|---|---|---|
| 001_standing #1 | Standing | 2.7° | 178.6° | 0.00 | True | 1.00 | Standing ✓ |
| 001_standing #2 | Standing | 3.0° | 178.3° | 0.00 | True | 0.99 | Standing ✓ |
| 002_fall | Fall | **10.3°** | 129.5° | 0.00 | False | 1.00 | Standing ✗ |
| 003_lying | Lying | —（YOLO 零检测，无姿态） | | | | | 未检出 |
| 004_help | Help-Waving | 1.1° | 177.1° | **0.27** | True | 0.98 | Standing ✗ |
| 005_multi_person | Sitting | 18.7° | **157.9°**（遮挡失真） | 0.00 | False | 0.92 | Standing ✗ |

## 3. Part A — 阈值扫描结果

| 实验 | 修改参数 | 准确率 | 召回变化 | 关键逐图结果 |
|---|---|---|---|---|
| Baseline | 原始参数 | 1/5 = 20% | Standing 1；Fall/Help/Sit/Lying 0 | 002→Standing, 004→Standing, 005→Standing |
| Exp-Fall-1 | LYING_ANGLE=50° | 1/5 = 20% | 无变化 | 002 仍 Standing（body_angle 仅 10.3°，远离阈值） |
| Exp-Fall-2 | LYING_ANGLE=45° | 1/5 = 20% | 无变化 | 同上，001 也未受影响 |
| Exp-Wave-1 | WAVE_RATIO=0.35 | 1/5 = 20% | Help 仍 0 | 004 wrist=0.27 < 0.35，未触发 |
| **Exp-Wave-2** | **WAVE_RATIO=0.25** | **2/5 = 40%** | **Help-Waving 1/1** | **004→Help-Waving ✓**；其余不变 |
| Exp-Sit-1 | SIT_KNEE=135° | 1/5 = 20% | 无净增益 | 005 仍 Standing（膝角 157.9°）；002 被误拉为 Sitting |
| Exp-Sit-2 | SIT_KNEE=145° | 1/5 = 20% | 无净增益 | 同上，002→Sitting(0.59) 但 GT=Fall 仍错 |

结论：
- **LYING_ANGLE 扫描完全无效**：002 跌坐者躯干角仅 10.3°（双手后撑、躯干近直立），降到 45° 也进不了水平姿态族 → Fall 不是阈值问题。
- **SIT_KNEE 上调无效且有副作用**：005 膝角 157.9°（MediaPipe 在轮椅/毯子遮挡下把腿估成接近伸直），膝角路径走不通；上调反而把 002 误判为 Sitting。
- **WAVE_RATIO=0.25 有效**：实测挥手样本抬手比 0.27，非挥手样本（001/002/005）均为 0.00，无回归。

## 4. Part B — Sitting 辅助特征探针（尺度无关）

`vert_leg_ratio` = 髋中点-踝中点垂直跨距 / 躯干垂直长（坐姿大腿水平 → 垂直跨距压缩）。

| 图片 | GT | vert_leg_ratio | hip_pos_in_box | body_compress | 踝点可见度 |
|---|---|---|---|---|---|
| 001_standing #1 | Standing | **1.30** | 0.53 | 1.54 | 0.98 |
| 001_standing #2 | Standing | **1.30** | 0.53 | 1.53 | 0.97 |
| 002_fall | Fall | 1.65 | 0.42 | 1.74 | 0.98 |
| 004_help | Help-Waving | 1.39 | 0.57 | 1.46 | 0.90 |
| **005_multi_person** | **Sitting** | **0.91** | 0.47 | 1.93 | 0.75 |

- 坐姿 0.91 与其余样本（1.30/1.39/1.65）间隔 ≥ 0.39，阈值取 **1.1**，两侧裕度约 0.19/0.20；
- 002（1.65）不会被误触发为 Sitting；004（1.39）先由挥手分支捕获；
- 005 踝点可见度 0.75，特征可靠；踝点缺失时代码给大值 9.9 保守判站立。
- hip_pos_in_box 分离度弱（0.47 vs 0.42~0.57），弃用；body_compress 有效但语义不如 vert_leg_ratio 直接，未采用。

## 5. 标定决策与 state.py 修改（最小改动）

仅改 `src/state.py`，未动 detector/pose/risk/ranking/main/writer：

1. `WAVE_RATIO: 0.45 → 0.25`（阈值常量，附标定注释）；
2. 新增常量 `SIT_LEG_RATIO = 1.1`（坐姿垂直压缩阈值）；
3. `classify()` 新增辅助特征 `vert_leg_ratio = |髋_y-踝_y| / |肩_y-髋_y|`（复用已有的肩/髋/踝中点，踝缺失取 9.9 保守值）；
4. Sitting 分支：`knee_avg <= SIT_KNEE` **或** `vert_leg_ratio <= SIT_LEG_RATIO`，置信度裕度取两者较大值，可见度统计纳入踝点；
5. `StateFeatures` 增加 `vert_leg_ratio` 字段保持可解释性；docstring 同步。

分支新优先级 3：躯干竖直 +（膝角弯曲 **或** 身体垂直压缩）→ Sitting。

## 6. 标定后评估（evaluate_states.py 实测）

```
001_standing      Standing      Standing      ✓ 正确   (框#1 0.80 / 框#2 0.79)
002_fall          Fall          Standing      ✗ 错误   (conf 0.35, 单帧原理性限制)
003_lying         Lying         未检出        ✗ 错误   (YOLO 检测漏检, 检测层问题)
004_help          Help-Waving   Help-Waving   ✓ 正确   (conf 0.35)
005_multi_person  Sitting       Sitting       ✓ 正确   (conf 0.65)

总体准确率: 3/5 = 60.0%
召回: Standing 1/1 | Sitting 1/1 | Help-Waving 1/1 | Fall 0/1 | Lying 0/1(检测层)
```

完整 pipeline（main.py）同步验证：004 输出 Help-Waving（risk 0.371）、005 输出 Sitting（risk 0.280），JSON/排序面板正常，rescuer 仍不参与排序。

## 7. 未解决问题与后续方向

- **002 Fall（单帧不可分）**：跌坐撑地姿态躯干角 10.3°、垂直压缩 1.65，与"坐"几何同源；真实 Fall 是"从立姿到地面"的**转移事件**，需 V0.2 TSF 时序状态机确认（PRD 本就如此定义）。当前单帧框架内不建议继续降 LYING_ANGLE——对 002 无效且会污染真实躺/倒判定。
- **003 Lying（检测层）**：高角度俯拍平躺人体 YOLO 零检测，state.py 任何改动不可达。需检测侧处理（补充训练数据/检测阈值实验），不在本任务范围。
- **样本量限制**：本次标定基于 5 张图（每类仅 1 个正样本，001 重复框），阈值是 **AIC 展示基线**而非泛化指标；正式标定需更多标注样本与网格搜索。

## 附：复现命令

```powershell
venv\Scripts\python.exe tests\calibrate_states.py   # 实验台(特征dump+扫描+探针)
venv\Scripts\python.exe tests\evaluate_states.py    # 官方评估
```
