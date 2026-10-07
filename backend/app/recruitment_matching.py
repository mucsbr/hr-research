"""Company-scoped evidence matching and reversible manual merges, without LLM calls."""
import hashlib
import re
import unicodedata
import uuid

from sqlalchemy import select

from app.collection import CollectionError
from app.models import AuditEvent, Company, Job, JobSourceRecord, SourceBinding
from app.recruitment_management_models import JobMatchProposal, JobMergeRecord
from app.serialization import row


def normalized(value):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", unicodedata.normalize("NFKC", value or "")).casefold()


def match_evidence(left, right, left_kind, right_kind):
    if left_kind == right_kind or left.job_id == right.job_id:
        return None
    a, b = left.data, right.data
    title_a, title_b = normalized(a.get("title")), normalized(b.get("title"))
    cities_a = sorted({normalized(x) for x in a.get("locations", []) if normalized(x)})
    cities_b = sorted({normalized(x) for x in b.get("locations", []) if normalized(x)})
    entity_a = normalized(left.recruiting_entity or a.get("recruiting_entity"))
    entity_b = normalized(right.recruiting_entity or b.get("recruiting_entity"))
    if not title_a or title_a != title_b or not cities_a or cities_a != cities_b or not entity_a or entity_a != entity_b:
        return None
    body_a = normalized(a.get("description", "") + " " + a.get("requirements", ""))
    body_b = normalized(b.get("description", "") + " " + b.get("requirements", ""))
    if min(len(body_a), len(body_b)) < 40:
        return None
    shingles_a = {body_a[index:index + 4] for index in range(len(body_a) - 3)}
    shingles_b = {body_b[index:index + 4] for index in range(len(body_b) - 3)}
    similarity = len(shingles_a & shingles_b) / len(shingles_a | shingles_b)
    if similarity < 0.9:
        return None
    return {"method": "title_city_entity_content_v1", "title": a.get("title"),
            "locations": a.get("locations"), "recruiting_entity": left.recruiting_entity or a.get("recruiting_entity"),
            "content_similarity": round(similarity, 5), "confidence": round(similarity * 100),
            "fingerprints": [hashlib.sha256(body_a.encode()).hexdigest(), hashlib.sha256(body_b.encode()).hexdigest()],
            "records": [{"id": str(record.id), "job_id": str(record.job_id), "hash": record.current_hash,
                         "url": record.url, "source_kind": kind}
                        for record, kind in [(left, left_kind), (right, right_kind)]],
            "limitation": "高相似度仅为候选，可能仍是独立编制；需人工确认后合并"}


def propose_matches(db, company_id, actor_id=None, limit=500):
    company = db.get(Company, company_id)
    if not company or company.archived:
        raise CollectionError("company_missing", "公司不存在或已归档")
    entries = db.execute(select(JobSourceRecord, SourceBinding.kind).join(
        SourceBinding, JobSourceRecord.binding_id == SourceBinding.id).join(
        Job, JobSourceRecord.job_id == Job.id).where(
        SourceBinding.company_id == company_id, Job.company_id == company_id,
        Job.status != "merged", JobSourceRecord.status == "observed"
    ).order_by(JobSourceRecord.id).limit(limit + 1)).all()
    truncated = len(entries) > limit
    entries = entries[:limit]
    groups = {}
    for record, kind in entries:
        key = (normalized(record.data.get("title")),
               tuple(sorted(normalized(city) for city in record.data.get("locations", []))),
               normalized(record.recruiting_entity or record.data.get("recruiting_entity")))
        groups.setdefault(key, []).append((record, kind))
    best = {}
    comparisons = 0
    for group in groups.values():
        for index, (left, left_kind) in enumerate(group):
            for right, right_kind in group[index + 1:]:
                comparisons += 1
                if comparisons > 3000:
                    truncated = True
                    break
                evidence = match_evidence(left, right, left_kind, right_kind)
                if evidence:
                    pair = tuple(sorted([left.job_id, right.job_id], key=str))
                    if pair not in best or best[pair]["confidence"] < evidence["confidence"]:
                        best[pair] = evidence
            if comparisons > 3000:
                break
        if comparisons > 3000:
            break
    proposals = []
    for (left_id, right_id), evidence in best.items():
        proposal = db.scalar(select(JobMatchProposal).where(
            JobMatchProposal.left_job_id == left_id, JobMatchProposal.right_job_id == right_id))
        if proposal and proposal.status != "pending":
            continue
        if proposal is None:
            proposal = JobMatchProposal(company_id=company_id, left_job_id=left_id, right_job_id=right_id)
            db.add(proposal)
        proposal.confidence, proposal.evidence = evidence["confidence"], evidence
        db.flush()
        proposals.append(proposal)
    db.add(AuditEvent(user_id=actor_id, action="recruitment.matches_proposed", target=str(company_id),
                      detail={"records_considered": len(entries), "candidates": len(proposals), "partial": truncated}))
    return {"candidates": [row(item) for item in proposals], "records_considered": len(entries),
            "comparisons": min(comparisons, 3000), "partial": truncated}


def _refresh_job(db, job):
    records = db.scalars(select(JobSourceRecord).where(JobSourceRecord.job_id == job.id)).all()
    if not records:
        job.status = "merged"
        return
    job.status = "observed" if any(record.status == "observed" for record in records) else "suspected_removed"
    latest = max(records, key=lambda item: item.last_seen_at)
    job.title = latest.data.get("title") or job.title
    job.locations = latest.data.get("locations") or job.locations


