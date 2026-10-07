import uuid

from fastapi import APIRouter, Query
from pydantic import Field

from app.analytics_service import company_metrics, create_comparison
from app.schemas import Input
from app.security import CurrentUser, Db

router = APIRouter(prefix="/api/v1/recruitment", tags=["analytics"])


class ComparisonInput(Input):
    company_ids: list[uuid.UUID] = Field(min_length=2, max_length=20)
    days: int = Field(default=30, ge=1, le=365)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


@router.get("/metrics")
def metrics(company_id: uuid.UUID, db: Db, user: CurrentUser, days: int = Query(30, ge=1, le=365)):
    return company_metrics(db, company_id, days=days)


@router.post("/comparisons", status_code=202)
def compare(data: ComparisonInput, db: Db, user: CurrentUser):
    task, created = create_comparison(db, **data.model_dump())
    return {"task_id": str(task.id), "created": created, "status": task.status, "model_calls": 0}
