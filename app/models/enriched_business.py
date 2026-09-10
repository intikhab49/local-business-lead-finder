from dataclasses import dataclass, field
from typing import Any

from app.providers.base import Business


@dataclass
class EnrichedBusiness:
    business_name: str
    address: str
    phone: str | None = None
    website: str | None = None
    email: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    twitter: str | None = None
    youtube: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence_score: float = 0.0
    source: str = "Google Places"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_business(cls, business: Business) -> "EnrichedBusiness":
        return cls(
            business_name=business.name,
            address=business.formatted_address,
            phone=business.phone_number,
            website=business.website,
            latitude=business.latitude,
            longitude=business.longitude,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_name": self.business_name,
            "address": self.address,
            "phone": self.phone,
            "website": self.website,
            "email": self.email,
            "linkedin": self.linkedin,
            "facebook": self.facebook,
            "instagram": self.instagram,
            "twitter": self.twitter,
            "youtube": self.youtube,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "confidence_score": self.confidence_score,
            "source": self.source,
        }

    def merge(self, **overrides: Any) -> "EnrichedBusiness":
        for key, value in overrides.items():
            if hasattr(self, key) and value is not None:
                setattr(self, key, value)
        return self
