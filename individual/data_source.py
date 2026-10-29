import json

from django.db.models import Q, TextField, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, NullIf

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
    tagged = Q(**{f"json_ext__{DATA_SOURCE_FIELD}": data_source})
    if data_source == DEFAULT_DATA_SOURCE:
        return (tagged | ~Q(json_ext__has_key=DATA_SOURCE_FIELD)
                | Q(**{f"json_ext__{DATA_SOURCE_FIELD}": None}) | Q(**{f"json_ext__{DATA_SOURCE_FIELD}": ""}))
    return tagged


def data_source_expression():
    return Coalesce(NullIf(KeyTextTransform(DATA_SOURCE_FIELD, "json_ext"), Value("")), Value(DEFAULT_DATA_SOURCE),
                    output_field=TextField())


def record_data_source(record):
    """Source of an individual or household: its json_ext tag, else the default source."""
    return (record.json_ext or {}).get(DATA_SOURCE_FIELD) or DEFAULT_DATA_SOURCE