def merge_proposal(db, proposal_id, target_job_id, actor_id=None, reason=""):
    proposal = db.scalar(select(JobMatchProposal).where(JobMatchProposal.id == proposal_id).with_for_update())
    if not proposal or proposal.status != "pending":
        raise CollectionError("match_unavailable", "匹配候选不存在或已处理")
    job_ids = {proposal.left_job_id, proposal.right_job_id}
    if target_job_id not in job_ids:
        raise CollectionError("invalid_target", "合并目标必须是候选中的一个岗位")
    jobs = db.scalars(select(Job).where(Job.id.in_(job_ids)).order_by(Job.id).with_for_update()).all()
    if len(jobs) != 2 or any(job.company_id != proposal.company_id or job.status == "merged" for job in jobs):
        raise CollectionError("company_scope_mismatch", "只能合并同一关注公司下的有效岗位")
    active = db.scalars(select(JobMergeRecord).where(JobMergeRecord.company_id == proposal.company_id,
                                                   JobMergeRecord.status == "merged")).all()
    if any(str(job_id) in {str(merge.target_job_id), *merge.merged_job_ids} for merge in active for job_id in job_ids):
        raise CollectionError("merge_dependency", "岗位已有未撤销的合并，请先撤销再调整组合")
    records = db.scalars(select(JobSourceRecord).where(JobSourceRecord.job_id.in_(job_ids))
                         .order_by(JobSourceRecord.id).with_for_update()).all()
    by_id = {str(record.id): record for record in records}
    evidence_records = proposal.evidence.get("records", [])
    if len(evidence_records) != 2:
        raise CollectionError("match_stale", "匹配证据缺失，请重新生成候选")
    selected = []
    for proof in evidence_records:
        record = by_id.get(proof.get("id"))
        if not record or record.current_hash != proof.get("hash") or str(record.job_id) != proof.get("job_id"):
            raise CollectionError("match_stale", "岗位已发生变化，请重新生成匹配候选")
        binding = db.get(SourceBinding, record.binding_id)
        if not binding or binding.company_id != proposal.company_id:
            raise CollectionError("company_scope_mismatch", "来源主体不一致")
        selected.append((record, binding.kind))
    evidence = match_evidence(selected[0][0], selected[1][0], selected[0][1], selected[1][1])
    if not evidence:
        raise CollectionError("match_stale", "岗位不再符合标题、地点、主体和正文匹配条件")
    source_job_id = next(job_id for job_id in job_ids if job_id != target_job_id)
    moves = []
    for record in records:
        if record.job_id == source_job_id:
            moves.append({"record_id": str(record.id), "from_job_id": str(source_job_id),
                          "to_job_id": str(target_job_id)})
            record.job_id = target_job_id
    if not moves:
        raise CollectionError("match_stale", "来源记录已变更，请重新生成候选")
    merge = JobMergeRecord(company_id=proposal.company_id, target_job_id=target_job_id,
                           merged_job_ids=[str(source_job_id)], record_moves=moves,
                           evidence={**evidence, "reason": reason, "proposal_id": str(proposal.id)}, actor_id=actor_id)
    db.add(merge)
    proposal.status = "merged"
    db.flush()
    for job in jobs:
        _refresh_job(db, job)
    db.add(AuditEvent(user_id=actor_id, action="recruitment.jobs_merged", target=str(merge.id),
                      detail={"target_job_id": str(target_job_id), "moved_records": len(moves), "reason": reason}))
    return merge


def undo_merge(db, merge_id, actor_id=None):
    merge = db.scalar(select(JobMergeRecord).where(JobMergeRecord.id == merge_id).with_for_update())
    if not merge or merge.status != "merged":
        raise CollectionError("merge_unavailable", "合并不存在或已经撤销")
    affected = {merge.target_job_id, *(uuid.UUID(value) for value in merge.merged_job_ids)}
    jobs = db.scalars(select(Job).where(Job.id.in_(affected)).order_by(Job.id).with_for_update()).all()
    if len(jobs) != len(affected) or any(job.company_id != merge.company_id for job in jobs):
        raise CollectionError("company_scope_mismatch", "合并记录主体发生变化，未执行撤销")
    record_ids = [uuid.UUID(move["record_id"]) for move in merge.record_moves]
    records = db.scalars(select(JobSourceRecord).where(JobSourceRecord.id.in_(record_ids))
                         .order_by(JobSourceRecord.id).with_for_update()).all()
    if len(records) != len(record_ids) or any(record.job_id != merge.target_job_id for record in records):
        raise CollectionError("merge_dependency", "记录已有后续归属变化，未覆盖后续操作")
    by_id = {str(record.id): record for record in records}
    for move in merge.record_moves:
        by_id[move["record_id"]].job_id = uuid.UUID(move["from_job_id"])
    merge.status, merge.undone_by = "undone", actor_id
    proposal_id = merge.evidence.get("proposal_id")
    proposal = db.get(JobMatchProposal, uuid.UUID(proposal_id)) if proposal_id else None
    if proposal:
        proposal.status = "reverted"
    db.flush()
    for job in jobs:
        _refresh_job(db, job)
    db.add(AuditEvent(user_id=actor_id, action="recruitment.job_merge_undone", target=str(merge.id),
                      detail={"restored_records": len(records)}))
    return merge
