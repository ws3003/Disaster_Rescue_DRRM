"""V4.4 传感器基础模块（阶段 A1：纯软件，不依赖真实 GPIO/DHT11）。"""

from src.sensor.reader import FakeReader, SensorReader, read_sample  # noqa: F401
from src.sensor.model import Reading  # noqa: F401
from src.sensor.sensor_errors import SensorReadError  # noqa: F401
from src.sensor.validate import build_reading, is_valid_sample  # noqa: F401