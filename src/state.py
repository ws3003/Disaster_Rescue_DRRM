"""
State layer — instantaneous human state classification from 33 keypoints
(Disaster_Rescue_DRRM V0.1.8)

职责:
  基于 MediaPipe 33 关键点的几何规则, 把单一人员 PoseResult 分类为:

      Standing
      Sitting
      Lying
      Fall
      Help-Waving

设计原则:
  1. 五种状态分别独立判断。
  2. 最后统一进行状态仲裁。
  3. 优先级:
         Fall > Help-Waving > Lying > Sitting > Standing
  4. 所有尺寸相关特征优先使用 torso-normalized ratio,
     避免人体在图像中的大小变化影响阈值。
  5. Lying / Fall 使用“贴地预检查 + 姿态异常组合”,
     避免仅依赖单个角度。
  6. 单帧 2D 姿态无法可靠判断真正的“跌倒过程”。
     因此 V0.1.8 只做静态几何异常判断。
     后续如需真正的跌倒时序确认, 可在 V0.2 增加 temporal module。
"""

import math
from dataclasses import dataclass
from typing import List, Optional

from pose import PoseResult


# ============================================================
# Thresholds
# ============================================================

# -------------------------
# Lying / Fall
# -------------------------

# 躯干相对于竖直方向的角度。
# 0° = 竖直
# 90° = 水平
GROUND_TORSO_ANGLE = 60.0

# 髋部到脚踝的垂直跨度 / torso长度。
# 真正贴地时, 髋和脚踝在图像中的垂直跨度通常不会很大。
GROUND_HIP_ANKLE_RATIO = 0.65

# 整体身体垂直跨度 / torso长度。
GROUND_VERTICAL_BODY_RATIO = 1.60

# Lying 的基础几何条件。
LYING_THIGH_ANGLE = 40.0
LYING_HORIZONTAL_BODY_RATIO = 1.50
LYING_KNEE_MIN = 110.0

# Fall 的异常条件。
# 在已经满足 on-ground 的情况下, 至少两个异常成立才认为 Fall。
FALL_KNEE_MAX = 110.0
FALL_HORIZONTAL_BODY_MAX = 1.25
FALL_THIGH_ANGLE_MAX = 35.0


# -------------------------
# Help-Waving
# -------------------------

WAVE_RATIO = 0.25


# -------------------------
# Sitting
# -------------------------

SIT_KNEE = 125.0
STAND_KNEE = 150.0

SIT_THIGH_ANGLE = 25.0
SIT_HIP_KNEE_RATIO = 0.20
SIT_KNEE_BELOW_HIP_RATIO = 0.20
SIT_MIN_MARGIN = 0.12

LONG_BODY_RATIO = 2.55
KNEE_HIP_HEIGHT_TOL = 0.12


# ============================================================
# Landmark indices
# ============================================================

_NOSE = 0

_LSH, _RSH = 11, 12
_LEW, _REW = 13, 14
_LWS, _RWS = 15, 16

_LHP, _RHP = 23, 24
_LKN, _RKN = 25, 26
_LAN, _RAN = 27, 28


# ============================================================
# Data structures
# ============================================================

@dataclass
class StateFeatures:
    """
    State classifier 的几何特征。

    原有字段全部保留, 避免影响 main.py / visualize.py 等调用。

    新增字段:
        torso_angle_deg
        body_len_ratio
        thigh_angle_deg
        hip_knee_ratio
        horizontal_body_ratio
        vertical_body_ratio
        hip_ankle_vertical_ratio
    """

    # -------------------------
    # Original fields
    # -------------------------

    body_angle_deg: float
    shoulder_hip_distance_px: float
    wrist_height_ratio: float
    knee_angle_deg: float
    torso_len_px: float
    keypoint_visibility: float
    leg_extended: bool
    vert_leg_ratio: float

    # -------------------------
    # New fields
    # -------------------------

    torso_angle_deg: float
    body_len_ratio: float
    thigh_angle_deg: float
    hip_knee_ratio: float
    horizontal_body_ratio: float
    vertical_body_ratio: float
    hip_ankle_vertical_ratio: float


@dataclass
class StateResult:
    state: str
    confidence: float
    features: StateFeatures


# ============================================================
# Main classifier
# ============================================================

