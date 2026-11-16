"""Write the order service's OpenAPI spec to contracts/openapi/order.json."""

import json
from pathlib import Path

from order_service.main import app

target = Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "order.json"
target.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {target}")
