import json
from pathlib import Path


DIRECTORY = Path(__file__).resolve().parents[1] / "props"


def find(family, search):
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(DIRECTORY.glob("*.json"))]
    if family is not None:
        return [record for record in records if record["family"].casefold() == family.casefold()]
    terms = search.casefold().split()
    return [record for record in records
            if all(term in json.dumps(record, ensure_ascii=False).casefold() for term in terms)]