class StateClassifier:
    """
    Five-state instantaneous human state classifier.

    独立检测器:
        1. Fall
        2. Help-Waving
        3. Lying
        4. Sitting
        5. Standing

    最终优先级:
        Fall > Help-Waving > Lying > Sitting > Standing
    """

    def classify(
        self,
        pose: PoseResult,
    ) -> Optional[StateResult]:

        # ----------------------------------------------------
        # 1. Basic validation
        # ----------------------------------------------------

        if pose is None:
            return None

        if not pose.is_ok:
            return None

        L = pose.landmarks

        if L is None:
            return None

        if len(L) < 33:
            return None

        # ----------------------------------------------------
        # 2. Read landmarks
        # ----------------------------------------------------

        l_sh = _pt(L, _LSH)
        r_sh = _pt(L, _RSH)

        l_ws = _pt(L, _LWS)
        r_ws = _pt(L, _RWS)

        l_hp = _pt(L, _LHP)
        r_hp = _pt(L, _RHP)

        l_kn = _pt(L, _LKN)
        r_kn = _pt(L, _RKN)

        l_an = _pt(L, _LAN)
        r_an = _pt(L, _RAN)

        sh = self._mid(l_sh, r_sh)
        hp = self._mid(l_hp, r_hp)
        kn = self._mid(l_kn, r_kn)
        an = self._mid(l_an, r_an)

        # ----------------------------------------------------
        # 3. Basic body geometry
        # ----------------------------------------------------

        torso_len = _dist(sh, hp)

        body_angle = self._angle_from_vertical(
            sh,
            hp,
        )

        # torso angle is intentionally kept as an explicit
        # feature for Lying / Fall rules.
        torso_angle = body_angle

        # ----------------------------------------------------
        # 4. Knee angles
        # ----------------------------------------------------

        left_knee_angle = self._joint_angle(
            l_hp,
            l_kn,
            l_an,
        )

        right_knee_angle = self._joint_angle(
            r_hp,
            r_kn,
            r_an,
        )

        knee_values = []

        if left_knee_angle > 0:
            knee_values.append(left_knee_angle)

        if right_knee_angle > 0:
            knee_values.append(right_knee_angle)

        knee_avg = self._avg(
            knee_values
        )

        # ----------------------------------------------------
        # 5. Wrist raise
        # ----------------------------------------------------

        wrist_ratio = 0.0

        if torso_len > 1e-6:

            raise_values = []

            if l_sh and l_ws:
                raise_values.append(
                    max(
                        0.0,
                        l_sh[1] - l_ws[1],
                    )
                )

            if r_sh and r_ws:
                raise_values.append(
                    max(
                        0.0,
                        r_sh[1] - r_ws[1],
                    )
                )

            if raise_values:
                wrist_ratio = (
                    max(raise_values)
                    / torso_len
                )

        # ----------------------------------------------------
        # 6. Body length
        # ----------------------------------------------------

        body_len_ratio = 0.0

        if torso_len > 1e-6 and sh and an:

            body_len_ratio = (
                _dist(sh, an)
                / torso_len
            )

        # Backward-compatible name.
        vert_leg_ratio = 9.9

        torso_vertical = 0.0

        if sh and hp:
            torso_vertical = abs(
                hp[1] - sh[1]
            )

        if torso_vertical > 1e-6 and hp and an:

            vert_leg_ratio = (
                abs(
                    an[1] - hp[1]
                )
                / torso_vertical
            )

        # ----------------------------------------------------
        # 7. Leg extension
        # ----------------------------------------------------

        leg_extended = (
            knee_avg >= STAND_KNEE
            and body_len_ratio >= 2.20
        )

        # ----------------------------------------------------
        # 8. Thigh angle
        # ----------------------------------------------------

        thigh_values = []

        if l_hp and l_kn:
            thigh_values.append(
                self._angle_from_vertical(
                    l_hp,
                    l_kn,
                )
            )

        if r_hp and r_kn:
            thigh_values.append(
                self._angle_from_vertical(
                    r_hp,
                    r_kn,
                )
            )

        thigh_angle = self._avg(
            thigh_values
        )

        # ----------------------------------------------------
        # 9. Hip-knee horizontal displacement
        # ----------------------------------------------------

        hip_knee_values = []

        if torso_len > 1e-6:

            if l_hp and l_kn:
                hip_knee_values.append(
                    abs(
                        l_kn[0] - l_hp[0]
                    )
                    / torso_len
                )

            if r_hp and r_kn:
                hip_knee_values.append(
                    abs(
                        r_kn[0] - r_hp[0]
                    )
                    / torso_len
                )

        hip_knee_ratio = self._avg(
            hip_knee_values
        )

        # ----------------------------------------------------
        # 10. Horizontal body ratio
        # ----------------------------------------------------

        horizontal_body_ratio = 0.0

        if sh and an and torso_len > 1e-6:

            horizontal_body_ratio = (
                abs(
                    an[0] - sh[0]
                )
                / torso_len
            )

        # ----------------------------------------------------
        # 11. Vertical body ratio
        # ----------------------------------------------------

        vertical_body_ratio = 0.0

        if sh and an and torso_len > 1e-6:

            vertical_body_ratio = (
                abs(
                    an[1] - sh[1]
                )
                / torso_len
            )

        # ----------------------------------------------------
        # 12. Hip -> ankle vertical ratio
        # ----------------------------------------------------

        hip_ankle_vertical_ratio = 9.9

        if hp and an and torso_len > 1e-6:

            hip_ankle_vertical_ratio = (
                abs(
                    an[1] - hp[1]
                )
                / torso_len
            )

        # ----------------------------------------------------
        # 13. Visibility
        # ----------------------------------------------------

        visibility = self._vis(
            L,
            [
                _LSH,
                _RSH,
                _LHP,
                _RHP,
                _LKN,
                _RKN,
                _LAN,
                _RAN,
            ],
        )

        # ----------------------------------------------------
        # 14. Build features
        # ----------------------------------------------------

        features = StateFeatures(
            body_angle_deg=body_angle,
            shoulder_hip_distance_px=torso_len,
            wrist_height_ratio=wrist_ratio,
            knee_angle_deg=knee_avg,
            torso_len_px=torso_len,
            keypoint_visibility=visibility,
            leg_extended=leg_extended,
            vert_leg_ratio=vert_leg_ratio,

            torso_angle_deg=torso_angle,
            body_len_ratio=body_len_ratio,
            thigh_angle_deg=thigh_angle,
            hip_knee_ratio=hip_knee_ratio,
            horizontal_body_ratio=horizontal_body_ratio,
            vertical_body_ratio=vertical_body_ratio,
            hip_ankle_vertical_ratio=(
                hip_ankle_vertical_ratio
            ),
        )

        # ====================================================
        # 15. Independent detectors
        # ====================================================

        fall_ok, fall_margin = (
            self._detect_fall(
                features
            )
        )

        wave_ok, wave_margin = (
            self._detect_help_waving(
                features
            )
        )

        lying_ok, lying_margin = (
            self._detect_lying(
                features
            )
        )

        sitting_ok, sitting_margin = (
            self._detect_sitting(
                features
            )
        )

        standing_ok, standing_margin = (
            self._detect_standing(
                features
            )
        )

        # ====================================================
        # 16. Final arbitration
        # ====================================================

        # Priority:
        # Fall > Help-Waving > Lying > Sitting > Standing

        if fall_ok:

            state = "Fall"
            margin = fall_margin

        elif wave_ok:

            state = "Help-Waving"
            margin = wave_margin

        elif lying_ok:

            state = "Lying"
            margin = lying_margin

        elif sitting_ok:

            state = "Sitting"
            margin = sitting_margin

        elif standing_ok:

            state = "Standing"
            margin = standing_margin

        else:

            # ------------------------------------------------
            # Safe fallback
            # ------------------------------------------------
            #
            # 如果所有规则都不满足, 不强行判成 Fall。
            # Standing 作为默认低风险状态。
            #

            state = "Standing"
            margin = 0.20

        # ----------------------------------------------------
        # 17. Confidence
        # ----------------------------------------------------

        confidence = self._confidence(
            margin,
            visibility,
        )

        # Low visibility means lower confidence.
        if visibility < 0.30:
            confidence *= 0.50

        # Lying detected only through torso angle without
        # complete ground geometry should be lower confidence.
        if (
            state == "Lying"
            and (
                features.hip_ankle_vertical_ratio >= 9.0
                or features.vertical_body_ratio <= 0.0
            )
        ):
            confidence *= 0.50

        # Confidence is bounded.
        confidence = max(
            0.0,
            min(
                1.0,
                confidence,
            ),
        )

        return StateResult(
            state=state,
            confidence=confidence,
            features=features,
        )

    # ========================================================
    # Independent detector: Fall
    # ========================================================

    def _detect_fall(
        self,
        f: StateFeatures,
    ):
        """
        Fall detector V0.1.9.

        Fall is divided into two static posture patterns:

        1. Ground / horizontal fall
           - Used for a person already close to the ground.
           - Keeps the original ground-precheck logic.
           - Requires at least two independent abnormal features.

        2. Forward / kneeling fall
           - Used for a person falling forward or supporting the body
             with the hands, such as a forward-fall posture.
           - Does not require the strict ground precheck.
           - Requires clear torso tilt plus abnormal leg/body evidence.

        Priority is handled by classify():
            Fall > Help-Waving > Lying > Sitting > Standing

        Important:
            The Sitting detector should remain unaffected unless the
            torso is clearly tilted beyond the forward-fall threshold.
        """

        # ========================================================
        # 1. Original ground / horizontal fall detector
        # ========================================================

        on_ground, ground_margin = (
            self._ground_precheck(
                f
            )
        )

        if on_ground:

            anomalies = 0
            anomaly_margins = []

            # ----------------------------------------------------
            # Anomaly 1: bent knee
            # ----------------------------------------------------

            if f.knee_angle_deg > 0:

                if f.knee_angle_deg < FALL_KNEE_MAX:

                    anomalies += 1

                    margin = self._marg(
                        FALL_KNEE_MAX
                        - f.knee_angle_deg,
                        0.0,
                        60.0,
                    )

                    anomaly_margins.append(
                        margin
                    )

            # ----------------------------------------------------
            # Anomaly 2: compressed horizontal body
            # ----------------------------------------------------

            if (
                f.horizontal_body_ratio > 0
                and f.horizontal_body_ratio
                < FALL_HORIZONTAL_BODY_MAX
            ):

                anomalies += 1

                margin = self._marg(
                    FALL_HORIZONTAL_BODY_MAX
                    - f.horizontal_body_ratio,
                    0.0,
                    1.25,
                )

                anomaly_margins.append(
                    margin
                )

            # ----------------------------------------------------
            # Anomaly 3: thigh direction
            # ----------------------------------------------------

            if (
                f.thigh_angle_deg > 0
                and f.thigh_angle_deg
                < FALL_THIGH_ANGLE_MAX
            ):

                anomalies += 1

                margin = self._marg(
                    FALL_THIGH_ANGLE_MAX
                    - f.thigh_angle_deg,
                    0.0,
                    35.0,
                )

                anomaly_margins.append(
                    margin
                )

            # At least two independent anomalies are required.
            if anomalies >= 2:

                anomaly_margin = self._avg(
                    anomaly_margins
                )

                margin = (
                    0.40 * ground_margin
                    + 0.60 * anomaly_margin
                )

                return True, max(
                    0.0,
                    min(
                        1.0,
                        margin,
                    ),
                )

        # ========================================================
        # 2. Forward / kneeling fall detector
        # ========================================================
        #
        # This branch is specifically designed for postures such as:
        #
        #       torso leaning forward
        #             /\\
        #            /    \
        #         hand    hand
        #             \  /
        #              knee
        #
        # The key difference from normal Sitting is that the torso
        # must show a clear tilt.
        #
        # We therefore do NOT use "knee bent" alone.
        #
        # Required:
        #   A. clear torso tilt
        #   B. bent knees
        #   C. at least one additional abnormal body feature
        #
        # This prevents ordinary Sitting from becoming Fall merely
        # because the knees are bent.

        torso_tilt_ok = (
            f.torso_angle_deg >= 35.0
        )

        knee_bent = (
            f.knee_angle_deg > 0
            and f.knee_angle_deg < 125.0
        )

        thigh_low = (
            f.thigh_angle_deg > 0
            and f.thigh_angle_deg < 45.0
        )

        body_compressed = (
            f.horizontal_body_ratio > 0
            and f.horizontal_body_ratio < 1.55
        )

        if (
            torso_tilt_ok
            and knee_bent
            and (
                thigh_low
                or body_compressed
            )
        ):

            margins = []

            # ----------------------------------------------------
            # Torso tilt margin
            # ----------------------------------------------------

            torso_margin = self._marg(
                f.torso_angle_deg,
                35.0,
                55.0,
            )

            margins.append(
                torso_margin
            )

            # ----------------------------------------------------
            # Knee abnormality margin
            # ----------------------------------------------------

            knee_margin = self._marg(
                125.0
                - f.knee_angle_deg,
                0.0,
                55.0,
            )

            margins.append(
                knee_margin
            )

            # ----------------------------------------------------
            # Additional body abnormality
            # ----------------------------------------------------

            if thigh_low:

                thigh_margin = self._marg(
                    45.0
                    - f.thigh_angle_deg,
                    0.0,
                    45.0,
                )

                margins.append(
                    thigh_margin
                )

            if body_compressed:

                body_margin = self._marg(
                    1.55
                    - f.horizontal_body_ratio,
                    0.0,
                    1.55,
                )

                margins.append(
                    body_margin
                )

            margin = self._avg(
                margins
            )

            # Forward-fall confidence is intentionally moderate.
            # The static image cannot prove the temporal event
            # "just fell"; it only identifies a fall-like posture.
            confidence = (
                0.55 * margin
                + 0.45 * torso_margin
            )

            return True, max(
                0.0,
                min(
                    1.0,
                    confidence,
                ),
            )

        # ========================================================
        # 3. Not Fall
        # ========================================================

        return False, 0.0

    # ========================================================
    # Independent detector: Help-Waving
    # ========================================================

    def _detect_help_waving(
        self,
        f: StateFeatures,
    ):
        """
        Help-Waving detector.

        手腕相对于肩部明显抬高时触发。
        """

        if (
            f.wrist_height_ratio
            < WAVE_RATIO
        ):
            return False, 0.0

        margin = self._marg(
            f.wrist_height_ratio,
            WAVE_RATIO,
            0.60,
        )

        return True, margin

    # ========================================================
    # Independent detector: Lying
    # ========================================================

    def _detect_lying(
        self,
        f: StateFeatures,
    ):
        """
        Lying detector.

        基础要求:
            1. torso angle >= 60°
            2. hip-ankle vertical ratio <= 0.65
            3. vertical body ratio <= 1.60

        然后使用:
            thigh angle >= 40°
            horizontal body ratio >= 1.50
            knee >= 110°

        判断典型水平躺姿。

        如果只满足 on-ground, 也允许低置信度 Lying。
        """

        on_ground, ground_margin = (
            self._ground_precheck(
                f
            )
        )

        if not on_ground:
            # 如果脚踝不可用, ground_precheck 会允许
            # torso-angle fallback。
            #
            # 这里仍然只给较低 margin。
            if (
                f.torso_angle_deg
                < GROUND_TORSO_ANGLE
            ):
                return False, 0.0

            margin = self._marg(
                f.torso_angle_deg,
                GROUND_TORSO_ANGLE,
                30.0,
            )

            return True, margin * 0.50

        # ------------------------------------------------
        # Clean horizontal lying
        # ------------------------------------------------

        clean_count = 0
        clean_margins = []

        if (
            f.thigh_angle_deg
            >= LYING_THIGH_ANGLE
        ):

            clean_count += 1

            clean_margins.append(
                self._marg(
                    f.thigh_angle_deg,
                    LYING_THIGH_ANGLE,
                    40.0,
                )
            )

        if (
            f.horizontal_body_ratio
            >= LYING_HORIZONTAL_BODY_RATIO
        ):

            clean_count += 1

            clean_margins.append(
                self._marg(
                    f.horizontal_body_ratio,
                    LYING_HORIZONTAL_BODY_RATIO,
                    1.50,
                )
            )

        if (
            f.knee_angle_deg
            >= LYING_KNEE_MIN
        ):

            clean_count += 1

            clean_margins.append(
                self._marg(
                    f.knee_angle_deg,
                    LYING_KNEE_MIN,
                    70.0,
                )
            )

        # Three clean conditions.
        if clean_count >= 3:

            clean_margin = self._avg(
                clean_margins
            )

            margin = (
                0.40 * ground_margin
                + 0.60 * clean_margin
            )

            return True, margin

        # Two clean conditions:
        # still allow Lying, but lower confidence.
        if clean_count >= 2:

            clean_margin = self._avg(
                clean_margins
            )

            margin = (
                0.40 * ground_margin
                + 0.40 * clean_margin
            )

            return True, min(
                0.70,
                margin,
            )

        # Ground evidence alone:
        # low-confidence Lying.
        return True, min(
            0.45,
            0.50 * ground_margin,
        )

    # ========================================================
    # Independent detector: Sitting
    # ========================================================

    def _detect_sitting(
        self,
        f: StateFeatures,
    ):
        """
        Sitting detector.

        保留 V0.1.7 中已经验证较好的坐姿逻辑。

        重点使用:
            knee angle
            thigh angle
            hip-knee horizontal displacement
            knee below hip
            body length

        避免仅根据 knee angle 判断 Sitting。
        """

        knee_sit = (
            f.knee_angle_deg
            > 0
            and f.knee_angle_deg
            <= SIT_KNEE
        )

        thigh_sit = (
            f.thigh_angle_deg
            >= SIT_THIGH_ANGLE
        )

        hip_knee_sit = (
            f.hip_knee_ratio
            >= SIT_HIP_KNEE_RATIO
        )

        knee_below_hip = (
            self._knee_below_hip_ratio(
                f
            )
            >= SIT_KNEE_BELOW_HIP_RATIO
        )

        suspicious_low_knee = (
            self._knee_below_hip_ratio(
                f
            )
            < KNEE_HIP_HEIGHT_TOL
        )

        long_body = (
            f.body_len_ratio
            >= LONG_BODY_RATIO
        )

        strong_sitting = (
            thigh_sit
            and hip_knee_sit
            and knee_below_hip
        )

        medium_sitting = (
            knee_sit
            and thigh_sit
            and knee_below_hip
        )

        weak_sitting = (
            knee_sit
            and thigh_sit
            and not long_body
            and not suspicious_low_knee
        )

        sitting_ok = (
            strong_sitting
            or medium_sitting
            or weak_sitting
        )

        if not sitting_ok:
            return False, 0.0

        margins = []

        if knee_sit:
            margins.append(
                self._marg(
                    SIT_KNEE
                    - f.knee_angle_deg,
                    0.0,
                    70.0,
                )
            )

        if thigh_sit:
            margins.append(
                self._marg(
                    f.thigh_angle_deg,
                    SIT_THIGH_ANGLE,
                    45.0,
                )
            )

        if hip_knee_sit:
            margins.append(
                self._marg(
                    f.hip_knee_ratio,
                    SIT_HIP_KNEE_RATIO,
                    0.80,
                )
            )

        if knee_below_hip:
            margins.append(
                self._marg(
                    self._knee_below_hip_ratio(
                        f
                    ),
                    SIT_KNEE_BELOW_HIP_RATIO,
                    0.80,
                )
            )

        margin = self._avg(
            margins
        )

        # Weak sitting should have limited confidence.
        if weak_sitting and not (
            strong_sitting
            or medium_sitting
        ):
            margin *= 0.75

        margin = max(
            SIT_MIN_MARGIN,
            margin,
        )

        return True, min(
            1.0,
            margin,
        )

    # ========================================================
    # Independent detector: Standing
    # ========================================================

    def _detect_standing(
        self,
        f: StateFeatures,
    ):
        """
        Standing detector.

        Standing 是最终低风险兜底状态。
        """

        # Clearly lying / ground-like posture should not
        # become Standing.
        if (
            f.torso_angle_deg
            >= GROUND_TORSO_ANGLE
        ):
            return False, 0.0

        knee_margin = 0.0

        if f.knee_angle_deg > 0:

            if (
                f.knee_angle_deg
                >= STAND_KNEE
            ):

                knee_margin = self._marg(
                    f.knee_angle_deg,
                    STAND_KNEE,
                    30.0,
                )

            else:

                # Slightly bent standing is still possible.
                knee_margin = 0.20

        body_margin = self._marg(
            GROUND_TORSO_ANGLE
            - f.torso_angle_deg,
            0.0,
            GROUND_TORSO_ANGLE,
        )

        margin = (
            0.65 * knee_margin
            + 0.35 * body_margin
        )

        return True, max(
            0.20,
            min(
                1.0,
                margin,
            ),
        )

    # ========================================================
    # Ground precheck
    # ========================================================

    def _ground_precheck(
        self,
        f: StateFeatures,
    ):
        """
        判断人体是否具有明显的“贴地 / 水平”特征。

        条件:
            torso_angle >= 60°
            hip_ankle_vertical_ratio <= 0.65
            vertical_body_ratio <= 1.60

        如果脚踝信息缺失:
            不允许直接判 Fall。
            仅允许 torso-angle fallback,
            并降低置信度。

        这是为了避免:
            单纯弯腰
            坐姿
            摄像头视角变化
        被误判成 Fall。
        """

        # ------------------------------------------------
        # First condition: torso orientation
        # ------------------------------------------------

        if (
            f.torso_angle_deg
            < GROUND_TORSO_ANGLE
        ):
            return False, 0.0

        torso_margin = self._marg(
            f.torso_angle_deg,
            GROUND_TORSO_ANGLE,
            30.0,
        )

        # ------------------------------------------------
        # Missing ankle fallback
        # ------------------------------------------------

        if (
            f.hip_ankle_vertical_ratio
            >= 9.0
        ):

            # Cannot assess ground geometry.
            # Do NOT permit Fall from this fallback.
            return True, torso_margin * 0.50

        # ------------------------------------------------
        # Hip -> ankle vertical span
        # ------------------------------------------------

        hip_ankle_ok = (
            f.hip_ankle_vertical_ratio
            <= GROUND_HIP_ANKLE_RATIO
        )

        if not hip_ankle_ok:
            return False, 0.0

        hip_ankle_margin = self._marg(
            GROUND_HIP_ANKLE_RATIO
            - f.hip_ankle_vertical_ratio,
            0.0,
            GROUND_HIP_ANKLE_RATIO,
        )

        # ------------------------------------------------
        # Overall vertical body span
        # ------------------------------------------------

        vertical_body_ok = (
            f.vertical_body_ratio
            <= GROUND_VERTICAL_BODY_RATIO
        )

        if not vertical_body_ok:
            return False, 0.0

        vertical_margin = self._marg(
            GROUND_VERTICAL_BODY_RATIO
            - f.vertical_body_ratio,
            0.0,
            GROUND_VERTICAL_BODY_RATIO,
        )

        margin = (
            0.40 * torso_margin
            + 0.30 * hip_ankle_margin
            + 0.30 * vertical_margin
        )

        return True, max(
            0.0,
            min(
                1.0,
                margin,
            ),
        )

    # ========================================================
    # Sitting helper
    # ========================================================

    @staticmethod
    def _knee_below_hip_ratio(
        f: StateFeatures,
    ):
        """
        这里使用 vert_leg_ratio 作为兼容字段时无法直接恢复
        原始 signed ratio。

        对当前分类而言:
            只要整体 torso angle 未进入 ground region,
            Sitting 仍主要依赖 knee angle + thigh angle
            + hip-knee ratio。

        因此这里使用一个保守的近似。
        """

        if (
            f.vert_leg_ratio
            >= 9.0
        ):
            return 0.50

        # vert_leg_ratio 本身描述的是
        # hip -> ankle 的垂直跨度 / torso vertical span。
        #
        # Sitting 时这个值通常较大或中等。
        return min(
            2.0,
            max(
                0.0,
                f.vert_leg_ratio
                / 2.0,
            ),
        )

    # ========================================================
    # Geometry helpers
    # ========================================================

    @staticmethod
    def _mid(a, b):

        if a is None or b is None:

            return (
                a
                if a is not None
                else b
            )

        return (
            (a[0] + b[0]) / 2.0,
            (a[1] + b[1]) / 2.0,
        )

    @staticmethod
    def _angle_from_vertical(a, b):
        """
        计算 a -> b 相对于竖直方向的夹角。

        0°  = 竖直
        90° = 水平
        """

        if not a or not b:
            return 0.0

        dx = b[0] - a[0]
        dy = b[1] - a[1]

        if (
            abs(dx) < 1e-6
            and abs(dy) < 1e-6
        ):
            return 0.0

        if abs(dy) < 1e-6:
            return 90.0

        return math.degrees(
            math.atan2(
                abs(dx),
                abs(dy),
            )
        )

    @staticmethod
    def _joint_angle(a, b, c):
        """
        计算 A-B-C 的关节角度。
        """

        if not (
            a
            and b
            and c
        ):
            return 0.0

        v1 = (
            a[0] - b[0],
            a[1] - b[1],
        )

        v2 = (
            c[0] - b[0],
            c[1] - b[1],
        )

        n1 = math.hypot(
            v1[0],
            v1[1],
        )

        n2 = math.hypot(
            v2[0],
            v2[1],
        )

        if (
            n1 < 1e-6
            or n2 < 1e-6
        ):
            return 0.0

        cos_value = (
            v1[0] * v2[0]
            + v1[1] * v2[1]
        ) / (
            n1 * n2
        )

        cos_value = max(
            -1.0,
            min(
                1.0,
                cos_value,
            ),
        )

        return math.degrees(
            math.acos(
                cos_value
            )
        )

    @staticmethod
    def _avg(
        xs: List[float]
    ):

        xs = [
            float(x)
            for x in xs
            if x is not None
        ]

        if not xs:
            return 0.0

        return sum(xs) / len(xs)

    @staticmethod
    def _marg(
        value,
        ref,
        scale,
    ):

        if not scale:
            return 0.0

        return max(
            0.0,
            min(
                1.0,
                (value - ref) / scale,
            ),
        )

    @staticmethod
    def _vis(
        L,
        idxs,
    ):

        values = []

        for i in idxs:

            if i < len(L):

                values.append(
                    float(
                        L[i].visibility
                    )
                )

        return StateClassifier._avg(
            values
        )

    @staticmethod
    def _confidence(
        margin,
        vis,
    ):

        base = (
            0.35
            + 0.55 * margin
        )

        return base * (
            0.4
            + 0.6 * vis
        )


