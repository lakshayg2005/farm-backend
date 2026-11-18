"""Write the notification service's OpenAPI spec to contracts/openapi/notification.json."""

import json
from pathlib import Path

from notification_service.main import app

target = Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "notification.json"
target.write_text(
    json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
)
print(f"wrote {target}")
