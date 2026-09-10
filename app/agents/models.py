from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchAgentRequest:
    location: str
    business_category: str
    radius: int = 5000
    max_results: int = 20


@dataclass
class SearchAgentResponse:
    businesses: list[dict[str, Any]]
    total_results: int = 0
    success: bool = True
    error: str | None = None


@dataclass
class AgentRequest:
    action: str
    business: dict[str, Any] = field(default_factory=dict)
    website: str | None = None
    business_name: str = ""
    category: str = ""
    location: str = ""


@dataclass
class AgentResponse:
    success: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ValidationAgentRequest:
    business: dict[str, Any] = field(default_factory=dict)
    enriched: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationAgentResponse:
    valid: bool = True
    confidence_score: float = 0.0
    issues: list[str] = field(default_factory=list)
    feedback: dict[str, Any] = field(default_factory=dict)
