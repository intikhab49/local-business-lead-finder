import logging
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any

from app.agents.context import AgentContext
from app.agents.message_bus import BaseMessageBus
from app.agents.messaging import AgentMessage, AgentState, MessageType
from app.core.config import settings

logger = logging.getLogger(__name__)


def track_state(func: Any) -> Any:
    """Decorator that records BUSY/COMPLETE/FAILED agent state transitions."""

    @wraps(func)
    async def wrapper(self: "BaseAgent", *args: Any, **kwargs: Any) -> Any:
        self._set_state(AgentState.BUSY)
        try:
            result = await func(self, *args, **kwargs)
            self._set_state(AgentState.COMPLETE)
            return result
        except Exception:
            self._set_state(AgentState.FAILED)
            raise

    return wrapper


class BaseAgent(ABC):
    """Base class for all specialized agents.

    Agents communicate with each other and with the coordinator through
    structured request/response models. When a shared context and message bus
    are bound, agents can also publish events, request work from one another
    and track their lifecycle state.
    """

    name: str = "base"

    def __init__(
        self,
        context: AgentContext | None = None,
        bus: BaseMessageBus | None = None,
    ) -> None:
        self._context = context
        self._bus = bus
        self._state = AgentState.IDLE

    @abstractmethod
    async def execute(self, request: Any) -> Any:
        ...

    def bind(self, context: AgentContext, bus: BaseMessageBus) -> None:
        """Attach a shared context and message bus after construction."""
        self._context = context
        self._bus = bus

    def _set_state(self, state: AgentState) -> None:
        self._state = state
        if self._context is not None:
            self._context.set_state(self.name, state)
        logger.info("[AgentState] %s state=%s", self.name, state.value)

    def get_state(self) -> AgentState:
        if self._context is not None:
            return self._context.get_state(self.name)
        return self._state

    async def send_message(
        self,
        recipient: str,
        payload: dict[str, Any],
        message_type: MessageType = MessageType.EVENT,
    ) -> AgentMessage | None:
        """Fire-and-forget message delivery through the bound bus."""
        if self._bus is None:
            logger.debug(
                "[AgentBus] %s has no bus bound; message to %s dropped",
                self.name,
                recipient,
            )
            return None
        message = AgentMessage(
            sender=self.name,
            recipient=recipient,
            message_type=message_type,
            payload=payload,
        )
        if self._context is not None:
            self._context.append_message(message)
        await self._bus.publish(message)
        return message

    async def request_work(
        self,
        recipient: str,
        payload: dict[str, Any],
        message_type: MessageType = MessageType.REQUEST,
        timeout: float | None = None,
    ) -> AgentMessage | None:
        """Send a request to another agent and await its correlated response."""
        if self._bus is None:
            return None
        if timeout is None:
            timeout = settings.agent_feedback_timeout
        response = await self._bus.request(
            sender=self.name,
            recipient=recipient,
            payload=payload,
            message_type=message_type,
            timeout=timeout,
        )
        if response is not None and self._context is not None:
            self._context.append_message(response)
        return response

    async def handle_message(self, message: AgentMessage) -> AgentMessage | None:
        """Process an incoming message and optionally return a response."""
        logger.info(
            "[%s] Received message type=%s from=%s",
            self.name,
            message.message_type.value,
            message.sender,
        )
        return self._reply(
            message,
            data={},
            success=False,
            error=f"message type {message.message_type.value} not supported by {self.name}",
        )

    def _reply(
        self,
        message: AgentMessage,
        data: dict[str, Any],
        success: bool = True,
        error: str | None = None,
        message_type: MessageType = MessageType.RESPONSE,
    ) -> AgentMessage:
        return AgentMessage(
            sender=self.name,
            recipient=message.sender,
            message_type=message_type,
            correlation_id=message.correlation_id,
            payload={"success": success, "data": data, "error": error},
        )
