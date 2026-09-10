import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import NoResultsError, ProviderError, ValidationError
from app.models.enriched_business import EnrichedBusiness
from app.providers.base import BaseProvider, Business, SearchQuery
from app.services.deduplicator import Deduplicator
from app.services.enrichment_service import EnrichmentService

logger = logging.getLogger(__name__)


class BusinessSearchService:
    def __init__(self, provider: BaseProvider) -> None:
        self.provider = provider
        self._enrichment = EnrichmentService()

    async def search(
        self,
        location: str,
        business_category: str,
        radius: int = 5000,
        keyword: str | None = None,
        min_rating: float | None = None,
        max_results: int | None = None,
        exclude_saved: bool = True,
        db: Session | None = None,
    ) -> dict[str, Any]:
        """Search businesses with optional deduplication against saved leads."""
        if not location or not location.strip():
            raise ValidationError("Location is required", field="location")
        if not business_category or not business_category.strip():
            raise ValidationError("Business category is required", field="business_category")

        query = SearchQuery(
            query=business_category.strip(),
            location=location.strip(),
            radius=radius,
            type=business_category.strip().lower(),
            keyword=keyword.strip() if keyword else None,
            min_rating=min_rating,
            max_results=max_results or 500,
        )

        try:
            logger.info(
                "Searching: %s near %s (radius=%dm, max=%d, exclude_saved=%s)",
                business_category, location, radius,
                max_results or 500, exclude_saved,
            )
            result = await self.provider.search(query)

            if not result.businesses:
                logger.info("No businesses found: %s near %s", business_category, location)
                return self._empty_result(location, business_category, radius)

            businesses = result.businesses
            total_from_api = len(businesses)
            skipped = 0

            if exclude_saved and db is not None:
                dedup = Deduplicator(db)
                businesses, skipped = dedup.filter(businesses)
                if not businesses:
                    logger.info(
                        "All %d results already known (dedup filtered): %s near %s",
                        total_from_api, business_category, location,
                    )
                    return self._empty_result(location, business_category, radius)

            return {
                "businesses": [b.to_dict() for b in businesses],
                "total_results": len(businesses),
                "new_results": len(businesses),
                "skipped": skipped,
                "total_from_api": total_from_api,
                "search_location": location,
                "search_category": business_category,
                "search_radius": radius,
                "next_page_token": result.next_page_token,
                "message": (
                    f"Found {total_from_api} total, {len(businesses)} new"
                    if skipped > 0
                    else f"Found {len(businesses)} businesses"
                ),
            }
        except NoResultsError:
            logger.info("No businesses found (provider): %s near %s", business_category, location)
            return self._empty_result(location, business_category, radius)
        except (ProviderError, ValidationError):
            raise
        except Exception as e:
            logger.error("Search failed: %s", e)
            raise ProviderError(
                message=f"Search failed: {e}",
                provider=self.provider.provider_name,
            ) from e

    async def search_by_query(
        self,
        query_text: str,
        location: str | None = None,
        radius: int = 5000,
        business_category: str | None = None,
        min_rating: float | None = None,
        max_results: int | None = None,
        exclude_saved: bool = True,
        db: Session | None = None,
    ) -> dict[str, Any]:
        """Free-text search with dedup support."""
        if not query_text or not query_text.strip():
            raise ValidationError("Query text is required", field="query_text")

        query = SearchQuery(
            query=query_text.strip(),
            location=location.strip() if location else None,
            radius=radius,
            type=business_category.strip().lower() if business_category else None,
            min_rating=min_rating,
            max_results=max_results or 500,
        )

        try:
            result = await self.provider.search(query)
            if not result.businesses:
                return self._empty_result(location or "auto", business_category or "all", radius)

            businesses = result.businesses
            total_from_api = len(businesses)
            skipped = 0

            if exclude_saved and db is not None:
                dedup = Deduplicator(db)
                businesses, skipped = dedup.filter(businesses)

            return {
                "businesses": [b.to_dict() for b in businesses],
                "total_results": len(businesses),
                "new_results": len(businesses),
                "skipped": skipped,
                "total_from_api": total_from_api,
                "search_location": location or "auto",
                "search_category": business_category or "all",
                "search_radius": radius,
                "next_page_token": result.next_page_token,
                "message": (
                    f"Found {total_from_api} total, {len(businesses)} new"
                    if skipped > 0
                    else f"Found {len(businesses)} businesses"
                ),
            }
        except NoResultsError:
            return self._empty_result(location or "auto", business_category or "all", radius)
        except (ProviderError, ValidationError):
            raise
        except Exception as e:
            logger.error("Text search failed: %s", e)
            raise ProviderError(
                message=f"Search failed: {e}",
                provider=self.provider.provider_name,
            ) from e

    async def get_business_details(self, place_id: str) -> dict:
        if not place_id or not place_id.strip():
            raise ValidationError("Place ID is required", field="place_id")
        business = await self.provider.get_details(place_id.strip())
        return business.to_dict()

    async def enrich_businesses(self, businesses: list[Business]) -> list[dict]:
        if not businesses:
            return []
        enriched = []
        for business in businesses:
            if not business.fsq_place_id:
                enriched.append(business.to_dict())
                continue
            try:
                detailed = await self.provider.get_details(business.fsq_place_id)
                enriched.append(detailed.to_dict())
            except Exception as e:
                logger.warning("Failed to enrich %s: %s", business.fsq_place_id, e)
                enriched.append(business.to_dict())
        return enriched

    async def health_check(self) -> bool:
        try:
            return await self.provider.health_check()
        except Exception:
            return False

    async def close(self) -> None:
        await self.provider.close()

    @staticmethod
    def _empty_result(location: str, category: str, radius: int) -> dict[str, Any]:
        return {
            "businesses": [],
            "total_results": 0,
            "new_results": 0,
            "skipped": 0,
            "total_from_api": 0,
            "search_location": location,
            "search_category": category,
            "search_radius": radius,
            "next_page_token": None,
            "message": "No businesses found.",
        }