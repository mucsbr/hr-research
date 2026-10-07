"""QA reference source only. Execute solely in the registered remote sandbox."""
from html.parser import HTMLParser


def envelope(request, **result):
    return {"protocol": "adapter-program/1", "input_id": request["input_id"],
            "operation": request["operation"], **result}


def field(value, pointer):
    return {"value": str(value), "evidence": {"pointer": pointer}}


def parse_list(request):
    payload = next(document["content"] for document in request["documents"] if document["id"] == "json")
    rows = payload.get("zpData", {}).get("jobList", [])
    records = []
    for index, row in enumerate(rows):
        mapping = {"id": "encryptJobId", "title": "jobName", "company": "brandName",
                   "company_id": "encryptBrandId", "salary": "salaryDesc", "location": "cityName"}
        fields = {name: field(row[key], "/" + key) for name, key in mapping.items()
                  if row.get(key) is not None and str(row[key]).strip()}
        records.append({"row": {"document_id": "json", "pointer": f"/zpData/jobList/{index}"}, "fields": fields})
    return envelope(request, records=records, navigation=[])


class Region(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += 1
        elif "job-sec-text" in dict(attrs).get("class", "").split():
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.text.append(data.strip())


def parse_detail(request):
    html = next(document["content"] for document in request["documents"] if document["id"] == "dom")
    parser = Region()
    parser.feed(html)
    if not parser.text:
        raise ValueError("The registered old detail region is absent")
    return envelope(request, row={"document_id": "dom", "selector": ".job-sec-text"}, fields={
        "description": {"value": " ".join(parser.text), "evidence": {"selector": ""}}}, navigation=[])


def navigation(request):
    return envelope(request, navigation=[{"type": "scroll", "pixels": 500}, {"type": "wait", "milliseconds": 100}])


def run(operation, payload):
    if operation not in {"parse_list", "parse_detail", "navigation"} or type(payload) is not dict:
        raise ValueError("Unsupported operation or input")
    if payload.get("protocol") != "adapter-program/1" or payload.get("kind") != "boss" or payload.get("operation") != operation:
        raise ValueError("Input protocol, platform or operation does not match")
    return {"parse_list": parse_list, "parse_detail": parse_detail, "navigation": navigation}[operation](payload)
