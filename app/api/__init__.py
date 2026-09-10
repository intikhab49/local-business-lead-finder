from fastapi import APIRouter

from app.api.routes import business, discovery, export, history, leads, review

api_router = APIRouter()
api_router.include_router(business.router)
api_router.include_router(discovery.router)
api_router.include_router(review.router)
api_router.include_router(leads.router)
api_router.include_router(export.router)
api_router.include_router(history.router)