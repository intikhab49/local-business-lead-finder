from fastapi import APIRouter, Depends, HTTPException, status

from app.core.exceptions import (
    BusinessLeadFinderError,
    ProviderError,
    ProviderNotFoundError,
    ValidationError,
)
from app.dependencies import get_business_search_service
from app.schemas.requests import (
    BusinessDetailResponse,
    BusinessSearchQueryRequest,
    BusinessSearchRequest,
    ErrorResponse,
    HealthResponse,
    SearchResponse,
)
from app.services.business_search import BusinessSearchService

router = APIRouter()


def map_business_to_response(business) -> BusinessDetailResponse:
    """Map domain business to API response."""
    from app.schemas.requests import BusinessAddress, BusinessHours, BusinessResponse

    address = BusinessAddress(
        formatted=business.formatted_address,
    )

    hours = BusinessHours(
        weekday_text=business.opening_hours if business.opening_hours else [],
    )

    google_maps_url = None
    if business.place_id:
        google_maps_url = f"https://www.google.com/maps/place/?q=place_id:{business.place_id}"

    business_response = BusinessResponse(
        place_id=business.place_id,
        name=business.name,
        address=address,
        phone=business.phone_number,
        website=business.website,
        rating=business.rating,
        user_ratings_total=business.user_ratings_total,
        google_maps_url=google_maps_url,
        types=business.types,
        price_level=business.price_level,
        business_status=business.business_status,
        hours=hours,
        photo_references=business.photo_references or [],
    )

    return BusinessDetailResponse(business=business_response)


def map_search_result_to_response(
    result,
    search_location: str,
    search_category: str,
    search_radius: int,
) -> SearchResponse:
    """Map search result to API response."""
    from app.schemas.requests import BusinessAddress, BusinessHours, BusinessResponse

    businesses = []
    for business in result.businesses:
        address = BusinessAddress(formatted=business.formatted_address)
        hours = BusinessHours(weekday_text=business.opening_hours or [])
        google_maps_url = None
        if business.place_id:
            google_maps_url = f"https://www.google.com/maps/place/?q=place_id:{business.place_id}"

        businesses.append(
            BusinessResponse(
                place_id=business.place_id,
                name=business.name,
                address=address,
                phone=business.phone_number,
                website=business.website,
                rating=business.rating,
                user_ratings_total=business.user_ratings_total,
                google_maps_url=google_maps_url,
                types=business.types,
                price_level=business.price_level,
                business_status=business.business_status,
                hours=hours,
                photo_references=business.photo_references or [],
            )
        )

    return SearchResponse(
        businesses=businesses,
        total_results=result.total_results,
        next_page_token=result.next_page_token,
        search_location=search_location,
        search_category=search_category,
        search_radius=search_radius,
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Check if the service is healthy and can connect to the provider",
)
async def health_check(
    service: BusinessSearchService = Depends(get_business_search_service),
) -> HealthResponse:
    """Health check endpoint."""
    from app.core.config import settings

    is_healthy = await service.health_check()

    return HealthResponse(
        status="healthy" if is_healthy else "unhealthy",
        service=settings.app_name,
        version=settings.app_version,
        provider=service.provider.provider_name,
    )


@router.post(
    "/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search businesses by location and category",
    description="Search for businesses by providing a location and business category. Returns a list of matching businesses with their details.",
    responses={
        200: {"model": SearchResponse, "description": "Successful search results"},
        400: {"model": ErrorResponse, "description": "Invalid request parameters"},
        404: {"model": ErrorResponse, "description": "No results found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        502: {"model": ErrorResponse, "description": "Provider error"},
        504: {"model": ErrorResponse, "description": "Provider timeout"},
    },
)
async def search_businesses(
    request: BusinessSearchRequest,
    service: BusinessSearchService = Depends(get_business_search_service),
) -> SearchResponse:
    """
    Search for businesses by location and category.

    - **location**: Location to search (e.g., "New York, NY", "San Francisco, CA", or "lat,lng")
    - **business_category**: Business category/type (e.g., "restaurant", "gym", "dentist")
    - **radius**: Search radius in meters (100-50000, default: 5000)
    - **keyword**: Additional keyword to filter results (optional)
    - **min_rating**: Minimum rating filter 0-5 (optional)
    - **max_results**: Maximum results to return (1-60, default: 20)
    """
    try:
        result = await service.search(
            location=request.location,
            business_category=request.business_category,
            radius=request.radius,
            keyword=request.keyword,
            min_rating=request.min_rating,
            max_results=request.max_results,
        )
        return map_search_result_to_response(
            result,
            search_location=request.location,
            search_category=request.business_category,
            search_radius=request.radius,
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="ValidationError",
                message=e.message,
                status_code=status.HTTP_400_BAD_REQUEST,
                details=e.details,
            ).model_dump(),
        ) from e
    except ProviderNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                error="NotFound",
                message=e.message,
                status_code=status.HTTP_404_NOT_FOUND,
                details=e.details,
            ).model_dump(),
        ) from e
    except ProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="ProviderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except BusinessLeadFinderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="BusinessLeadFinderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="InternalServerError",
                message="An unexpected error occurred",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ).model_dump(),
        ) from e


