import logging
import threading
from collections import defaultdict
from typing import Any

from app.agents.messaging import AgentMessage, AgentState

logger = logging.getLogger(__name__)


class AgentContext:
    """Thread-safe shared context for a group of collaborating agents.

    Holds a shared data store, per-agent lifecycle states and a conversation
    history of every message exchanged. All mutation is guarded by an RLock so
    concurrent async tasks can safely read and update context.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._shared: dict[str, Any] = {}
        self._states: dict[str, AgentState] = defaultdict(lambda: AgentState.IDLE)
        self._conversation: list[AgentMessage] = []

    def set_shared(self, key: str, value: Any) -> None:
        with self._lock:
            self._shared[key] = value
        logger.debug("[Context] set shared '%s'", key)

    def get_shared(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._shared.get(key, default)

    def update_shared(self, **kwargs: Any) -> None:
        with self._lock:
            self._shared.update(kwargs)
        logger.debug("[Context] updated shared keys=%s", sorted(kwargs))

    def get_shared_data(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._shared)

    def set_state(self, agent_name: str, state: AgentState) -> None:
        with self._lock:
            self._states[agent_name] = state
        logger.info(
            "[Context] agent=%s state=%s", agent_name, state.value
        )

    def get_state(self, agent_name: str) -> AgentState:
        with self._lock:
            return self._states.get(agent_name, AgentState.IDLE)

    def get_states(self) -> dict[str, str]:
        with self._lock:
            return {name: state.value for name, state in self._states.items()}

    def append_message(self, message: AgentMessage) -> None:
        with self._lock:
            self._conversation.append(message)
        logger.debug(
            "[Context] conversation += %s -> %s (%s)",
            message.sender,
            message.recipient,
            message.message_type.value,
        )

    def get_conversation(self) -> list[AgentMessage]:
        with self._lock:
            return list(self._conversation)

    def get_conversation_for(self, agent_name: str) -> list[AgentMessage]:
        with self._lock:
            return [
                message
                for message in self._conversation
                if message.sender == agent_name or message.recipient == agent_name
            ]

    def conversation_size(self) -> int:
        with self._lock:
            return len(self._conversation)

    def clear_conversation(self) -> None:
        with self._lock:
            self._conversation.clear()
        logger.info("[Context] conversation cleared")

    def clear(self) -> None:
        with self._lock:
            self._shared.clear()
            self._states.clear()
            self._conversation.clear()
        logger.info("[Context] context cleared")

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            conversation = [message.to_dict() for message in self._conversation]
            shared = dict(self._shared)
            states = {name: state.value for name, state in self._states.items()}
        return {
            "shared": shared,
            "states": states,
            "conversation": conversation,
            "conversation_size": len(conversation),
        }
