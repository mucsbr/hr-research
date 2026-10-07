import json
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.collection import CollectionError, JobDocument
from app.db import get_db
from app.models import (
    AuditEvent, Company, Job, JobObservation, JobSourceRecord, JobVersion, SourceAccount, SourceBinding,
    Subject, TaskRun, User,
)
from app.recruitment_discovery import bind_candidate, discover_entries
from app.recruitment_management_models import RecruitmentEntryCandidate
from app.recruitment_management_api import router
from app.recruitment_matching import merge_proposal, propose_matches, undo_merge
from app.security import current_user


@pytest.fixture
def manager(db):
    user = User(username="management-test", password_hash="not-used", role="admin")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client


def company(db, name="甲公司", website=None):
    subject = Subject(name=name)
    db.add(subject)
    db.flush()
    value = Company(subject_id=subject.id, name=name, website=website)
    db.add(value)
    db.flush()
    return value


class Fetcher:
    def __init__(self, callback):
        self.callback, self.requests = callback, []

    def fetch(self, url, **kwargs):
        self.requests.append(url)
        return self.callback(url)

    def close(self):
        pass


def test_bulk_names_preserve_same_name_entities_and_do_not_add_guessed_targets(manager, db):
    existing = company(db, "甲公司", "https://www.acme.example/")
    db.commit()
    response = manager.post("/api/v1/companies/bulk", json={"names": "甲公司\n乙公司\n乙公司\n"})
    assert response.status_code == 201
    assert response.json()["created"] == 1 and response.json()["review"] == 2
    assert response.json()["items"][0]["company_ids"] == [str(existing.id)]
    assert db.scalar(select(func.count()).select_from(Company)) == 2
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 0


def test_bulk_invalid_input_is_atomic(manager, db):
    response = manager.post("/api/v1/companies/bulk", json={"companies": [{"name": "甲公司"}, {"name": ""}]})
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Company)) == 0


def test_name_only_discovery_exposes_usable_search_configuration(db, monkeypatch):
    target = company(db)
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: Fetcher(
        lambda _: pytest.fail("no website and no search service: no network")))
    result = discover_entries(db, target)
    assert result["status"] == "search_configuration_required"
    assert result["configuration"]["method"] == "PUT"
    assert result["configuration"]["path"] == "/api/v1/settings/recruitment-discovery"


def test_official_discovery_only_reads_provided_home_and_requires_binding(manager, db, monkeypatch):
    target = company(db, website="https://www.acme.example/")
    db.commit()
    html = '<a href="https://careers.acme.example/jobs">加入我们</a>'
    html += '<a href="https://other.example/jobs">外部招聘广告</a><a href="/about">关于我们</a>'
    fetch = Fetcher(lambda url: (url, html))
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: fetch)
    response = manager.post(f"/api/v1/companies/{target.id}/entry-discovery", json={})
    assert response.status_code == 200
    candidates = response.json()["candidates"]
    assert len(candidates) == 1 and candidates[0]["status"] == "pending"
    assert fetch.requests == [target.website]
    assert candidates[0]["evidence"]["anchor_text"] == "加入我们"
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 0
    monkeypatch.setattr("app.recruitment_discovery.validate_fetch_url", lambda _: "93.184.216.34")
    bound = manager.post(f'/api/v1/entry-candidates/{candidates[0]["id"]}/bind')
    assert bound.status_code == 200 and bound.json()["candidate"]["status"] == "bound"
    again = manager.post(f'/api/v1/entry-candidates/{candidates[0]["id"]}/bind')
    assert again.json()["source"]["id"] == bound.json()["source"]["id"]
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 1


