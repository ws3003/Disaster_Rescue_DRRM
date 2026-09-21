"""
Disaster_Rescue_DRRM Demo V0.1 — 实时摄像头输入 (Raspberry Pi 5 + Picamera2)
=============================================================================

职责:
    仅负责 Picamera2 实时帧采集。
    不包含任何算法逻辑。

输出:
    (frame_idx, t_sec, frame_bgr)

说明:
    - 与 video.VideoFrameReader 接口保持一致
    - 供 main.py / edge_client.py 共用
    - 不负责 YOLO / MediaPipe / 风险计算

摄像头:
    Raspberry Pi 5 + OV5647 + Picamera2

图像处理:
    - 保留 Picamera2 RGB888 原始输出
    - 不进行 RGB/BGR 通道交换
    - 修正摄像头物理安装导致的 180° 倒置
"""

import time

import cv2


_CAPTURE_FORMAT = "RGB888"


class CameraFrameReader:
    """
    Picamera2 实时帧读取器。

    输出:
        frame_bgr:
            OpenCV 可直接显示的 ndarray

    接口:
        iter_frames()
        stop()
        release()
        close()
    """

    def __init__(self, cfg: dict | None = None):

        ccfg = (cfg or {}).get("camera", {}) or {}

        self.width = int(ccfg.get("width", 1280))
        self.height = int(ccfg.get("height", 720))
        self.fps = float(ccfg.get("fps", 15))

        self.total_frames = 0

        try:
            from picamera2 import Picamera2

        except ImportError as e:

            raise RuntimeError(
                "无法导入 picamera2。\n"
                "请确认运行环境为 Raspberry Pi。"
            ) from e


        self.picam2 = Picamera2()


        cam_config = self.picam2.create_video_configuration(
            main={
                "size": (
                    self.width,
                    self.height
                ),
                "format": _CAPTURE_FORMAT,
            },
            controls={
                "FrameRate": self.fps,
            },
        )


        self.picam2.configure(cam_config)

        self.picam2.start()


        # 获取实际生效分辨率
        self.width, self.height = (
            self.picam2
            .camera_configuration()["main"]["size"]
        )


        self._closed = False



    def iter_frames(self, frame_stride: int = 1):

        """
        实时产生摄像头帧。

        返回:

            frame_idx,
            时间,
            frame

        """

        stride = max(1, int(frame_stride))


        t0 = time.monotonic()

        idx = -1


        while not self._closed:


            frame_rgb = self.picam2.capture_array()


            if frame_rgb is None:
                break


            idx += 1


            if idx % stride != 0:
                continue



            # ==================================================
            # 关键修复:
            #
            # OV5647 + Picamera2 当前环境:
            #
            # 不需要 RGB -> BGR 转换
            #
            # 否则会导致:
            #   蓝色偏色
            #
            # 同时摄像头安装方向倒置:
            #   旋转180°
            #
            # ==================================================

            frame_bgr = frame_rgb.copy()


            frame_bgr = cv2.rotate(
                frame_bgr,
                cv2.ROTATE_180
            )


            yield (
                idx,
                time.monotonic() - t0,
                frame_bgr
            )



    def stop(self):

        """
        停止摄像头。
        """

        if self._closed:
            return


        self._closed = True


        try:

            self.picam2.stop()
            self.picam2.close()

        except Exception:

            pass



    def release(self):

        self.stop()



    def close(self):

        self.stop()



    def __enter__(self):

        return self



    def __exit__(
        self,
        exc_type,
        exc_val,
        exc_tb
    ):

        self.stop()

        return False
