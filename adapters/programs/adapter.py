"""Built-in read-only recruitment parser. Packaged and run only by the remote Runner."""
from bs4 import BeautifulSoup

from lib.rules import KIND, RULES


def envelope(payload, **result):
    return {"protocol": "adapter-program/1", "input_id": payload["input_id"], "operation": payload["operation"], **result}


def claim(value, **evidence):
    return {"value": str(value), "evidence": evidence}


def pointer(path):
    return "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)


def at(value, path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def document(payload, name):
    return next((entry["content"] for entry in payload["documents"] if entry["id"] == name), None)


def native_list(payload):
    source = document(payload, "json")
    selected, rows = None, []
    for path in RULES["list_paths"]:
        values = at(source, path)
        if isinstance(values, list) and (selected is None or values):
            selected, rows = path, values
            if values:
                break
    records = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        fields = {}
        for name, paths in RULES["fields"].items():
            for path in paths:
                value = at(row, path)
                if type(value) in {str, int, float} and str(value).strip():
                    fields[name] = claim(value, pointer=pointer(path))
                    break
        if {"id", "title"}.issubset(fields):
            record = {"row": {"document_id": "json", "pointer": pointer([*selected, index])}, "fields": fields}
            observed = next((link["id"] for link in payload["links"] if link.get("job_id") == fields["id"]["value"]), None)
            if observed:
                record["job_link_id"] = observed
            records.append(record)
    return envelope(payload, records=records[:100], navigation=[])


def postings(value, path=()):
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from postings(child, (*path, index))
    elif isinstance(value, dict):
        if value.get("_program_job_link_id"):
            yield path, value
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from postings(child, (*path, key))


def official_list(payload):
    records = []
    for path, row in postings(document(payload, "json")):
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            continue
        fields = {"title": claim(row["title"], pointer="/title")}
        organization = row.get("hiringOrganization")
        if isinstance(organization, dict) and isinstance(organization.get("name"), str):
            fields["company"] = claim(organization["name"], pointer="/hiringOrganization/name")
        elif isinstance(organization, str) and organization.strip():
            fields["company"] = claim(organization, pointer="/hiringOrganization")
        records.append({"row": {"document_id": "json", "pointer": pointer(path)}, "fields": fields,
                        "job_link_id": row["_program_job_link_id"]})
    if not records and payload.get("plan") and document(payload, "dom"):
        soup = BeautifulSoup(document(payload, "dom"), "html.parser")
        for node in soup.select(payload["plan"]["job_link_selector"]):
            ref = node.get("data-program-link-id")
            text = node.get_text(" ", strip=True)
            if not ref or not text:
                continue
            selector = 'a[data-program-link-id="' + ref + '"]'
            records.append({"row": {"document_id": "dom", "selector": selector},
                            "fields": {"title": claim(text, selector="")}, "job_link_id": ref})
    return envelope(payload, records=records[:100], navigation=[])


def parse_list(payload):
    if payload.get("board") == "tencent":
        data = document(payload, "json").get("Data", {})
        records = []
        mapping = {"id": "PostId", "title": "RecruitPostName", "company": "ComName", "location": "LocationName",
                   "department": "BGName", "function": "CategoryName", "experience": "RequireWorkYearsName"}
        for index, row in enumerate(data.get("Posts", [])):
            fields = {name: claim(row[key], pointer="/" + key) for name, key in mapping.items()
                      if type(row.get(key)) in {str, int, float} and str(row[key]).strip()}
            if {"id", "title"}.issubset(fields) and row.get("_program_job_link_id"):
                records.append({"row": {"document_id": "json", "pointer": f"/Data/Posts/{index}"},
                                "fields": fields, "job_link_id": row["_program_job_link_id"]})
        return envelope(payload, records=records[:100], navigation=[])
    return official_list(payload) if KIND == "official" else native_list(payload)


def parse_detail(payload):
    data = document(payload, "json")
    if payload.get("board") == "tencent":
        path = "/verified_details/0" if "verified_details" in data else "/Data"
        row = data["verified_details"][0] if "verified_details" in data else data["Data"]
        mapping = {"description": "Responsibility", "requirements": "Requirement", "title": "RecruitPostName",
                   "company": "ComName", "location": "LocationName", "department": "BGName", "function": "CategoryName",
                   "experience": "RequireWorkYearsName", "source_updated_at": "LastUpdateTime"}
        fields = {name: claim(row[key], pointer="/" + key) for name, key in mapping.items()
                  if isinstance(row.get(key), str) and row[key].strip()}
        return envelope(payload, row={"document_id": "json", "pointer": path}, fields=fields, navigation=[])
    if KIND == "zhaopin" and data and data.get("verified_details"):
        row = data["verified_details"][0]
        return envelope(payload, row={"document_id": "json", "pointer": "/verified_details/0"},
                        fields={"description": claim(row["jobDesc"], pointer="/jobDesc")}, navigation=[])
    if KIND == "official":
        current = payload.get("current_job_link_id")
        for path, row in postings(document(payload, "json")):
            if row.get("_program_job_link_id") == current and row.get("description"):
                fields = {"description": claim(row["description"], pointer="/description")}
                if isinstance(row.get("qualifications"), str) and row["qualifications"].strip():
                    fields["requirements"] = claim(row["qualifications"], pointer="/qualifications")
                return envelope(payload, row={"document_id": "json", "pointer": pointer(path)}, fields=fields, navigation=[])
        selectors = [payload.get("plan", {}).get("description_selector")]
    else:
        selectors = RULES["detail_selectors"]
    html = document(payload, "dom")
    if not html:
        raise ValueError("No observed detail document")
    soup = BeautifulSoup(html, "html.parser")
    for selector in selectors:
        if not selector:
            continue
        nodes = soup.select(selector, limit=2)
        if len(nodes) == 1 and nodes[0].get_text(" ", strip=True):
            return envelope(payload, row={"document_id": "dom", "selector": selector},
                            fields={"description": claim(nodes[0].get_text(" ", strip=True), selector="")}, navigation=[])
    raise ValueError("No unique verified job description region")


def navigation(payload):
    next_link = next((link["id"] for link in payload["links"] if link["purpose"] == "next"), None)
    action = {"type": "follow", "link_id": next_link} if next_link else {"type": "scroll", "pixels": 500}
    return envelope(payload, navigation=[action, {"type": "wait", "milliseconds": 100}])


def run(operation, payload):
    if (type(payload) is not dict or operation not in {"parse_list", "parse_detail", "navigation"}
            or payload.get("operation") != operation or payload.get("protocol") != "adapter-program/1"
            or payload.get("kind") != KIND):
        raise ValueError("Input protocol, operation or registered platform mismatch")
    return {"parse_list": parse_list, "parse_detail": parse_detail, "navigation": navigation}[operation](payload)
