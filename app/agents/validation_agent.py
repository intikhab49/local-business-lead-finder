import logging

from app.agents.base_agent import BaseAgent, track_state
from app.agents.context import AgentContext
from app.agents.decision_engine import DISCOVER_WEBSITE, SCRAPE_WEBSITE
from app.agents.message_bus import BaseMessageBus
from app.agents.messaging import AgentMessage, MessageType
from app.agents.models import ValidationAgentRequest, ValidationAgentResponse
from app.core.config import settings

logger = logging.getLogger(__name__)


class ValidationAgent(BaseAgent):
    """Validates enriched lead data.

    Owns the validation responsibility: checks an enriched lead for required
    fields and quality signals, returning a structured validation verdict. When
    data is missing or confidence is low, the agent sends event-driven feedback
    requests over the message bus asking the DiscoveryAgent to rediscover a
    website and/or the EnrichmentAgent to retry scraping.
    """

    name = "ValidationAgent"

    def __init__(
        self,
        context: AgentContext | None = None,
        bus: BaseMessageBus | None = None,
        feedback_timeout: float | None = None,
    ) -> None:
        super().__init__(context=context, bus=bus)
        self._feedback_timeout = feedback_timeout or settings.agent_feedback_timeout

    @track_state
    async def execute(
        self,
        request: ValidationAgentRequest,
    ) -> ValidationAgentResponse:
        business = request.business
        enriched = request.enriched

        issues: list[str] = []
        if not business.get("name"):
            issues.append("missing business name")
        if not business.get("formatted_address") and not enriched.get("address"):
            issues.append("missing address")

        if not enriched.get("email"):
            issues.append("missing email")
        if not enriched.get("phone") and not business.get("phone_number"):
            issues.append("missing phone")
        if not enriched.get("website"):
            issues.append("missing website")

        confidence = float(enriched.get("confidence_score", 0.0))
        if confidence < 0.3:
            issues.append("low confidence")

        valid = "missing business name" not in issues and "missing address" not in issues

        logger.info(
            "[ValidationAgent] Validated business=%s valid=%s confidence=%.2f issues=%s",
            business.get("name") or enriched.get("business_name") or "unknown",
            valid,
            confidence,
            "; ".join(issues) if issues else "none",
        )

        feedback = await self._request_feedback(business, enriched, issues)

        return ValidationAgentResponse(
            valid=valid,
            confidence_score=confidence,
            issues=issues,
            feedback=feedback,
        )

    async def _request_feedback(
        self,
        business: dict,
        enriched: dict,
        issues: list[str],
    ) -> dict:
        if self._bus is None or self._context is None:
            return {}

        business_name = business.get("name") or enriched.get("business_name") or "unknown"
        categories = business.get("categories") or []
        category = categories[0] if isinstance(categories, list) and categories else ""
        location = business.get("formatted_address") or enriched.get("address") or ""

        feedback: dict = {}

        if "missing website" in issues:
            discovery_feedback = await self._request_agent(
                recipient="DiscoveryAgent",
                action=DISCOVER_WEBSITE.name,
                business=business,
                website=None,
                business_name=business_name,
                category=category,
                location=location,
            )
            feedback.update(discovery_feedback)

        website = enriched.get("website") or business.get("website")
        if ("missing email" in issues or "low confidence" in issues) and website:
            enrichment_feedback = await self._request_agent(
                recipient="EnrichmentAgent",
                action=SCRAPE_WEBSITE.name,
                business=business,
                website=website,
                business_name=business_name,
                category=category,
                location=location,
            )
            feedback.update(enrichment_feedback)

        return feedback

    async def _request_agent(
        self,
        recipient: str,
        action: str,
        business: dict,
        website: str | None,
        business_name: str,
        category: str,
        location: str,
    ) -> dict:
        payload = {
            "action": action,
            "business": business,
            "website": website,
            "business_name": business_name,
            "category": category,
            "location": location,
        }

        logger.info(
            "[ValidationAgent] Requesting %s for %s via message bus",
            recipient,
            business_name,
        )

        response = await self.request_work(
            recipient=recipient,
            payload=payload,
            message_type=MessageType.REQUEST,
            timeout=self._feedback_timeout,
        )

        if response is None:
            logger.info("[ValidationAgent] No feedback from %s for %s", recipient, business_name)
            return {}

        result = response.payload or {}
        feedback = result.get("data", {}) if result.get("success") else {}
        if feedback:
            logger.info(
                "[ValidationAgent] Feedback from %s for %s: %s",
                recipient,
                business_name,
                feedback,
            )
        return feedback

    @track_state
    async def handle_message(self, message: AgentMessage) -> AgentMessage | None:
        if message.message_type != MessageType.REQUEST:
            return await super().handle_message(message)

        payload = message.payload or {}
        request = ValidationAgentRequest(
            business=payload.get("business", {}),
            enriched=payload.get("enriched", {}),
        )
        response = await self.execute(request)
        return self._reply(
            message,
            {
                "valid": response.valid,
                "confidence_score": response.confidence_score,
                "issues": response.issues,
                "feedback": response.feedback,
            },
            success=response.valid,
        )
