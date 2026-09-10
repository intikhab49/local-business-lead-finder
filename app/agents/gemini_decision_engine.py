import logging
from typing import Any

from app.agents.decision_engine import (
    DISCOVER_SOCIAL,
    DISCOVER_WEBSITE,
    EXTRACT_EMAIL,
    SCRAPE_WEBSITE,
    VERIFY_WEBSITE,
    Action,
    BaseDecisionEngine,
    RuleBasedDecisionEngine,
)
from app.llm.gemini_client import (
    GeminiClient,
    GeminiError,
    GeminiInvalidResponseError,
)
from app.prompts.planner_prompt import (
    PLANNER_SYSTEM_PROMPT,
    build_planner_prompt,
)

logger = logging.getLogger(__name__)

_ACTION_CATALOG: dict[str, Action] = {
    DISCOVER_WEBSITE.name: DISCOVER_WEBSITE,
    VERIFY_WEBSITE.name: VERIFY_WEBSITE,
    SCRAPE_WEBSITE.name: SCRAPE_WEBSITE,
    EXTRACT_EMAIL.name: EXTRACT_EMAIL,
    DISCOVER_SOCIAL.name: DISCOVER_SOCIAL,
}


class GeminiDecisionEngine(BaseDecisionEngine):
    def __init__(
        self,
        client: GeminiClient | None = None,
        fallback_engine: BaseDecisionEngine | None = None,
    ) -> None:
        self._client = client or GeminiClient()
        self._fallback = fallback_engine or RuleBasedDecisionEngine()

    async def decide(self, state: dict[str, Any]) -> list[Action]:
        if not self._client.is_configured:
            logger.warning(
                "[LLM] Gemini API key not configured — falling back to RuleBasedDecisionEngine"
            )
            return await self._fallback.decide(state)

        try:
            prompt = build_planner_prompt(state)
            logger.info("[LLM] Prompt sent: business=%s", state.get("name", "unknown"))

            result = await self._client.generate(
                prompt=prompt,
                system_prompt=PLANNER_SYSTEM_PROMPT,
            )

            logger.info("[LLM] Response received")
            actions = self._parse_actions(result)

            logger.info(
                "[LLM] Parsed actions: %s",
                ", ".join(a.name for a in actions) if actions else "none",
            )
            return actions

        except (GeminiError, GeminiInvalidResponseError, ValueError) as e:
            logger.warning(
                "[LLM] Falling back to RuleBasedDecisionEngine: %s",
                e,
            )
            return await self._fallback.decide(state)
        except Exception:
            logger.exception(
                "[LLM] Unexpected error in Gemini decision — falling back to RuleBasedDecisionEngine"
            )
            return await self._fallback.decide(state)

    def _parse_actions(self, result: dict[str, Any]) -> list[Action]:
        raw_actions = result.get("actions", [])
        if not isinstance(raw_actions, list):
            raise GeminiInvalidResponseError("Gemini 'actions' is not a list")

        actions: list[Action] = []
        seen: set[str] = set()

        for item in raw_actions:
            name = str(item).strip()
            if name not in _ACTION_CATALOG:
                logger.warning(
                    "[LLM] Ignoring unknown action from Gemini: %s", name,
                )
                continue
            if name in seen:
                continue
            seen.add(name)
            actions.append(_ACTION_CATALOG[name])

        actions.sort(key=lambda a: a.priority)

        if not actions:
            logger.info("[LLM] Gemini returned no actions for this business")

        return actions
