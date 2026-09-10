import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.monitoring.metrics import get_metrics
from app.providers.base import Business

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    success: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def supported_actions(self) -> list[str]:
        return [self.name]

    def _record_tool_metrics(self, start: float) -> None:
        get_metrics().record_tool_duration(self.name, time.monotonic() - start)

    @abstractmethod
    async def execute(
        self,
        action: str,
        business: Business | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        ...
