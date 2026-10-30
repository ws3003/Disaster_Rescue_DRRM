"""DS18B20 One-Wire 温度读取器（E7-01，A2）。

读取 Linux 内核 w1 总线暴露的 DS18B20 设备目录下的 ``w1_slave`` 文件：

    48 01 4b 46 7f ff 04 10 8b : crc=8b YES
    48 01 4b 46 7f ff 04 10 8b t=26750

- 检查 CRC 行包含 ``YES``。
- 解析 ``t=-?\\d+``（单位毫摄氏度），换算为摄氏度。
- 支持按配置指定设备 ID；未指定时自动发现首个 ``28-*`` 设备。
- 设备目录/文件不存在、读取失败、权限不足、CRC 失败、缺字段、格式错误 → SensorReadError。
- 只在 ``read()`` 时访问文件系统，模块导入不触碰 Linux 设备路径（Windows 无副作用导入）。
- Reader 只负责采集返回 Reading；不写数据库、不生成告警。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from src.sensor.model import Reading
from src.sensor.sensor_errors import SensorReadError
from src.sensor.validate import build_reading

# 内核 w1 总线设备目录（Linux）；DS18B20 家族前缀 28-
DEFAULT_W1_DIR = Path("/sys/bus/w1/devices")
DEVICE_PREFIX = "28-"
_SLAVE_NAME = "w1_slave"


def _now_iso() -> str:
    """带时区的 ISO 8601 时间（与 reader._now_iso 一致）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class DS18B20Reader:
    """读取一个 DS18B20 温度读数（℃）。仅温度，不含湿度。"""

    def __init__(
        self,
        device_dir: str | Path = DEFAULT_W1_DIR,
        device_id: Optional[str] = None,
    ) -> None:
        self.base = Path(device_dir)
        self.device_id = device_id

    def _resolve_slave(self) -> Path:
        """定位 w1_slave 文件：指定 device_id，或自动发现首个 28-* 设备。"""
        if not self.base.is_dir():
            raise SensorReadError(f"w1 设备目录不存在: {self.base}")
        if self.device_id:
            slave = self.base / self.device_id / _SLAVE_NAME
            if not slave.is_file():
                raise SensorReadError(f"设备文件不存在: {slave}")
            return slave
        try:
            children = sorted(self.base.iterdir())
        except OSError as exc:
            raise SensorReadError(
                f"无法遍历 w1 设备目录 {self.base}: {type(exc).__name__}: {exc}"
            ) from exc
        for child in children:
            if child.name.startswith(DEVICE_PREFIX):
                slave = child / _SLAVE_NAME
                if slave.is_file():
                    return slave
        raise SensorReadError(
            f"未发现 DS18B20 设备 (目录: {self.base}, 前缀: {DEVICE_PREFIX})"
        )

    @staticmethod
    def _parse_w1_slave(text: str) -> float:
        """校验 CRC 并解析温度（返回 ℃）。"""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            raise SensorReadError(f"w1_slave 内容行数不足 ({len(lines)} 行)")
        if "YES" not in lines[0]:
            raise SensorReadError("CRC 校验失败 (w1_slave 首行无 YES)")
        match = re.search(r"t=(-?\d+)", lines[1])
        if not match:
            raise SensorReadError("w1_slave 缺少 t= 温度字段")
        raw_mc = int(match.group(1))
        return raw_mc / 1000.0  # 毫摄氏度 → 摄氏度

    def read(self, now: Callable[[], str] | None = None) -> Reading:
        ts = (now or _now_iso)()
        slave = self._resolve_slave()
        try:
            text = slave.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SensorReadError(
                f"读取 w1_slave 失败: {slave}; {type(exc).__name__}: {exc}"
            ) from exc
        temp_c = self._parse_w1_slave(text)
        # DS18B20 仅温度：humidity=None；build_reading 内含 E7-02 量程校验
        return build_reading(temp_c, None, ts)

    def close(self) -> None:
        """无持久资源；保留以兼容 ReaderProto 约定。"""
        return None