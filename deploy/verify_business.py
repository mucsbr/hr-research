"""End-to-end module smoke against an isolated deployment, with no external delivery/LLM.

All records are explicit fixtures. Private-destination tasks deliberately exercise
production denial before a network connection; social tasks use unlogged accounts.
"""
import argparse
from http.cookiejar import CookieJar
import json
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import uuid


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, data=None, expected=200):
        body = json.dumps(data).encode() if data is not None else None
        try:
            response = self.opener.open(Request(self.base + "/api/v1" + path, body,
                                               {"Content-Type": "application/json"}, method=method), timeout=15)
            code, raw = response.status, response.read()
        except HTTPError as exc:
            code, raw = exc.code, exc.read()
        assert code == expected, f"{method} {path}: expected {expected}, got {code}"
        return json.loads(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="deploy/server.env")
    parser.add_argument("--output", default="deploy/verification-results-business.json")
    parser.add_argument("--resume", help="Resume the same recorded fixture tasks after inspecting a failed assertion")
    args = parser.parse_args()
    values = dict(line.split("=", 1) for line in Path(args.env_file).read_text().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    base = f"http://127.0.0.1:{int(values.get('HTTP_PORT', '18080'))}"
    client = Client(base)
    protected = ["/sentiment/status", "/news/status", "/notification-channels",
                 "/adapter-maintenance/adapters", "/assistant/capabilities", "/users"]
    for path in protected:
        client.call("GET", path, expected=401)
    client.call("POST", "/auth/login", {"username": values.get("ADMIN_USERNAME", "admin"),
                                        "password": values["ADMIN_PASSWORD"]})
    for path in protected:
        client.call("GET", path)
    model = client.call("GET", "/system/status")["llm"]
    assert not model["configured"] and model["daily_request_limit"] == 0 and model["calls_today"] == 0
    assert client.call("GET", "/sentiment/status")["bettafish"]["status"] == "not_configured"
    if args.resume:
        state = json.loads(Path(args.resume).read_text())
        finish(client, base=base, output=args.output, **state)
        return
    suffix = uuid.uuid4().hex[:8]
    checks = ["new_modules_authentication", "bettafish_honestly_not_configured", "zero_model_budget"]

    companies = [client.call("POST", "/companies", {
        "name": f"容器完整验证-{suffix}-{index}", "role": "other",
        "notes": "隔离部署测试样本，不是用户竞品；不访问外部来源。"}, expected=201) for index in range(2)]
    metrics = client.call("GET", "/recruitment/metrics?company_id=" + companies[0]["id"])
    assert metrics["data_status"] == "unknown" and metrics["observed_jobs"] is None
    query = client.call("POST", "/assistant/messages", {"message": "查询岗位：" + companies[0]["name"]})
    assert query["type"] == "query_result" and query["model_calls"] == 0
    checks.append("recruitment_unknown_coverage_and_assistant_query")
    comparison = client.call("POST", "/recruitment/comparisons", {
        "company_ids": [company["id"] for company in companies], "days": 30}, expected=202)

    subject = client.call("POST", "/sentiment/subjects", {
        "name": "容器舆情样本-" + suffix, "type": "own_company"}, expected=201)
    imported = client.call("POST", "/sentiment/imports", {
        "subject_id": subject["id"], "note": "纯人工fixture，不代表真实事件", "items": [{
            "platform": "public_web", "url": "https://example.invalid/fixture/" + suffix,
            "title": subject["name"] + " 售后反馈", "content": subject["name"] + "的客户反馈退款处理慢。"}]}, expected=201)
    assert client.call("GET", "/sentiment/runs/" + imported["task_id"])["status"] == "succeeded"
    items = client.call("GET", "/sentiment/items?subject_id=" + subject["id"])
    assert items["total"] == 1
    item = client.call("PATCH", "/sentiment/items/" + items["items"][0]["id"], {
        "is_read": True, "is_favorite": True, "notes": "部署fixture已核对"})
    assert item["is_read"] and item["is_favorite"]
    checks.append("sentiment_import_evidence_and_curation")
    account = client.call("POST", "/source-accounts", {"kind": "xhs", "name": "未登录fixture-" + suffix}, expected=201)
    source = client.call("POST", "/sentiment/sources", {
        "subject_id": subject["id"], "kind": "xhs", "name": "未登录来源fixture",
        "account_id": account["id"], "entry_urls": []}, expected=201)
    sentiment = client.call("POST", "/sentiment/runs", {
        "subject_id": subject["id"], "source_ids": [source["id"]], "max_pages": 1}, expected=202)
    bridge = client.call("POST", "/sentiment/runs", {
        "subject_id": subject["id"], "mode": "bettafish"}, expected=202)

    news_source = client.call("POST", "/news/sources", {
        "name": "禁止访问目标fixture-" + suffix, "kind": "feed",
        "url": "https://127.0.0.1:9/do-not-connect.xml", "source_type": "other"}, expected=201)
    news = client.call("POST", "/news/runs", {
        "source_ids": [news_source["id"]], "max_articles": 1, "max_pages": 1}, expected=202)
    channel = client.call("POST", "/notification-channels", {
        "name": "禁止外发fixture-" + suffix, "kind": "webhook", "enabled": True,
        "url": "https://127.0.0.1:9/do-not-deliver", "secret": "fixture-not-a-real-secret"}, expected=201)
    assert "encrypted_config" not in channel
    notification = client.call("POST", "/notification-channels/" + channel["id"] + "/test", {}, expected=202)
    adapters = client.call("GET", "/adapter-maintenance/adapters")
    assert {entry["kind"] for entry in adapters} == {"boss", "liepin", "zhaopin", "51job"}
    assert all(not (entry.get("registry") or {}).get("check_enabled", False) for entry in adapters)
    maintenance = client.call("POST", "/adapter-maintenance/adapters/liepin/check", {}, expected=202)

    pending = {"comparison": ("/tasks/" + comparison["task_id"], "partial"),
               "sentiment": ("/sentiment/runs/" + sentiment["task_id"], "waiting_login"),
               "bettafish": ("/sentiment/runs/" + bridge["task_id"], "blocked_configuration"),
               "news": ("/news/runs/" + news["task_id"], "failed"),
               "maintenance": ("/adapter-maintenance/runs/" + maintenance["id"], "not_configured")}
    state = {"checks": checks, "pending": pending, "notification": notification, "channel": channel,
             "news_source": news_source, "subject": subject, "comparison": comparison, "suffix": suffix}
    Path(args.output + ".pending.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
    finish(client, base=base, output=args.output, **state)


def finish(client, *, base, output, checks, pending, notification, channel, news_source, subject, comparison, suffix):
    terminal = {}
    print(json.dumps({"queued_fixture_tasks": {name: path for name, (path, _) in pending.items()},
                      "notification_delivery_id": notification["delivery_id"]}), flush=True)
    deadline = time.monotonic() + 85
    while pending or "notification" not in terminal:
        for name, (path, expected) in list(pending.items()):
            result = client.call("GET", path)
            if result["status"] not in {"queued", "running"}:
                assert result["status"] == expected, (name, result["status"], result.get("error_code"))
                terminal[name] = {"status": result["status"], "error_code": result.get("error_code")}
                if name == "news":
                    assert result["sources"] and result["sources"][0]["error_code"] == "restricted_address"
                pending.pop(name)
        deliveries = client.call("GET", "/notification-deliveries")
        delivery = next((entry for entry in deliveries if entry["id"] == notification["delivery_id"]), None)
        if delivery and delivery["status"] not in {"queued", "sending", "retrying"}:
            assert delivery["status"] == "failed" and delivery["error_code"] == "restricted_address", delivery
            terminal["notification"] = {"status": delivery["status"], "error_code": delivery["error_code"]}
        if time.monotonic() > deadline:
            raise RuntimeError("Existing module tasks did not reach terminal state; inspect their IDs, do not recreate them")
        if pending or "notification" not in terminal:
            time.sleep(2)
    client.call("PATCH", "/notification-channels/" + channel["id"], {"enabled": False})
    client.call("PATCH", "/news/sources/" + news_source["id"], {"enabled": False})
    checks.extend(["analysis_queue_comparison", "sentiment_queue_waiting_login",
                   "bettafish_queue_configuration_block", "news_queue_ssrf_denial",
                   "notification_queue_ssrf_denial", "maintenance_queue_no_unknown_upstream"])

    # Separate low-privilege session, then revoke it; no credentials are printed or retained.
    username, password = "fixture_reader_" + suffix, secrets.token_urlsafe(24)
    user = client.call("POST", "/users", {"username": username, "password": password, "role": "user"}, expected=201)
    reader = Client(base)
    reader.call("POST", "/auth/login", {"username": username, "password": password})
    reader.call("GET", "/sentiment/status")
    reader.call("GET", "/news/status")
    reader.call("GET", "/adapter-maintenance/adapters", expected=403)
    reader.call("GET", "/users", expected=403)
    reader.call("POST", "/notification-channels", {"name": "denied", "kind": "email", "recipient": "nobody@example.invalid"}, expected=403)
    client.call("PATCH", "/users/" + user["id"], {"active": False})
    reader.call("GET", "/auth/me", expected=401)
    checks.append("ordinary_user_permissions_and_revocation")
    assert client.call("GET", "/system/status")["llm"]["calls_today"] == 0
    result = {"checks": checks, "terminal": terminal, "llm_calls": 0, "real_notifications": 0,
              "real_platform_logins": 0, "subject_id": subject["id"],
              "comparison_task_id": comparison["task_id"]}
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
