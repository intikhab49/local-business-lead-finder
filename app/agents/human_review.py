import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.agents.agent_memory import AgentMemory
from app.agents.base_agent import BaseAgent, track_state
from app.agents.context import AgentContext
from app.agents.message_bus import BaseMessageBus
from app.agents.messaging import AgentMessage, MessageType

logger = logging.getLogger(__name__)


class ReviewStatus(Enum):
    """Lifecycle status of a lead going through human review."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


@dataclass
class ReviewEntry:
    """A single recorded decision in a lead's review history."""

    status: str
    reviewer: str = ""
    timestamp: float = field(default_factory=time.time)
    reason: str = ""
    updated_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reviewer": self.reviewer,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "updated_fields": dict(self.updated_fields),
        }


@dataclass
class ReviewRequest:
    """Structured request asking the human review agent to act on a lead."""

    action: str = "get"
    fsq_place_id: str = ""
    reviewer: str = ""
    reason: str = ""
    updated_fields: dict[str, Any] = field(default_factory=dict)
    enriched: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewResponse:
    """Result of a review action, including the full review history."""

    fsq_place_id: str = ""
    status: str = ReviewStatus.PENDING.value
    reviewer: str = ""
    timestamp: float = 0.0
    reason: str = ""
    updated_fields: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    lead: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fsq_place_id": self.fsq_place_id,
            "status": self.status,
            "reviewer": self.reviewer,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "updated_fields": dict(self.updated_fields),
            "history": list(self.history),
            "lead": dict(self.lead),
            "success": self.success,
            "error": self.error,
        }


