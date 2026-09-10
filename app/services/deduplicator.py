import logging
from typing import Any

from sqlalchemy.orm import Session

from app.db import repository
from app.providers.base import Business

logger = logging.getLogger(__name__)


class Deduplicator:
    """DB-backed deduplication: filters known place_ids from search results."""

    def __init__(self, db: Session) -> None:
        self._known_place_ids = repository.get_all_known_place_ids(db)
        logger.info(
            "[Dedup] Loaded %d known place IDs (leads + exclusions)",
            len(self._known_place_ids),
        )

    def filter(self, businesses: list[Business]) -> tuple[list[Business], int]:
        """Return only new businesses not already in leads or exclusions.

        Returns (new_businesses, skipped_count).
        """
        if not businesses:
            return [], 0

        new: list[Business] = []
        skipped = 0
        for b in businesses:
            if b.fsq_place_id and b.fsq_place_id in self._known_place_ids:
                skipped += 1
            else:
                new.append(b)

        logger.info(
            "[Dedup] total=%d new=%d skipped=%d",
            len(businesses), len(new), skipped,
        )
        return new, skipped

    def mark_seen(self, fsq_place_id: str) -> None:
        """Track a place_id as seen (in-memory for this session)."""
        self._known_place_ids.add(fsq_place_id)

    def bulk_mark_seen(self, place_ids: list[str]) -> None:
        """Track many place_ids as seen in one call."""
        self._known_place_ids.update(place_ids)