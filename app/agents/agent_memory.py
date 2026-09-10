import logging
from typing import Any

logger = logging.getLogger(__name__)


class AgentMemory:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, place_id: str, key: str, default: Any = None) -> Any:
        return self._store.get(place_id, {}).get(key, default)

    def set(self, place_id: str, key: str, value: Any) -> None:
        if place_id not in self._store:
            self._store[place_id] = {}
        self._store[place_id][key] = value
        logger.debug("[Memory] %s[%s] = %s", place_id, key, value)

    def has(self, place_id: str, key: str) -> bool:
        return key in self._store.get(place_id, {})

    def get_all(self, place_id: str) -> dict[str, Any]:
        return dict(self._store.get(place_id, {}))

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._store.items())

    def clear(self) -> None:
        self._store.clear()
