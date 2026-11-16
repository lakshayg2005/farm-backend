import json
from pathlib import Path

import pytest

from order_service import events as ev
from order_service.main import app

ROOT = Path(__file__).resolve().parents[3]


def test_committed_openapi_spec_is_current():
    """contracts/openapi/order.json must match the app. Regenerate it with:

    uv run --package farm-order python services/order/scripts/export_openapi.py
    """
    spec = json.loads((ROOT / "contracts" / "openapi" / "order.json").read_text())

    assert spec == json.loads(json.dumps(app.openapi()))


@pytest.mark.parametrize("event_type", list(ev.PUBLISHED))
def test_committed_event_schema_is_current(event_type):
    """Regenerate with: uv run --package farm-order python services/order/scripts/export_events.py"""
    path = ROOT / "contracts" / "events" / f"{event_type}.json"

    assert json.loads(path.read_text()) == ev.json_schema(event_type)
