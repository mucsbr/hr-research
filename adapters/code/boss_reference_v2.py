"""QA upgraded source; requires a hash-locked BeautifulSoup wheel in its sandbox."""
from bs4 import BeautifulSoup


def envelope(request, **result):
    return {"protocol": "adapter-program/1", "input_id": request["input_id"],
            "operation": request["operation"], **result}


def field(value, pointer, **slice):
    return {"value": str(value), "evidence": {"pointer": pointer, **slice}}


def parse_list(request):
    payload = next(document["content"] for document in request["documents"] if document["id"] == "json")
    data = payload.get("zpData", {})
    name = "vacanciesV2" if "vacanciesV2" in data else "jobList"
    records = []
    for index, row in enumerate(data.get(name, [])):
        if name == "vacanciesV2":
            position, company = row["position"], row["company"]
            raw = position["key"]
            prefix, separator, identifier = raw.partition(":")
            if not separator or prefix != "job" or not identifier:
                raise ValueError("The new composite job identifier is not recognized")
            fields = {"id": field(identifier, "/position/key", start=len(prefix) + 1, end=len(raw)),
                      "title": field(position["title"], "/position/title"),
                      "company": field(company["name"], "/company/name"),
                      "company_id": field(company["id"], "/company/id")}
        else:
            mapping = {"id": "encryptJobId", "title": "jobName", "company": "brandName",
                       "company_id": "encryptBrandId", "salary": "salaryDesc", "location": "cityName"}
            fields = {key: field(row[source], "/" + source) for key, source in mapping.items()
                      if row.get(source) is not None and str(row[source]).strip()}
        records.append({"row": {"document_id": "json", "pointer": f"/zpData/{name}/{index}"}, "fields": fields})
    return envelope(request, records=records, navigation=[])


def parse_detail(request):
    html = next(document["content"] for document in request["documents"] if document["id"] == "dom")
    soup = BeautifulSoup(html, "html.parser")
    selector = next((value for value in (".jd-v2", ".job-sec-text") if soup.select_one(value)), None)
    if not selector:
        raise ValueError("No supported original job description was found")
    text = soup.select_one(selector).get_text(" ", strip=True)
    return envelope(request, row={"document_id": "dom", "selector": selector}, fields={
        "description": {"value": text, "evidence": {"selector": ""}}}, navigation=[])


def navigation(request):
    next_link = next((link["id"] for link in request["links"] if link["purpose"] == "next"), None)
    action = {"type": "follow", "link_id": next_link} if next_link else {"type": "scroll", "pixels": 500}
    return envelope(request, navigation=[action, {"type": "wait", "milliseconds": 100}])


def run(operation, payload):
    if operation not in {"parse_list", "parse_detail", "navigation"} or type(payload) is not dict:
        raise ValueError("Unsupported operation or input")
    if payload.get("protocol") != "adapter-program/1" or payload.get("kind") != "boss" or payload.get("operation") != operation:
        raise ValueError("Input protocol, platform or operation does not match")
    return {"parse_list": parse_list, "parse_detail": parse_detail, "navigation": navigation}[operation](payload)
