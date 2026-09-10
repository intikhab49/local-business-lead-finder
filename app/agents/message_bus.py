import asyncio
import logging
import threading
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.messaging import AgentMessage, MessageStatus, MessageType
from app.core.config import settings

logger = logging.getLogger(__name__)

Handler = Callable[[AgentMessage], Awaitable[AgentMessage | None]]


class BaseMessageBus(ABC):
    """Abstract message bus for agent-to-agent communication.

    Concrete backends (in-memory, Redis, RabbitMQ, Kafka) can be swapped in
    without changing agent implementations, which only depend on this interface.
    """

    @abstractmethod
    def subscribe(self, recipient: str, handler: Handler) -> None:
        """Register a handler that receives messages addressed to recipient."""

    @abstractmethod
    def unsubscribe(self, recipient: str, handler: Handler) -> None:
        """Remove a previously registered handler."""

    @abstractmethod
    async def publish(self, message: AgentMessage) -> list[AgentMessage]:
        """Deliver a message to matching subscribers and return responses."""

    @abstractmethod
    async def request(
        self,
        sender: str,
        recipient: str,
        payload: dict[str, Any],
        message_type: MessageType = MessageType.REQUEST,
        timeout: float = 5.0,
    ) -> AgentMessage | None:
        """Send a request and wait for the correlated response (or timeout)."""

    @abstractmethod
    def get_history(self, recipient: str | None = None) -> list[AgentMessage]:
        """Return the history of published messages, optionally filtered."""

    @abstractmethod
    def clear(self) -> None:
        """Drop subscribers, history and pending requests."""

    @abstractmethod
    def close(self) -> None:
        """Release all resources held by the bus."""


class InMemoryMessageBus(BaseMessageBus):
    """In-process, thread-safe message bus implementation.

    Uses a coroutine-based pub/sub registry guarded by an RLock. Handlers are
    awaited during publish; responses are matched back to pending requests via
    correlation ids. Suitable for single-process deployments and as the default
    for local development and tests.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[AgentMessage] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._published = 0

    def subscribe(self, recipient: str, handler: Handler) -> None:
        with self._lock:
            self._subscribers[recipient].append(handler)
        logger.info(
            "[AgentBus] subscribed handler for recipient=%s", recipient
        )

    def unsubscribe(self, recipient: str, handler: Handler) -> None:
        with self._lock:
            handlers = self._subscribers.get(recipient, [])
            if handler in handlers:
                handlers.remove(handler)
        logger.info(
            "[AgentBus] unsubscribed handler for recipient=%s", recipient
        )

    async def publish(self, message: AgentMessage) -> list[AgentMessage]:
        with self._lock:
            self._history.append(message)
            self._published += 1
            targets = list(self._subscribers.get(message.recipient, ())) + list(
                self._subscribers.get("*", ())
            )

        logger.info(
            "[AgentBus] publish type=%s from=%s to=%s id=%s",
            message.message_type.value,
            message.sender,
            message.recipient,
            message.message_id,
        )

        responses: list[AgentMessage] = []
        for handler in targets:
            try:
                response = await handler(message)
                if response is not None:
                    responses.append(response)
            except Exception as e:
                logger.warning(
                    "[AgentBus] handler for %s raised: %s", message.recipient, e
                )

        with self._lock:
            message.status = MessageStatus.DELIVERED.value

        for response in responses:
            correlation_id = response.correlation_id
            if correlation_id and correlation_id in self._pending:
                future = self._pending.pop(correlation_id)
                if not future.done():
                    future.set_result(response)

        return responses

    async def request(
        self,
        sender: str,
        recipient: str,
        payload: dict[str, Any],
        message_type: MessageType = MessageType.REQUEST,
        timeout: float = 5.0,
    ) -> AgentMessage | None:
        correlation_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            self._pending[correlation_id] = future

        request_message = AgentMessage(
            sender=sender,
            recipient=recipient,
            message_type=message_type,
            payload=payload,
            correlation_id=correlation_id,
        )

        try:
            await self.publish(request_message)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            logger.warning(
                "[AgentBus] request from=%s to=%s timed out after %.1fs",
                sender,
                recipient,
                timeout,
            )
            return None
        finally:
            with self._lock:
                self._pending.pop(correlation_id, None)

    def get_history(self, recipient: str | None = None) -> list[AgentMessage]:
        with self._lock:
            if recipient is None:
                return list(self._history)
            return [
                message
                for message in self._history
                if message.sender == recipient or message.recipient == recipient
            ]

    def get_published_count(self) -> int:
        with self._lock:
            return self._published

    def subscriber_count(self, recipient: str) -> int:
        with self._lock:
            return len(self._subscribers.get(recipient, []))

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._subscribers.clear()
            for future in self._pending.values():
                if not future.done():
                    future.cancel()
            self._pending.clear()
            self._published = 0
        logger.info("[AgentBus] bus cleared")

    def close(self) -> None:
        self.clear()


def create_message_bus(backend: str | None = None) -> BaseMessageBus:
    """Create a message bus based on the configured (or given) backend."""
    backend_name = backend or settings.message_bus_backend
    if backend_name == "in-memory":
        return InMemoryMessageBus()
    raise ValueError(f"Unsupported message bus backend: {backend_name}")