def test_search_discovery_queries_configured_service_without_fetching_candidates(manager, db, monkeypatch):
    target = company(db)
    db.commit()
    response = manager.put("/api/v1/settings/recruitment-discovery", json={
        "endpoint_url": "https://search.example/search", "enabled": True, "max_results": 2})
    assert response.status_code == 200
    def answer(url):
        params = parse_qs(urlparse(url).query)
        assert params["q"] == ['"甲公司" 招聘 官网'] and params["format"] == ["json"]
        return url, json.dumps({"results": [{"url": "https://careers.example/jobs", "title": "甲公司招聘", "content": "搜索摘要"}]})
    fetch = Fetcher(answer)
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: fetch)
    result = manager.post(f"/api/v1/companies/{target.id}/entry-discovery", json={}).json()
    assert result["status"] == "candidates_found"
    assert result["candidates"][0]["evidence"]["method"] == "search_result"
    assert len(fetch.requests) == 1 and urlparse(fetch.requests[0]).hostname == "search.example"
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 0


@pytest.mark.parametrize("url,kind,identifier", [
    ("https://www.zhipin.com/gongsi/acme~.html", "boss", "acme~"),
    ("https://www.liepin.com/company/12345/", "liepin", "12345"),
    ("https://www.zhaopin.com/companydetail/CZ123.htm", "zhaopin", "CZ123"),
    ("https://jobs.51job.com/all/co123.html", "51job", "123"),
])
def test_search_platform_company_candidate_binds_the_correct_collector(manager, db, monkeypatch, url, kind, identifier):
    target = company(db)
    candidate = RecruitmentEntryCandidate(company_id=target.id, url=url, title="公开搜索候选", evidence={})
    db.add(candidate)
    db.commit()
    monkeypatch.setattr("app.recruitment_discovery.validate_fetch_url", lambda _: "93.184.216.34")
    response = manager.post(f"/api/v1/entry-candidates/{candidate.id}/bind")
    assert response.status_code == 200
    assert response.json()["source"]["kind"] == kind
    assert response.json()["source"]["external_company_id"] == identifier
    assert response.json()["source"]["account_id"] is None
    assert response.json()["candidate"]["kind"] == kind


def test_platform_job_link_is_not_bound_as_a_generic_company_website(manager, db, monkeypatch):
    target = company(db)
    candidate = RecruitmentEntryCandidate(company_id=target.id, url="https://www.zhipin.com/job_detail/job1.html", title="单个岗位", evidence={})
    db.add(candidate)
    db.commit()
    monkeypatch.setattr("app.recruitment_discovery.validate_fetch_url", lambda _: pytest.fail("ambiguous entry must not start a fetch"))
    response = manager.post(f"/api/v1/entry-candidates/{candidate.id}/bind")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "identity_review"
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 0


def test_rejected_entry_is_not_reactivated_by_later_discovery(manager, db, monkeypatch):
    target = company(db, website="https://www.acme.example")
    db.commit()
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: Fetcher(
        lambda url: (url, '<a href="/jobs">招聘</a>')))
    first = manager.post(f"/api/v1/companies/{target.id}/entry-discovery", json={}).json()
    identifier = first["candidates"][0]["id"]
    assert manager.post(f"/api/v1/entry-candidates/{identifier}/reject").status_code == 200
    second = manager.post(f"/api/v1/companies/{target.id}/entry-discovery", json={}).json()
    assert second["candidates"][0]["status"] == "rejected"
    assert manager.post(f"/api/v1/entry-candidates/{identifier}/bind").status_code == 409


def test_official_metadata_candidates_keep_evidence_and_ignore_other_domains(db, monkeypatch):
    target = company(db, website="https://www.acme.example")
    html = '<script type="application/ld+json">' + json.dumps({"@graph": [
        {"@type": "Organization", "name": "甲公司", "sameAs": [
            "https://jobs.acme.example/careers", "https://other.example/careers"]}
    ]}) + '</script>'
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: Fetcher(lambda url: (url, html)))
    result = discover_entries(db, target)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["evidence"]["method"] == "official_metadata"
    assert result["candidates"][0]["url"] == "https://jobs.acme.example/careers"


def test_metadata_discovery_respects_persisted_candidate_budget(db, monkeypatch):
    target = company(db, website="https://www.acme.example")
    html = '<script type="application/ld+json">' + json.dumps({"@graph": [
        {"name": "招聘", "sameAs": [f"https://jobs.acme.example/jobs/{index}" for index in range(8)]}
    ]}) + '</script>'
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: Fetcher(lambda url: (url, html)))
    result = discover_entries(db, target, max_candidates=2)
    assert len(result["candidates"]) == 2
    assert db.scalar(select(func.count()).select_from(RecruitmentEntryCandidate)) == 2


