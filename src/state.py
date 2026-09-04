"""
State layer — instantaneous human state classification from 33 keypoints
(DDRRM V0.1, Step 4)
========================================================================
职责: 基于 MediaPipe 33 关键点的几何规则, 把单一人员 PoseResult 分类为瞬时状态:
      Standing / Sitting / Lying / Fall / Help-Waving, 并附带可解释几何特征与置信度。

范围约束 (V0.1):
  - 仅用关键点几何规则, 不引入任何深度学习分类模型。
  - 不实现 DRRM 风险评分 / 排序 / 视频时序 / Static Lying / TSF。
  - 单帧限制: Fall 仅作为"瞬时异常水平姿态"判断, 不声称真实跌倒事件
    (真实 Fall 需后续帧状态机确认, 属 TSF 阶段)。
  - 状态判定基于可解释特征: body_angle / shoulder_hip_distance /
    wrist_height / knee_angle 等。

判定优先级 (可解释, 单帧):
  1) 躯干近水平 (body_angle 大) -> Fall/Lying 族 (异常水平姿态优先, 呼应 PRD 对 Fall 的定义)
  2) 躯干竖直且手腕明显抬高于肩膀 -> Help-Waving (挥手求救, 依据 raised-arm)
  3) 躯干竖直: 膝角弯曲明显, 或髋-踝垂直跨距压缩(遮挡兜底) -> Sitting; 否则 -> Standing
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from pose import Landmark, PoseResult

# ---------------------------------------------------------------------------
# 几何阈值 (V0.1 手工标定先验, 对应标定阶段一; 均具名便于解释/后续 Grid Search)
# ---------------------------------------------------------------------------
LYING_ANGLE = 55.0        # 躯干与竖直夹角 >= 此值 -> 水平姿态族 (Lying/Fall) [deg]
FALL_KNEE = 150.0         # 水平 + 膝角 >= 此值 -> 肢体伸展(Fall倾向) [deg]
FALL_BODYLEN = 2.2        # (肩-踝跨距)/(躯干长) >= 此值 -> 身体舒展拉长(Fall倾向)
# V0.1.5 Step3 标定 (2026-09-04, tests/calibrate_states.py):
#   挥手样本实测 wrist_ratio=0.27, 原 0.45 无法触发; 非挥手样本该值均为 0, 无回归
WAVE_RATIO = 0.25         # 手腕抬高于同侧肩 >= 0.25x躯干长 -> 挥手信号
SIT_KNEE = 125.0          # 竖直 + 膝角 <= 此值 -> 坐姿 [deg]
STAND_KNEE = 150.0        # 竖直 + 膝角 >= 此值 -> 站立 [deg], 与SIT之间为过渡带
# 坐姿垂直压缩: 髋中点-踝中点垂直跨距 / 躯干垂直长。
# 实测: 站立 1.30~1.39, 坐姿(轮椅) 0.91, 跌坐撑地 1.65 -> 取 1.1 分隔;
# 用途: 膝/踝被遮挡时膝角失真 (坐姿样本膝角实测 158° 看似站立), 压缩特征仍可判坐。
SIT_LEG_RATIO = 1.1       # 竖直 + 髋踝垂直跨距比 <= 此值 -> 坐姿 (垂直压缩)

# MediaPipe 33 关键点标准索引 (已用 PoseLandmark 枚举实证)
_NOSE = 0
_LSH, _RSH = 11, 12
_LEW, _REW = 13, 14
_LWS, _RWS = 15, 16
_LHP, _RHP = 23, 24
_LKN, _RKN = 25, 26
_LAN, _RAN = 27, 28


@dataclass
class StateFeatures:
    """可解释几何特征 (原图像素/角度, 均来自 33 关键点)。"""

    body_angle_deg: float       # 躯干与竖直方向夹角 [0..90]; 0=直立, 90=水平
    shoulder_hip_distance_px: float  # 肩中点-髋中点像素距离(躯干视觉长度)
    wrist_height_ratio: float   # 最高手腕抬高于同侧肩的比值(相对躯干长), >=0
    knee_angle_deg: float       # 双侧膝角均值 (伸直~180, 弯曲~80)
    torso_len_px: float         # 躯干视觉长度 (肩中点-髋中点)
    keypoint_visibility: float  # 决策所用关键点平均可见度 [0,1]
    leg_extended: bool          # 水平姿态下肢体是否伸展拉长 (辅助 Fall/Lying 区分)
    vert_leg_ratio: float       # 髋-踝垂直跨距/躯干垂直长; <=SIT_LEG_RATIO 提示坐姿(身体垂直压缩)


@dataclass
class StateResult:
    """单个人员的瞬时状态分类结果。"""

    state: str                # Standing | Sitting | Lying | Fall | Help-Waving
    confidence: float         # [0,1]
    features: StateFeatures   # 可解释特征


class StateClassifier:
    """基于 33 关键点几何规则的瞬时状态分类器。"""

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def classify(self, pose: PoseResult) -> Optional[StateResult]:
        if not pose.is_ok or not pose.landmarks or len(pose.landmarks) < 33:
            return None  # 无有效关键点, 交由上层跳过该人员
        L = pose.landmarks

        # -- 计算基础几何量 --------------------------------------------
        sh = self._mid(_pt(L, _LSH), _pt(L, _RSH))      # 肩中点
        hp = self._mid(_pt(L, _LHP), _pt(L, _RHP))      # 髋中点
        torso = _dist(sh, hp) if sh and hp else 0.0

        body_angle = self._angle_from_vertical(sh, hp)  # [0,90]

        knee_l = self._joint_angle(_pt(L, _LHP), _pt(L, _LKN), _pt(L, _LAN))
        knee_r = self._joint_angle(_pt(L, _RHP), _pt(L, _RKN), _pt(L, _RAN))
        knee_avg = self._avg([knee_l, knee_r])

        # 手腕抬高于同侧肩 (image y 向下, wrist_y < shoulder_y 表示更高)
        raise_l = max(0.0, (L[_LSH].y - L[_LWS].y)) if torso > 1 else 0.0
        raise_r = max(0.0, (L[_RSH].y - L[_RWS].y)) if torso > 1 else 0.0
        wrist_ratio = max(raise_l, raise_r) / torso if torso > 1 else 0.0

        # 身体舒展度: (肩踝跨距)/(躯干长), 用于水平姿态族内区分为 Fall/Lying
        an_mid = self._mid(_pt(L, _LAN), _pt(L, _RAN))
        bodylen = _dist(sh, an_mid) / torso if (torso > 1 and sh and an_mid) else 0.0
        leg_extended = knee_avg >= FALL_KNEE and bodylen >= FALL_BODYLEN

        # 坐姿垂直压缩: 髋-踝垂直跨距 / 躯干垂直长 (站立~1.3+, 坐姿大腿水平 < SIT_LEG_RATIO)。
        # 踝点缺失时给大值(9.9) -> 不触发坐姿压缩, 保守判站立
        torso_v = abs(sh[1] - hp[1]) if (sh and hp) else 0.0
        vert_leg_ratio = (abs(hp[1] - an_mid[1]) / torso_v) if (torso_v > 1 and an_mid) else 9.9

        feats = StateFeatures(
            body_angle_deg=body_angle,
            shoulder_hip_distance_px=torso,
            wrist_height_ratio=wrist_ratio,
            knee_angle_deg=knee_avg,
            torso_len_px=torso,
            keypoint_visibility=0.0,  # 按分支覆盖
            leg_extended=leg_extended,
            vert_leg_ratio=vert_leg_ratio,
        )

        # -- 分支判定 ---------------------------------------------------
        if body_angle >= LYING_ANGLE:
            # 1) 异常水平姿态族
            if leg_extended:
                state, margin = "Fall", self._marg(body_angle, LYING_ANGLE, 25.0) * 0.6 \
                    + self._marg(knee_avg, FALL_KNEE, 15.0) * 0.4
            else:
                state, margin = "Lying", self._marg(body_angle, LYING_ANGLE, 25.0) * 0.6 \
                    + self._marg(-knee_avg, -(FALL_KNEE - 10.0), 15.0) * 0.4
            vis = self._vis(L, [_LSH, _RSH, _LHP, _RHP, _LKN, _RKN, _LAN, _RAN])
        elif wrist_ratio >= WAVE_RATIO:
            # 2) 竖直 + 挥手
            state, margin = "Help-Waving", self._marg(wrist_ratio, WAVE_RATIO, 0.3)
            vis = self._vis(L, [_LSH, _RSH, _LWS, _RWS, _LHP, _RHP])
        elif knee_avg <= SIT_KNEE or vert_leg_ratio <= SIT_LEG_RATIO:
            # 3) 竖直 + 坐姿: 膝角弯曲, 或身体垂直压缩(膝/踝遮挡致膝角失真时兜底)
            margin = max(
                self._marg(SIT_KNEE - knee_avg, 0.0, 35.0),
                self._marg(SIT_LEG_RATIO - vert_leg_ratio, 0.0, 0.3),
            )
            state = "Sitting"
            vis = self._vis(L, [_LSH, _RSH, _LHP, _RHP, _LKN, _RKN, _LAN, _RAN])
        else:
            # 4) 竖直 + 直立
            state, margin = "Standing", self._marg(knee_avg, STAND_KNEE, 35.0)
            vis = self._vis(L, [_LSH, _RSH, _LHP, _RHP, _LKN, _RKN])

        feats.keypoint_visibility = vis
        confidence = self._confidence(margin, vis)
        # 避免全员可见度极差时给出高置信度的误导性结论
        if vis < 0.3:
            confidence *= 0.5
        return StateResult(state=state, confidence=float(min(confidence, 1.0)), features=feats)

    # ------------------------------------------------------------------
    # 几何工具
    # ------------------------------------------------------------------
    @staticmethod
    def _mid(a: Optional[Tuple[float, float]], b: Optional[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
        if a is None or b is None:
            return a if a else b
        return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)

    @staticmethod
    def _angle_from_vertical(sh, hp) -> float:
        """躯干(髋-肩)方向与竖直方向的夹角 [0,90]。头发/立姿≈0, 平躺≈90。"""
        if not sh or not hp:
            return 0.0
        dx = hp[0] - sh[0]
        dy = hp[1] - sh[1]
        if abs(dy) < 1e-6:
            return 90.0
        return math.degrees(math.atan2(abs(dx), abs(dy)))

    @staticmethod
    def _joint_angle(a, b, c) -> float:
        """三点 a-b-c, 返回 b 处夹角 [0,180]。"""
        if not (a and b and c):
            return 0.0
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 < 1e-6 or n2 < 1e-6:
            return 0.0
        cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        cos = max(-1.0, min(1.0, cos))
        return math.degrees(math.acos(cos))

    @staticmethod
    def _avg(xs) -> float:
        xs = [x for x in xs]
        return sum(xs) / len(xs) if xs else 0.0

    @staticmethod
    def _marg(value, ref, scale) -> float:
        """判定边界裕度 -> [0,1]: value 超过 ref 越多裕度越大。"""
        return max(0.0, min(1.0, (value - ref) / scale)) if scale else 0.0

    @staticmethod
    def _vis(L: List[Landmark], idxs) -> float:
        return StateClassifier._avg([L[i].visibility for i in idxs])

    @staticmethod
    def _confidence(margin: float, vis: float) -> float:
        base = 0.35 + 0.55 * margin      # 边界越明确越高
        return base * (0.4 + 0.6 * vis)  # 证据可见度越高越可信


def _pt(L: List[Landmark], i: int) -> Optional[Tuple[float, float]]:
    return (L[i].x, L[i].y) if i < len(L) else None


def _dist(a, b) -> float:
    if not (a and b):
        return 0.0
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ---------------------------------------------------------------------------
# 最小验证 (Step 4 验收): detector -> pose -> state 对 test.jpg 每名人员分类
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import cv2

    from detector import Detector
    from pose import PoseEstimator

    img = cv2.imread("data/test.jpg")
    if img is None:
        raise SystemExit("无法读取图像: data/test.jpg")

    det = Detector("config/config.yaml")
    persons = det.detect_frame(img)

    pe = PoseEstimator("config/config.yaml")
    poses = pe.estimate_persons(img, persons)

    clf = StateClassifier()
    print(f"[persons] 检测 {len(persons)} 人 -> 姿态成功 {sum(1 for p in poses if p.is_ok)}")
    for p in poses:
        if not p.is_ok:
            print(f"  [{p.status}] {p.detection.class_name:8s} roi={p.roi} (无姿态, 跳过状态) {p.skip_reason}")
            continue
        sr = clf.classify(p)
        if sr is None:
            print(f"  [skip] {p.detection.class_name:8s} 无有效关键点")
            continue
        f = sr.features
        print(f"  [{p.detection.class_name:8s}] state={sr.state:12s} conf={sr.confidence:.2f}")
        print(f"      body_angle={f.body_angle_deg:5.1f}deg shoulder_hip={f.shoulder_hip_distance_px:5.1f}px "
              f"knee={f.knee_angle_deg:5.1f}deg wrist_raise={f.wrist_height_ratio:.2f} "
              f"leg_extended={f.leg_extended} vis={f.keypoint_visibility:.2f}")