from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from farm_common.auth import CurrentUser, Role, require_roles
from farm_common.errors import AppError
from farm_common.events import Event
from farm_common.outbox import add_event
from quality_service.database import get_session
from quality_service.db import Inspection, InspectorLocation, OutboxEvent
from quality_service.geo import point_to_db
from quality_service.schemas import InspectionOut, InspectionReport, InspectorLocationIn

router = APIRouter()
inspector_or_admin = require_roles(Role.INSPECTOR, Role.ADMIN)


def output(row: Inspection) -> InspectionOut:
    return InspectionOut(
        id=row.id, subject_type=row.subject_type, subject_id=row.subject_id, attempt=row.attempt,
        inspector_id=row.inspector_id, status=row.status, grade=row.grade,
        moisture_pct=row.moisture_pct, pest_found=row.pest_found, weight_kg=row.weight_kg,
        result=row.result, resolution=row.resolution, media_ids=row.media_ids or [],
        created_at=row.created_at, reported_at=row.reported_at,
    )


@router.put("/inspectors/me/location", tags=["inspectors"])
async def update_my_location(
    body: InspectorLocationIn,
    user: CurrentUser = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(InspectorLocation, UUID(user.id))
    if row is None:
        row = InspectorLocation(
            inspector_id=UUID(user.id), location=point_to_db(body.location),
            available=body.available,
        )
        session.add(row)
    else:
        row.location = point_to_db(body.location)
        row.available = body.available
        row.updated_at = datetime.now(UTC)
    await session.commit()
    return {"inspectorId": user.id, "available": row.available}


@router.get("/inspections", response_model=list[InspectionOut], tags=["inspections"])
async def list_inspections(
    user: CurrentUser = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
):
    query = select(Inspection).order_by(Inspection.created_at)
    if not user.has_any(Role.ADMIN):
        query = query.where(
            (Inspection.inspector_id == UUID(user.id)) | (Inspection.inspector_id.is_(None))
        )
    rows = (await session.execute(query.limit(100))).scalars().all()
    return [output(row) for row in rows]


@router.post("/inspections/{inspection_id}/claim", response_model=InspectionOut, tags=["inspections"])
async def claim_inspection(
    inspection_id: UUID,
    user: CurrentUser = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.scalar(
        select(Inspection).where(Inspection.id == inspection_id).with_for_update()
    )
    if row is None:
        raise AppError("INSPECTION_NOT_FOUND", "Inspection not found", 404)
    if row.status != "PENDING":
        raise AppError("INSPECTION_NOT_PENDING", "Inspection is no longer pending", 409)
    if row.inspector_id is not None and row.inspector_id != UUID(user.id) and not user.has_any(Role.ADMIN):
        raise AppError("INSPECTION_ASSIGNED", "Inspection is assigned to another inspector", 409)
    profile = await session.get(InspectorLocation, UUID(user.id), with_for_update=True)
    if row.inspector_id is None and profile is not None and not profile.available:
        raise AppError("INSPECTOR_BUSY", "This inspector is already assigned to another inspection", 409)
    row.inspector_id = UUID(user.id)
    if profile is not None:
        profile.available = False
    await session.commit()
    await session.refresh(row)
    return output(row)


@router.post("/inspections/{inspection_id}/report", response_model=InspectionOut, tags=["inspections"])
async def submit_report(
    inspection_id: UUID,
    body: InspectionReport,
    user: CurrentUser = Depends(inspector_or_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.scalar(
        select(Inspection).where(Inspection.id == inspection_id).with_for_update()
    )
    if row is None:
        raise AppError("INSPECTION_NOT_FOUND", "Inspection not found", 404)
    if row.status != "PENDING":
        raise AppError("INSPECTION_ALREADY_REPORTED", "Inspection has already been reported", 409)
    if row.inspector_id is not None and row.inspector_id != UUID(user.id) and not user.has_any(Role.ADMIN):
        raise AppError("INSPECTION_ASSIGNED", "Inspection is assigned to another inspector", 403)
    if row.inspector_id is None:
        row.inspector_id = UUID(user.id)
    assigned_profile = await session.get(InspectorLocation, row.inspector_id, with_for_update=True)
    if assigned_profile is not None:
        assigned_profile.available = True

    grade = body.accepted_grade if body.resolution == "ACCEPT_LOWER_GRADE" else body.grade
    row.grade = grade
    row.moisture_pct = body.moisture_pct
    row.pest_found = body.pest_found
    row.weight_kg = body.weight_kg
    row.result = body.result
    row.resolution = body.resolution
    row.media_ids = [str(item) for item in body.media_ids]
    row.location = point_to_db(body.location) if body.location else row.location
    row.report_note = body.note
    row.status = "REPORTED"
    row.reported_at = datetime.now(UTC)
    add_event(
        session,
        OutboxEvent,
        Event(
            type="quality.graded",
            producer="quality",
            payload={
                "inspectionId": str(row.id),
                "subjectType": row.subject_type,
                "subjectId": str(row.subject_id),
                "attempt": row.attempt,
                "grade": grade,
                "result": body.result,
                "resolution": body.resolution,
                "weightKg": body.weight_kg,
                "moisturePct": body.moisture_pct,
                "pestFound": body.pest_found,
                "mediaIds": [str(item) for item in body.media_ids],
            },
        ),
    )
    await session.commit()
    await session.refresh(row)
    return output(row)