def test_discovery_rejects_identity_changing_redirect(db, monkeypatch):
    target = company(db, website="https://www.acme.example")
    monkeypatch.setattr("app.recruitment_discovery.PublicFetcher", lambda: Fetcher(
        lambda url: ("https://other.example/", '<a href="/jobs">招聘</a>')))
    with pytest.raises(CollectionError) as exc:
        discover_entries(db, target)
    assert exc.value.code == "identity_review"


def test_binding_known_official_host_reuses_existing_adapter(db, monkeypatch):
    target = company(db)
    candidate = RecruitmentEntryCandidate(company_id=target.id,
                                           url="https://careers.tencent.com/zh-cn/home.html", title="加入我们")
    db.add(candidate)
    db.flush()
    monkeypatch.setattr("app.recruitment_discovery.validate_fetch_url", lambda _: "93.184.216.34")
    source = bind_candidate(db, candidate)
    assert source.adapter == "tencent"
    assert candidate.status == "bound"


def test_patch_source_checks_account_and_preserves_existing_task_snapshot(manager, db, monkeypatch):
    target = company(db)
    source = SourceBinding(company_id=target.id, kind="boss", entry_url="https://www.zhipin.com/gongsi/a.html")
    boss, other = SourceAccount(kind="boss", name="boss"), SourceAccount(kind="liepin", name="other")
    db.add_all([source, boss, other])
    db.flush()
    snapshot = {"sources_snapshot": [{"id": str(source.id), "entry_url": source.entry_url, "account_id": None}]}
    task = TaskRun(company_id=target.id, config_snapshot=snapshot, scope_hash="frozen")
    db.add(task)
    db.commit()
    path = f"/api/v1/source-bindings/{source.id}"
    assert manager.patch(path, json={"account_id": str(other.id)}).status_code == 422
    monkeypatch.setattr("app.recruitment_management_api.validate_fetch_url", lambda _: "93.184.216.34")
    response = manager.patch(path, json={"account_id": str(boss.id), "entry_url": "https://www.zhipin.com/gongsi/b.html", "enabled": False})
    assert response.status_code == 200 and response.json()["account_id"] == str(boss.id)
    db.refresh(task)
    assert task.config_snapshot == snapshot
    assert manager.patch(path, json={"entry_url": "https://www.liepin.com/company/123/"}).status_code == 422
    assert manager.patch(path, json={"config": {"cookie": "must-not-store"}}).status_code == 422
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "recruitment.source_updated")) == 1


def job_record(db, target, kind, title="平台研发工程师", locations=None, entity="甲公司", description=None):
    binding = SourceBinding(company_id=target.id, kind=kind, entry_url=f"https://{kind}.example/jobs")
    db.add(binding)
    db.flush()
    doc = JobDocument(source_job_id=str(binding.id), title=title, url=binding.entry_url + "/1",
                      locations=locations or ["上海"], recruiting_entity=entity,
                      description=description or "负责企业平台研发和稳定性建设，参与系统架构设计与性能优化，维护接口与数据流程，和产品团队协作完成迭代，具备扎实的软件工程基础。")
    job = Job(company_id=target.id, title=title, locations=doc.locations)
    db.add(job)
    db.flush()
    record = JobSourceRecord(job_id=job.id, binding_id=binding.id, source_job_id=doc.source_job_id,
                             url=doc.url, recruiting_entity=entity, current_hash=doc.content_hash(),
                             data=doc.model_dump(), baseline_scope="test")
    db.add(record)
    db.flush()
    return job, record