# ============================================================
# Landmark helper
# ============================================================

def _pt(
    L,
    i,
):

    if i >= len(L):
        return None

    return (
        L[i].x,
        L[i].y,
    )


def _dist(
    a,
    b,
):

    if not (
        a
        and b
    ):
        return 0.0

    return math.hypot(
        a[0] - b[0],
        a[1] - b[1],
    )


# ============================================================
# Standalone test
# ============================================================

if __name__ == "__main__":

    import cv2

    from detector import Detector
    from pose import PoseEstimator

    img = cv2.imread(
        "data/test.jpg"
    )

    if img is None:

        raise SystemExit(
            "无法读取图像: data/test.jpg"
        )

    det = Detector(
        "config/config.yaml"
    )

    persons = det.detect_frame(
        img
    )

    pe = PoseEstimator(
        "config/config.yaml"
    )

    poses = pe.estimate_persons(
        img,
        persons,
    )

    clf = StateClassifier()

    print(
        f"[persons] 检测 {len(persons)} 人 "
        f"-> 姿态成功 "
        f"{sum(1 for p in poses if p.is_ok)}"
    )

    for p in poses:

        if not p.is_ok:

            print(
                f"  [{p.status}] "
                f"{p.detection.class_name:8s} "
                f"roi={p.roi} "
                f"(无姿态, 跳过状态) "
                f"{p.skip_reason}"
            )

            continue

        sr = clf.classify(
            p
        )

        if sr is None:

            print(
                f"  [skip] "
                f"{p.detection.class_name:8s} "
                f"无有效关键点"
            )

            continue

        f = sr.features

        print(
            f"  [{p.detection.class_name:8s}] "
            f"state={sr.state:12s} "
            f"conf={sr.confidence:.2f}"
        )

        print(
            f"      "
            f"body_angle={f.body_angle_deg:5.1f}deg "
            f"shoulder_hip={f.shoulder_hip_distance_px:5.3f} "
            f"knee={f.knee_angle_deg:5.1f}deg "
            f"wrist_raise={f.wrist_height_ratio:.2f} "
            f"leg_extended={f.leg_extended} "
            f"vert_leg={f.vert_leg_ratio:.2f} "
            f"vis={f.keypoint_visibility:.2f}"
        )

        print(
            f"      "
            f"torso_angle={f.torso_angle_deg:5.1f}deg "
            f"body_len={f.body_len_ratio:.2f} "
            f"thigh_angle={f.thigh_angle_deg:5.1f}deg "
            f"hip_knee={f.hip_knee_ratio:.2f} "
            f"h_body={f.horizontal_body_ratio:.2f} "
            f"v_body={f.vertical_body_ratio:.2f} "
            f"hip_ankle_v={f.hip_ankle_vertical_ratio:.2f}"
        )
