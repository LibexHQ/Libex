"""Books router."""

# Third party
from fastapi import APIRouter

router = APIRouter(prefix="/book", tags=["Books"])