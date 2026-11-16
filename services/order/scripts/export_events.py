"""Write the JSON Schema of every order.* event to contracts/events/<event>.json."""

import json
from pathlib import Path

from order_service.events import PUBLISHED, json_schema

target_dir = Path(__file__).resolve().parents[3] / "contracts" / "events"
for event_type in PUBLISHED:
    path = target_dir / f"{event_type}.json"
    path.write_text(
        json.dumps(json_schema(event_type), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {path}")
