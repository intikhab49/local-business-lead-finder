from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchQuery:
    query: str
    location: str | None = None
    radius: int = 5000
    type: str | None = None
    keyword: str | None = None
    min_rating: float | None = None
    max_results: int | None = None


@dataclass
class Business:
    fsq_place_id: str
    name: str
    formatted_address: str
    phone_number: str | None = None
    website: str | None = None
    rating: float | None = None
    categories: list[str] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    opening_hours: list[str] = field(default_factory=list)
    price_level: int | None = None
    # Some providers (OSM) carry contact e-mail directly; scraping fills the rest.
    email: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fsq_place_id": self.fsq_place_id,
            "name": self.name,
            "formatted_address": self.formatted_address,
            "phone_number": self.phone_number,
            "website": self.website,
            "rating": self.rating,
            "categories": self.categories,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "opening_hours": self.opening_hours,
            "price_level": self.price_level,
            "email": self.email,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Business":
        return cls(
            fsq_place_id=data.get("fsq_place_id", ""),
            name=data.get("name", ""),
            formatted_address=data.get("formatted_address", ""),
            phone_number=data.get("phone_number"),
            website=data.get("website"),
            rating=data.get("rating"),
            categories=data.get("categories", []),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            opening_hours=data.get("opening_hours", []),
            price_level=data.get("price_level"),
            email=data.get("email"),
        )


@dataclass
class SearchResult:
    businesses: list[Business]
    total_results: int = 0
    next_page_token: str | None = None


class BaseProvider(ABC):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = 30,
        max_results: int = 20,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_results = max_results

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @abstractmethod
    async def search(self, query: SearchQuery) -> SearchResult:
        pass

    @abstractmethod
    async def get_details(self, place_id: str) -> Business:
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass