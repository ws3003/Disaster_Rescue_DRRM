"""
Disaster_Rescue_DRRM V0.2-M0 — 视频 I/O 封装
=============================================
职责: 仅负责视频文件的逐帧读取与标注视频写出, 不含任何算法逻辑。
感知链路 (detector/pose/state/risk/ranking) 由调用方逐帧复用,
本模块只提供 (frame_idx, t_sec, frame_bgr) 三元组流。

V0.2-M0 边界:
  - 仅支持视频文件回放; 不含摄像头实时流 / tracking / TSF;
  - frame_stride 抽帧: frame_idx/t_sec 均按【源视频原始帧号】计时,
    输出视频 fps = 源 fps / stride, 保证回放时长与源视频一致。
"""

from pathlib import Path

import cv2

# 默认支持的视频扩展名 (config video.extensions 可覆盖)
DEFAULT_VIDEO_EXTS = (".mp4", ".avi", ".mov", ".mkv")


def is_video_file(path: str | Path, exts=DEFAULT_VIDEO_EXTS) -> bool:
    """判定路径是否为受支持的视频文件 (按扩展名, 大小写不敏感)。"""
    return Path(path).is_file() and Path(path).suffix.lower() in tuple(e.lower() for e in exts)


class VideoFrameReader:
    """cv2.VideoCapture 封装: 顺序逐帧读取, 支持 frame_stride 抽帧。"""

    def __init__(self, video_path: str | Path):
        self.video_path = Path(video_path)
        self.cap = cv2.VideoCapture(str(self.video_path))
        if not self.cap.isOpened():
            raise FileNotFoundError(f"无法打开视频文件: {self.video_path}")

        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if self.fps <= 0:
            # 个别容器元数据缺失 fps, 兜底 30 避免除零
            self.fps = 30.0

    def iter_frames(self, frame_stride: int = 1):
        """
        逐帧生成器, yield (frame_idx, t_sec, frame_bgr):
          frame_idx : 源视频【原始帧号】(0-based, stride 抽帧时不连续)
          t_sec     : frame_idx / fps (源视频时间轴上的秒数)
          frame_bgr : np.ndarray BGR 帧
        """
        stride = max(1, int(frame_stride))
        idx = -1
        while True:
            ok, frame = self.cap.read()
            if not ok or frame is None:
                break
            idx += 1
            if idx % stride != 0:
                continue
            yield idx, idx / self.fps, frame

    def release(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def close(self):
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


class VideoFrameWriter:
    """cv2.VideoWriter 封装: 写出标注后的 mp4 (默认 mp4v)。"""

    def __init__(self, out_path: str | Path, fps: float, size: tuple, codec: str = "mp4v"):
        self.out_path = Path(out_path)
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.fps = float(fps)
        self.width, self.height = int(size[0]), int(size[1])
        fourcc = cv2.VideoWriter_fourcc(*codec)
        self.writer = cv2.VideoWriter(str(self.out_path), fourcc, self.fps,
                                     (self.width, self.height))
        if not self.writer.isOpened():
            raise RuntimeError(f"无法创建视频写出器: {self.out_path} (codec={codec})")
        self._frames = 0

    def write(self, frame_bgr):
        """写入一帧 (尺寸必须与构造 size 一致)。"""
        self.writer.write(frame_bgr)
        self._frames += 1

    @property
    def frames_written(self) -> int:
        return self._frames

    def release(self):
        if self.writer is not None:
            self.writer.release()
            self.writer = None

    def close(self):
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False