def test_cross_source_match_is_company_scoped_and_requires_full_evidence(db):
    a, b = company(db), company(db, "乙公司")
    job_record(db, a, "careers")
    job_record(db, a, "boss")
    job_record(db, b, "liepin")
    job_record(db, a, "liepin", locations=["北京"])
    job_record(db, a, "51job", entity="甲公司子公司")
    job_record(db, a, "zhaopin", description="负责研发")
    db.commit()
    result = propose_matches(db, a.id)
    assert len(result["candidates"]) == 1
    proof = result["candidates"][0]["evidence"]
    assert proof["content_similarity"] == 1 and proof["fingerprints"][0] == proof["fingerprints"][1]
    assert db.scalar(select(func.count()).select_from(JobSourceRecord)) == 6


def test_manual_merge_and_undo_preserve_source_data(manager, db):
    target = company(db)
    first, left = job_record(db, target, "careers")
    second, right = job_record(db, target, "boss")
    db.commit()
    result = manager.post(f"/api/v1/companies/{target.id}/job-matches").json()
    proposal_id = result["candidates"][0]["id"]
    merged = manager.post(f"/api/v1/job-matches/{proposal_id}/merge", json={
        "target_job_id": str(first.id), "reason": "核对原文后确认同一岗位"})
    assert merged.status_code == 200
    db.refresh(right)
    db.refresh(second)
    assert right.job_id == first.id and second.status == "merged"
    assert right.data["title"] == "平台研发工程师"
    undone = manager.post(f'/api/v1/job-merges/{merged.json()["id"]}/undo')
    assert undone.status_code == 200
    db.refresh(right)
    db.refresh(second)
    assert right.job_id == second.id and second.status == "observed"
    assert db.scalar(select(func.count()).select_from(JobSourceRecord)) == 2
    assert manager.post(f'/api/v1/job-merges/{merged.json()["id"]}/undo').status_code == 409


def test_changed_evidence_rejects_stale_merge(db):
    target = company(db)
    first, left = job_record(db, target, "careers")
    second, right = job_record(db, target, "boss")
    db.commit()
    result = propose_matches(db, target.id)
    proposal_id = result["candidates"][0]["id"]
    right.current_hash = "changed-after-proposal"
    db.commit()
    with pytest.raises(CollectionError) as exc:
        merge_proposal(db, proposal_id, first.id, reason="确认")
    assert exc.value.code == "match_stale"
    assert right.job_id == second.id


def test_undo_does_not_override_later_record_ownership(db):
    target = company(db)
    first, left = job_record(db, target, "careers")
    second, right = job_record(db, target, "boss")
    third, _ = job_record(db, target, "liepin", title="其他岗位")
    db.commit()
    result = propose_matches(db, target.id)
    merged = merge_proposal(db, result["candidates"][0]["id"], first.id, reason="人工确认")
    db.commit()
    right.job_id = third.id
    db.commit()
    with pytest.raises(CollectionError) as exc:
        undo_merge(db, merged.id)
    assert exc.value.code == "merge_dependency"
    assert right.job_id == third.id


def test_next_ingest_keeps_merged_ownership_and_undo_preserves_new_versions(db):
    from test_history import observe

    target = company(db)
    first, left = job_record(db, target, "careers")
    second, right = job_record(db, target, "boss")
    db.commit()
    proposal = propose_matches(db, target.id)["candidates"][0]
    merged = merge_proposal(db, proposal["id"], first.id, reason="核对原文")
    db.commit()
    source = db.get(SourceBinding, right.binding_id)
    updated = JobDocument.model_validate({**right.data, "salary_raw": "25-35K"})
    run, counts = observe(db, target, source, [updated], coverage="partial", scope="test")
    db.refresh(right)
    db.refresh(second)
    assert right.job_id == first.id and second.status == "merged"
    assert counts["salary_changed"] == 1 and counts["baseline_seen"] == 0
    assert db.scalar(select(func.count()).select_from(Job)) == 2
    assert db.scalar(select(func.count()).select_from(JobVersion).where(JobVersion.record_id == right.id)) == 1
    undo_merge(db, merged.id)
    db.commit()
    db.refresh(right)
    db.refresh(second)
    assert right.job_id == second.id and right.data["salary_raw"] == "25-35K"
    assert second.status == "observed"
    assert db.scalar(select(func.count()).select_from(JobObservation).where(JobObservation.record_id == right.id)) == 1
