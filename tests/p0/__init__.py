"""tests/p0 包: 导入时引导 sys.path, 使 p0 测试在两种运行方式下均可导入。

支持:
  python -m unittest discover -s tests/p0 -t tests/p0   (p0 独立 / 旧方式)
  python -m unittest discover -s tests -t tests          (全量: p0 + p1)
"""

import os
import sys

# 本目录(tests/p0): 使 `import _setup` 可达 (p0 作为包被顶层 discover 导入时)
_P0_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录: 使 `import src...` 可达
_ROOT = os.path.abspath(os.path.join(_P0_DIR, "..", ".."))

for _p in (_P0_DIR, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)