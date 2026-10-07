"""Record/check exact fixture state across recreation of this Compose project only."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid

import redis
from sqlalchemy import select

from app.browser_sessions import load_state, state_file
from app.config import get_settings
from app.db import session_factory
from app.models import Company


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["record", "check"])
    args = parser.parse_args()
    settings = get_settings()
    marker = Path(settings.storage_dir) / "deployment-persistence-baseline.json"
    browser_marker = Path(settings.storage_dir) / "deployment-browser-verification.json"
    browser = json.loads(browser_marker.read_text())
    account_id = uuid.UUID(browser["account_id"])
    state = load_state(account_id)
    assert any(cookie["name"] == "fixture-session" for cookie in state["cookies"])
    with session_factory()() as db:
        company_ids = sorted(str(value) for value in db.scalars(
            select(Company.id).where(Company.name.like("容器验证样本-%"))))
    assert company_ids, "No fixture company from verify_stack.py was found"
    cache = redis.Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    key = "deployment:volume-verification"
    if args.mode == "record":
        cache.set(key, uuid.uuid4().hex, ex=3600)
    result = {"company_ids": company_ids, "account_id": str(account_id),
              "encrypted_file_sha256": hashlib.sha256(state_file(account_id).read_bytes()).hexdigest(),
              "redis_marker": cache.get(key)}
    assert result["redis_marker"]
    if args.mode == "record":
        marker.write_text(json.dumps(result, indent=2))
    else:
        assert result == json.loads(marker.read_text()), "Persistent fixture state changed"
    print(json.dumps({"mode": args.mode, "postgres_company_count": len(company_ids),
                      "redis_value_preserved": True, "encrypted_session_decryptable": True,
                      "exact_state_matches": True}))


if __name__ == "__main__":
    main()