class HumanReviewAgent(BaseAgent):
    """Coordinates human-in-the-loop review of AI-generated leads.

    Qualifying leads are routed into a pending review state before they are
    finalized. A human reviewer can then approve, reject or edit each lead.
    Every decision is recorded in the shared agent memory as a per-business
    status plus a full, ordered review history.
    """

    name = "HumanReviewAgent"

    def __init__(
        self,
        memory: AgentMemory | None = None,
        context: AgentContext | None = None,
        bus: BaseMessageBus | None = None,
    ) -> None:
        super().__init__(context=context, bus=bus)
        self._memory = memory or AgentMemory()

    def set_memory(self, memory: AgentMemory) -> None:
        self._memory = memory

    def clear(self) -> None:
        self._memory.clear()
        logger.info("[HumanReview] Review store cleared")

    @track_state
    async def execute(self, request: ReviewRequest) -> ReviewResponse:
        action = request.action
        if action == "approve":
            return await self.approve(
                request.fsq_place_id, reviewer=request.reviewer, reason=request.reason
            )
        if action == "reject":
            return await self.reject(
                request.fsq_place_id, reviewer=request.reviewer, reason=request.reason
            )
        if action == "edit":
            return await self.edit(
                request.fsq_place_id,
                reviewer=request.reviewer,
                updated_fields=request.updated_fields,
            )
        if action == "submit":
            return await self.submit(
                request.fsq_place_id, reviewer=request.reviewer, enriched=request.enriched
            )
        if action == "get":
            return self.get_review(request.fsq_place_id)
        return ReviewResponse(
            fsq_place_id=request.fsq_place_id,
            success=False,
            error=f"unknown review action: {action}",
        )

    @track_state
    async def submit(
        self,
        fsq_place_id: str,
        reviewer: str = "",
        enriched: dict[str, Any] | None = None,
    ) -> ReviewResponse:
        if self._memory.has(fsq_place_id, "review_status"):
            logger.info(
                "[HumanReview] Already reviewed fsq_place_id=%s", fsq_place_id
            )
            return self.get_review(fsq_place_id)
        self._memory.set(fsq_place_id, "review_lead", dict(enriched or {}))
        self._record(fsq_place_id, ReviewStatus.PENDING, reviewer=reviewer or "system")
        logger.info(
            "[HumanReview] Pending fsq_place_id=%s reviewer=%s",
            fsq_place_id,
            reviewer or "system",
        )
        return self.get_review(fsq_place_id)

    @track_state
    async def approve(
        self,
        fsq_place_id: str,
        reviewer: str = "",
        reason: str = "",
    ) -> ReviewResponse:
        if not self._memory.has(fsq_place_id, "review_status"):
            return ReviewResponse(
                fsq_place_id=fsq_place_id,
                success=False,
                error=f"No review found for {fsq_place_id}",
            )
        self._record(fsq_place_id, ReviewStatus.APPROVED, reviewer=reviewer, reason=reason)
        logger.info(
            "[HumanReview] Approved fsq_place_id=%s reviewer=%s",
            fsq_place_id,
            reviewer or "system",
        )
        return self.get_review(fsq_place_id)

    @track_state
    async def reject(
        self,
        fsq_place_id: str,
        reviewer: str = "",
        reason: str = "",
    ) -> ReviewResponse:
        if not self._memory.has(fsq_place_id, "review_status"):
            return ReviewResponse(
                fsq_place_id=fsq_place_id,
                success=False,
                error=f"No review found for {fsq_place_id}",
            )
        self._record(fsq_place_id, ReviewStatus.REJECTED, reviewer=reviewer, reason=reason)
        logger.info(
            "[HumanReview] Rejected fsq_place_id=%s reviewer=%s reason=%s",
            fsq_place_id,
            reviewer or "system",
            reason,
        )
        return self.get_review(fsq_place_id)

    @track_state
    async def edit(
        self,
        fsq_place_id: str,
        reviewer: str = "",
        updated_fields: dict[str, Any] | None = None,
    ) -> ReviewResponse:
        if not self._memory.has(fsq_place_id, "review_status"):
            return ReviewResponse(
                fsq_place_id=fsq_place_id,
                success=False,
                error=f"No review found for {fsq_place_id}",
            )
        fields = dict(updated_fields or {})
        lead = dict(self._memory.get(fsq_place_id, "review_lead", {}) or {})
        lead.update(fields)
        self._memory.set(fsq_place_id, "review_lead", lead)
        self._record(fsq_place_id, ReviewStatus.EDITED, reviewer=reviewer, updated_fields=fields)
        logger.info(
            "[HumanReview] Edited fsq_place_id=%s reviewer=%s fields=%s",
            fsq_place_id,
            reviewer or "system",
            sorted(fields),
        )
        return self.get_review(fsq_place_id)

    def get_review(self, fsq_place_id: str) -> ReviewResponse:
        if not self._memory.has(fsq_place_id, "review_status"):
            return ReviewResponse(
                fsq_place_id=fsq_place_id,
                success=False,
                error=f"No review found for {fsq_place_id}",
            )
        history = list(self._memory.get(fsq_place_id, "review_history", []) or [])
        latest = history[-1] if history else {}
        return ReviewResponse(
            fsq_place_id=fsq_place_id,
            status=self._memory.get(
                fsq_place_id, "review_status", ReviewStatus.PENDING.value
            ),
            reviewer=latest.get("reviewer", ""),
            timestamp=latest.get("timestamp", 0.0),
            reason=latest.get("reason", ""),
            updated_fields=dict(latest.get("updated_fields", {}) or {}),
            history=history,
            lead=dict(self._memory.get(fsq_place_id, "review_lead", {}) or {}),
        )

    def list_pending(self) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        for place_id, _ in self._memory.items():
            if (
                self._memory.get(place_id, "review_status")
                == ReviewStatus.PENDING.value
            ):
                pending.append(self.get_review(place_id).to_dict())
        return pending

    def _record(
        self,
        fsq_place_id: str,
        status: ReviewStatus,
        reviewer: str = "",
        reason: str = "",
        updated_fields: dict[str, Any] | None = None,
    ) -> None:
        entry = ReviewEntry(
            status=status.value,
            reviewer=reviewer,
            reason=reason,
            updated_fields=updated_fields or {},
        )
        history = list(self._memory.get(fsq_place_id, "review_history", []) or [])
        history.append(entry.to_dict())
        self._memory.set(fsq_place_id, "review_history", history)
        self._memory.set(fsq_place_id, "review_status", status.value)

    @track_state
    async def handle_message(self, message: AgentMessage) -> AgentMessage | None:
        if message.message_type != MessageType.REQUEST:
            return await super().handle_message(message)

        payload = message.payload or {}
        request = ReviewRequest(
            action=payload.get("action", "get"),
            fsq_place_id=payload.get("fsq_place_id", ""),
            reviewer=payload.get("reviewer", ""),
            reason=payload.get("reason", ""),
            updated_fields=payload.get("updated_fields", {}),
            enriched=payload.get("enriched", {}),
        )
        response = await self.execute(request)
        return self._reply(
            message,
            response.to_dict(),
            success=response.success,
            error=response.error,
        )
