"""One public-company search against this deployment; no LLM or candidate visits."""

import argparse
import hashlib
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

BASE = "http://recruitment-search:8080"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"executed": False, "query": '"金蝶" 招聘 官网', "company_queries": 1,
                          "endpoint": BASE + "/search", "llm_calls": 0}))
        return
    opener = build_opener(ProxyHandler({}))
    with opener.open(BASE + "/healthz", timeout=5) as response:
        assert response.status == 200
    query = '"金蝶" 招聘 官网'
    url = BASE + "/search?" + urlencode({"q": query, "format": "json", "language": "zh-CN"})
    started = time.monotonic()
    try:
        with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=15) as response:
            body = response.read(1_000_001)
            assert len(body) <= 1_000_000
            payload = json.loads(body)
            result = {"executed": True, "http_status": response.status, "elapsed_seconds": round(time.monotonic() - started, 3),
                      "response_sha256": hashlib.sha256(body).hexdigest(), "response": payload,
                      "company_queries": 1, "candidate_site_visits": 0, "llm_calls": 0,
                      "official_recruitment_candidate_present": any(
                          item.get("url", "").startswith("https://www.kingdee.com/job")
                          for item in payload.get("results", []) if isinstance(item, dict))}
            assert isinstance(payload.get("results"), list)
            result["status"] = "results" if payload["results"] else "engine_failure" if payload.get("unresponsive_engines") else "no_results"
    except HTTPError as error:
        result = {"executed": True, "http_status": error.code, "status": "http_failure",
                  "company_queries": 1, "candidate_site_visits": 0, "llm_calls": 0}
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "results":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