@router.post(
    "/search/query",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Search businesses by text query",
    description="Search for businesses using a text query with optional location bias.",
    responses={
        200: {"model": SearchResponse, "description": "Successful search results"},
        400: {"model": ErrorResponse, "description": "Invalid request parameters"},
        404: {"model": ErrorResponse, "description": "No results found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        502: {"model": ErrorResponse, "description": "Provider error"},
        504: {"model": ErrorResponse, "description": "Provider timeout"},
    },
)
async def search_businesses_by_query(
    request: BusinessSearchQueryRequest,
    service: BusinessSearchService = Depends(get_business_search_service),
) -> SearchResponse:
    """
    Search for businesses by text query.

    - **query**: Text search query (e.g., "best pizza near Times Square")
    - **location**: Optional location bias (e.g., "New York, NY")
    - **radius**: Search radius in meters (100-50000, default: 5000)
    - **business_category**: Optional category filter
    - **min_rating**: Minimum rating filter 0-5 (optional)
    - **max_results**: Maximum results to return (1-60, default: 20)
    """
    try:
        result = await service.search_by_query(
            query_text=request.query,
            location=request.location,
            radius=request.radius,
            business_category=request.business_category,
            min_rating=request.min_rating,
            max_results=request.max_results,
        )
        return map_search_result_to_response(
            result,
            search_location=request.location or "auto",
            search_category=request.business_category or "all",
            search_radius=request.radius,
        )
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="ValidationError",
                message=e.message,
                status_code=status.HTTP_400_BAD_REQUEST,
                details=e.details,
            ).model_dump(),
        ) from e
    except ProviderNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                error="NotFound",
                message=e.message,
                status_code=status.HTTP_404_NOT_FOUND,
                details=e.details,
            ).model_dump(),
        ) from e
    except ProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="ProviderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except BusinessLeadFinderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="BusinessLeadFinderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="InternalServerError",
                message="An unexpected error occurred",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ).model_dump(),
        ) from e


@router.get(
    "/business/{place_id}",
    response_model=BusinessDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get business details",
    description="Get detailed information for a specific business by place_id.",
    responses={
        200: {"model": BusinessDetailResponse, "description": "Business details"},
        400: {"model": ErrorResponse, "description": "Invalid place_id"},
        404: {"model": ErrorResponse, "description": "Business not found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        502: {"model": ErrorResponse, "description": "Provider error"},
        504: {"model": ErrorResponse, "description": "Provider timeout"},
    },
)
async def get_business_details(
    place_id: str,
    service: BusinessSearchService = Depends(get_business_search_service),
) -> BusinessDetailResponse:
    """
    Get detailed information for a specific business.

    - **place_id**: Google Places place_id
    """
    try:
        business = await service.get_business_details(place_id)
        return map_business_to_response(business)
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="ValidationError",
                message=e.message,
                status_code=status.HTTP_400_BAD_REQUEST,
                details=e.details,
            ).model_dump(),
        ) from e
    except ProviderNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorResponse(
                error="NotFound",
                message=e.message,
                status_code=status.HTTP_404_NOT_FOUND,
                details=e.details,
            ).model_dump(),
        ) from e
    except ProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="ProviderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except BusinessLeadFinderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="BusinessLeadFinderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="InternalServerError",
                message="An unexpected error occurred",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ).model_dump(),
        ) from e


@router.post(
    "/business/enrich",
    response_model=list[BusinessDetailResponse],
    status_code=status.HTTP_200_OK,
    summary="Enrich businesses with details",
    description="Enrich a list of businesses with detailed information.",
    responses={
        200: {"description": "Enriched businesses"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
)
async def enrich_businesses(
    place_ids: list[str],
    service: BusinessSearchService = Depends(get_business_search_service),
) -> list[BusinessDetailResponse]:
    """
    Enrich a list of businesses with detailed information.

    - **place_ids**: List of Google Places place_ids to enrich
    """
    try:
        # Create basic business objects from place_ids
        from app.providers.base import Business

        businesses = [Business(place_id=pid, name="", formatted_address="") for pid in place_ids]
        enriched = await service.enrich_businesses(businesses)
        return [map_business_to_response(b) for b in enriched]
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="ValidationError",
                message=e.message,
                status_code=status.HTTP_400_BAD_REQUEST,
                details=e.details,
            ).model_dump(),
        ) from e
    except BusinessLeadFinderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=ErrorResponse(
                error="BusinessLeadFinderError",
                message=e.message,
                status_code=e.status_code,
                details=e.details,
            ).model_dump(),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="InternalServerError",
                message="An unexpected error occurred",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ).model_dump(),
        ) from e
