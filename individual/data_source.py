import json

from django.db.models import Q

# Imports tag records with the source they came from (json_ext.source_type, e.g. by msr_etl) and a
# benefit plan enrolls only from its own source (json_ext.data_source), so programmes on one instance
# never share people. Untagged records and plans without a data_source belong to the default source.
DATA_SOURCE_FIELD = "source_type"
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
    return benefit_plan_json_ext(benefit_plan).get("data_source") or DEFAULT_DATA_SOURCE


def data_source_q(data_source):
    """Records of data_source; the default source also owns untagged records."""
    tagged = Q(**{f"json_ext__{DATA_SOURCE_FIELD}": data_source})
    if data_source == DEFAULT_DATA_SOURCE:
        return tagged | ~Q(json_ext__has_key=DATA_SOURCE_FIELD)
    return tagged


def data_source_group_code(data_source, group_code):
    """Households are matched by code on import, so a non-default source gets its own codes."""
    group_code = str(group_code)
    prefix = f"{data_source}:"
    if data_source == DEFAULT_DATA_SOURCE or group_code.startswith(prefix):
        return group_code
    return f"{prefix}{group_code}"
