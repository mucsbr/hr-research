"""Verify worker-to-API networking and token enforcement without opening any website."""
import json

import httpx

from app.config import get_settings


settings = get_settings()
payload = {"task_id": "00000000-0000-0000-0000-000000000000",
           "binding_id": "00000000-0000-0000-0000-000000000000", "filters": {}, "expected_attempt": 1}
url = settings.browser_service_url.rstrip("/") + "/api/internal/browser/collect"
with httpx.Client(timeout=5, trust_env=False) as client:
    assert client.post(url, json=payload).status_code == 403
    response = client.post(url, json=payload,
                           headers={"X-HR-Worker-Token": settings.app_secret.get_secret_value()})
    assert response.status_code == 404  # Authentication passed; fixture IDs do not exist.
print(json.dumps({"worker_api_network": True, "missing_token_denied": True,
                  "valid_token_accepted": True, "external_requests": 0}))
