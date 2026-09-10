import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Action:
    name: str = field(compare=True)
    priority: int = field(default=100, compare=False)
    description: str = field(default="", compare=False)


SKIP_ENRICHMENT = Action("skip_enrichment", priority=100, description="No enrichment needed")
DISCOVER_WEBSITE = Action("discover_website", priority=10, description="Discover business website")
VERIFY_WEBSITE = Action("verify_website", priority=20, description="Verify website ownership")
SCRAPE_WEBSITE = Action("scrape_website", priority=30, description="Scrape website for contact info")
EXTRACT_EMAIL = Action("extract_email", priority=40, description="Extract emails from scraped HTML")
DISCOVER_SOCIAL = Action("discover_social", priority=50, description="Discover social profiles")


class BaseDecisionEngine(ABC):
    @abstractmethod
    async def decide(self, state: dict[str, Any]) -> list[Action]:
        ...


class RuleBasedDecisionEngine(BaseDecisionEngine):
    async def decide(self, state: dict[str, Any]) -> list[Action]:
        actions: list[Action] = []
        has_website = bool(state.get("website"))
        has_email = bool(state.get("email"))

        if not has_website:
            actions.append(DISCOVER_WEBSITE)
            actions.append(SCRAPE_WEBSITE)
            actions.append(EXTRACT_EMAIL)

        if has_website and not has_email:
            actions.append(SCRAPE_WEBSITE)
            actions.append(EXTRACT_EMAIL)

        if has_website:
            actions.append(DISCOVER_SOCIAL)

        if not actions:
            actions.append(SKIP_ENRICHMENT)

        self._log_decision(state, actions)
        return actions

    def _log_decision(self, state: dict[str, Any], actions: list[Action]) -> None:
        name = state.get("name", state.get("business_name", "unknown"))
        action_names = [a.name for a in actions if a.name != "skip_enrichment"]
        if not action_names:
            action_names = ["skip_enrichment"]

        logger.info(
            "[Decision] Business: %s\n[Decision] Actions: %s",
            name, ", ".join(action_names),
        )
