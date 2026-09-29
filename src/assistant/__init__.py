"""V4.1 Assistant 独立信息辅助决策层。

定位: 灾害现场信息辅助决策助手。
职责: 接收用户问题 → intent 分类 → context 聚合 → 模板回答(信息类)。
独立于 V4.0 决策链: 不调用 rescue_tool / communication_tool, 不产生报警/救援任务。
"""

from .context import AssistantContext  # noqa: F401
from .intent import classify  # noqa: F401
from .responder import AssistantResponder  # noqa: F401

__all__ = ["AssistantContext", "AssistantResponder", "classify"]
__version__ = "0.1"