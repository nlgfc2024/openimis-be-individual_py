import json

from django.db.models import Q

DATA_SOURCE_FIELD = "data_source"
DEFAULT_DATA_SOURCE = "ubr"


def benefit_plan_json_ext(benefit_plan):
    json_ext = benefit_plan.json_ext or {}
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError):
            json_ext = {}
    return json_ext


def benefit_plan_data_source(benefit_plan):
    return benefit_plan_json_ext(benefit_plan).get(DATA_SOURCE_FIELD) or DEFAULT_DATA_SOURCE


def data_source_q(data_source):
    """Records of data_source; the default source also owns untagged records."""
    tagged = Q(**{f"json_ext__{DATA_SOURCE_FIELD}": data_source})
    if data_source == DEFAULT_DATA_SOURCE:
        return tagged | ~Q(json_ext__has_key=DATA_SOURCE_FIELD)
    return tagged


def record_data_source(record):
    """Source of an individual or household: its json_ext tag, else the default source."""
    return (record.json_ext or {}).get(DATA_SOURCE_FIELD) or DEFAULT_DATA_SOURCE
