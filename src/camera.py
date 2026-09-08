"""
Disaster_Rescue_DRRM Demo V0.1 — 实时摄像头输入 (Raspberry Pi 5 + Picamera2)
=============================================================================
职责: 仅负责 Picamera2 实时帧的采集与 BGR 转换, 不含任何算法逻辑。
感知链路 (detector/pose/state/risk/ranking) 由调用方 (main.run_camera)
逐帧复用 process_one, 本模块只提供 (frame_idx, t_sec, frame_bgr) 三元组流,
接口与 video.VideoFrameReader 鸭子对齐 (fps/width/height + iter_frames + with)。

设计约束:
  - 仅 Raspberry Pi 5 + Picamera2 (CSI 摄像头); 不提供 Windows/USB 摄像头实现;
  - picamera2 为 Pi 系统包 (sudo apt install python3-picamera2), 延迟导入,
    缺失时抛带安装指引的 RuntimeError, 而非原始 ImportError traceback;
  - 背压模型: 处理一帧抓一帧 (capture_array 阻塞取最新帧), 推理慢于采集时
    驱动自动丢弃积压旧帧, 不堆队列、不占内存;
  - 每帧独立推理 (与 V0.2-M0 视频模式一致, pose static_image_mode=True);
    不含 tracking / TSF / 动态 DRRM (pid 帧内编号, 每帧重置)。
"""

import time

import cv2

# Picamera2 采集格式: RGB888 -> 转 BGR 供 OpenCV/管线使用
_CAPTURE_FORMAT = "RGB888"


class CameraFrameReader:
    """Picamera2 实时帧读取器, 接口对齐 video.VideoFrameReader。"""

    def __init__(self, cfg: dict | None = None):
        """
        按 config camera 段配置并启动摄像头。
          width/height: 采集分辨率 (默认 1280x720, Pi Camera 全系列兼容)
          fps:          采集帧率上限 (实际吞吐以推理速度为准, 背压取帧)
        """
        ccfg = (cfg or {}).get("camera", {}) or {}
        self.width = int(ccfg.get("width", 1280))
        self.height = int(ccfg.get("height", 720))
        self.fps = float(ccfg.get("fps", 15))
        # 实时流总帧数未知 (与视频文件区别; 写 JSONL header 时用 0)
        self.total_frames = 0

        try:
            from picamera2 import Picamera2
        except ImportError as e:
            raise RuntimeError(
                "无法导入 picamera2 —— 实时摄像头模式仅支持 Raspberry Pi 5。\n"
                "请在 Pi 上安装系统包:  sudo apt install python3-picamera2\n"
                "Windows/开发机不支持摄像头模式 (可用图片/视频模式验证管线)。"
            ) from e

        self.picam2 = Picamera2()
        cam_config = self.picam2.create_video_configuration(
            main={"size": (self.width, self.height), "format": _CAPTURE_FORMAT},
            controls={"FrameRate": self.fps},
        )
        self.picam2.configure(cam_config)
        self.picam2.start()
        # 实际生效分辨率以传感器/ISP 配置为准 (请求尺寸可能被调整到最接近的支持值)
        self.width, self.height = self.picam2.camera_configuration()["main"]["size"]
        self._closed = False

    def iter_frames(self, frame_stride: int = 1):
        """
        实时帧生成器, yield (frame_idx, t_sec, frame_bgr):
          frame_idx : 已处理帧序号 (0-based, stride 抽帧后连续递增)
          t_sec     : 自首帧起的实时经过秒数 (time.monotonic 计时)
          frame_bgr : np.ndarray BGR 帧 (与 VideoFrameReader 一致)
        背压: 每次循环阻塞抓取最新帧; 调用方推理多慢, 采集就多慢, 不积压。
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
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            yield idx, time.monotonic() - t0, frame_bgr

    def stop(self):
        """停止采集并释放摄像头 (幂等)。"""
        if self._closed:
            return
        self._closed = True
        if self.picam2 is not None:
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

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False
