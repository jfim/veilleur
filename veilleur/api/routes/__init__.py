"""Aggregator for the REST API routers."""

from fastapi import APIRouter

from veilleur.api.auth import require_bearer_token
from veilleur.api.routes.feeds import router as feeds_router

api_router = APIRouter(dependencies=[])
api_router.include_router(feeds_router)


__all__ = ["api_router", "require_bearer_token"]
