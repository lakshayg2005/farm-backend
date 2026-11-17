from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from quality_service.db import Inspection
from quality_service.schemas import InspectionReport
from quality_service import routes as quality_routes


def report(**overrides):
    values = {
        "grade": "A",
        "moisture_pct": 12.5,
        "pest_found": False,
        "weight_kg": 80,
        "result": "PASS",
    }
    values.update(overrides)
    return InspectionReport(**values)


def test_pass_report_accepts_quality_measurements():
    result = report()
    assert result.result == "PASS"
    assert result.grade == "A"


def test_failed_harvest_report_requires_resolution():
    with pytest.raises(ValidationError):
        report(result="FAIL")


def test_lower_grade_resolution_requires_accepted_grade():
    with pytest.raises(ValidationError):
        report(result="FAIL", resolution="ACCEPT_LOWER_GRADE")


def test_failed_harvest_can_be_reharvested():
    result = report(result="FAIL", resolution="REHARVEST")
    assert result.resolution == "REHARVEST"


def test_lower_grade_resolution_records_accepted_grade():
    result = report(result="FAIL", resolution="ACCEPT_LOWER_GRADE", accepted_grade="C")
    assert result.accepted_grade == "C"


def test_accepted_grade_must_be_lower_than_inspected_grade():
    with pytest.raises(ValidationError):
        report(result="FAIL", resolution="ACCEPT_LOWER_GRADE", accepted_grade="A")


def test_inspections_are_unique_per_subject_and_attempt():
    constraint = next(
        item for item in Inspection.__table__.constraints
        if item.name == "inspection_subject_attempt_unique"
    )
    assert tuple(column.name for column in constraint.columns) == (
        "subject_type", "subject_id", "attempt"
    )


@pytest.mark.asyncio
async def test_failed_order_inspection_is_published_for_order_refund_flow(monkeypatch):
    inspector_id = uuid4()
    inspection = SimpleNamespace(
        id=uuid4(),
        subject_type="ORDER",
        subject_id=uuid4(),
        attempt=1,
        inspector_id=inspector_id,
        status="PENDING",
        grade=None,
        moisture_pct=None,
        pest_found=None,
        weight_kg=None,
        result=None,
        resolution=None,
        media_ids=[],
        created_at=datetime.now(UTC),
        reported_at=None,
        location=None,
        report_note=None,
    )
    user = SimpleNamespace(id=str(inspector_id), has_any=lambda *_: False)
    session = AsyncMock()
    session.scalar.return_value = inspection
    session.get.return_value = None
    published = []

    monkeypatch.setattr(
        quality_routes,
        "add_event",
        lambda _session, _model, event: published.append(event),
    )
    body = InspectionReport(
        grade="B",
        moisture_pct=14,
        pest_found=True,
        weight_kg=75,
        result="FAIL",
        resolution="REHARVEST",
    )

    result = await quality_routes.submit_report(inspection.id, body, user, session)

    assert result.result == "FAIL"
    assert inspection.status == "REPORTED"
    assert len(published) == 1
    assert published[0].type == "quality.graded"
    assert published[0].payload["subjectType"] == "ORDER"
    assert published[0].payload["subjectId"] == str(inspection.subject_id)
    assert published[0].payload["result"] == "FAIL"
    session.commit.assert_awaited_once()
