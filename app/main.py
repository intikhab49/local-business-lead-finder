import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import api_router
from app.core.config import settings
from app.core.exceptions import BusinessLeadFinderError
from app.core.logging import setup_logging
from app.db.database import get_engine, get_session_local, init_db
from app.dependencies import get_business_search_service, get_provider
from app.services.run_history_service import mark_interrupted_runs

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    logger.info("Environment: %s", settings.environment)

    init_db()
    logger.info("Database ready")

    session = get_session_local()()
    try:
        mark_interrupted_runs(session)
    except Exception:
        logger.warning("Could not close out orphaned runs during startup", exc_info=True)
    finally:
        session.close()

    # Non-blocking health check
    try:
        provider = get_provider()
        is_healthy = await provider.health_check()
        if is_healthy:
            logger.info("%s API health check passed", provider.provider_name)
        else:
            logger.warning("%s API health check failed", provider.provider_name)
    except Exception:
        logger.warning("Provider health check failed during startup")

    yield

    logger.info("Shutting down...")
    service = get_business_search_service()
    await service.close()
    get_engine().dispose()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Business Lead Finder API — v2 with pagination and auto-dedup",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=settings.cors_allow_methods,
        allow_headers=settings.cors_allow_headers,
    )

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.exception_handler(BusinessLeadFinderError)
    async def business_lead_finder_exception_handler(request, exc: BusinessLeadFinderError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.__class__.__name__,
                "message": exc.message,
                "status_code": exc.status_code,
                "details": exc.details,
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request, exc: Exception):
        logger.exception("Unhandled exception")
        return JSONResponse(
            status_code=500,
            content={
                "error": "InternalServerError",
                "message": "An unexpected error occurred",
                "status_code": 500,
            },
        )

    @app.get("/", include_in_schema=False)
    async def root():
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "docs": "/docs" if settings.is_development else "disabled in production",
        }

    @app.get("/health", tags=["health"])
    async def health_check():
        service = get_business_search_service()
        is_healthy = await service.provider.health_check()
        return {
            "status": "healthy" if is_healthy else "unhealthy",
            "service": settings.app_name,
            "version": settings.app_version,
            "provider": service.provider.provider_name,
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.is_development,
        log_level=settings.log_level.lower(),
    )