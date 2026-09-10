import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MessageType(Enum):
    """Semantic category of a message travelling between agents."""

    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    FEEDBACK = "feedback"
    ERROR = "error"


class MessageStatus(Enum):
    """Lifecycle status of a single message."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class AgentState(Enum):
    """Lifecycle state of an agent, tracked in the shared context."""

    IDLE = "idle"
    BUSY = "busy"
    WAITING = "waiting"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class AgentMessage:
    """A structured message exchanged between agents.

    Carries the sender, recipient, payload and correlation metadata so
    request/response pairs can be matched across the message bus.
    """

    sender: str
    recipient: str
    message_type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    correlation_id: str | None = None
    status: str = MessageStatus.PENDING.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "message_type": self.message_type.value,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "status": self.status,
        }
