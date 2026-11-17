import json
import re

from django.db.models import Q, TextField, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, NullIf

DATA_SOURCE_FIELD = "data_source"
DEFAULT_DATA_SOURCE = "ubr"
KYC_DETAILS_WORKFLOW_NAME = "Python KYC Details"
PAYMENT_DETAILS_FIELDS = {"account_number", "account_name", "bank"}
PAYMENT_DETAILS_CONTEXT_COLUMNS = {
    "district", "ta", "gvh", "village", "catchment", "gender", "phase_name",
}
PAYMENT_DETAILS_TEMPLATE_COLUMNS = [
    "form_number",
    "district",
    "ta",
    "gvh",
    "village",
    "catchment",
    "first_name",
    "last_name",
    "nid",
    "account_number",
    "account_name",
    "bank",
]
PAYMENT_DETAILS_REQUIRED_COLUMNS = {
    "form_number",
    "nid",
    "first_name",
    "last_name",
    *PAYMENT_DETAILS_FIELDS,
}
PAYMENT_DETAILS_TEXT_FIELDS = {
    "form_number",
    "nid",
    "account_number",
    "account_name",
    "bank",
}

KYC_HEADER_ALIASES = {
    "district": "district",
    "district name": "district",
    "ubr household form number": "form_number",
    "form number": "form_number",
    "traditional authority": "ta",
    "traditional authority name": "ta",
    "group village head": "gvh",
    "group village head name": "gvh",
    "village": "village",
    "village name": "village",
    "catchment": "catchment",
    "catchment name": "catchment",
    "first name": "first_name",
    "last name": "last_name",
    "full name": "full_name",
    "gender": "gender",
    "nid": "nid",
    "national id": "nid",
    "national identification number": "nid",
    "date of birth": "dob",
    "account number": "account_number",
    "account name": "account_name",
    "bank provider": "bank",
    "bank": "bank",
    "id": "ID",
}


def has_payment_details_columns(columns):
    return bool(PAYMENT_DETAILS_FIELDS.intersection(columns))


def normalize_kyc_header(header):
    cleaned = str(header).lstrip("\ufeff").strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.casefold()).strip()
    if normalized in KYC_HEADER_ALIASES:
        return KYC_HEADER_ALIASES[normalized]
    return cleaned


def normalize_kyc_dataframe_headers(dataframe):
    normalized_columns = [normalize_kyc_header(column) for column in dataframe.columns]
    duplicates = sorted({
        column for column in normalized_columns if normalized_columns.count(column) > 1
    })
    if duplicates:
        raise ValueError(
            "KYC upload contains duplicate columns after header normalization: "
            + ", ".join(duplicates)
        )
    dataframe = dataframe.copy()
    dataframe.columns = normalized_columns
    # The household KYC export identifies the selected primary recipient in one
    # `full_name` column. The KYC workflow stores and validates names separately,
    # so derive those values before validating the upload. Explicit first/last
    # name columns, when supplied, remain authoritative.
    if "full_name" in dataframe.columns:
        full_names = dataframe.pop("full_name").fillna("").astype(str).str.strip()
        name_parts = full_names.str.split()
        if "first_name" not in dataframe.columns:
            dataframe["first_name"] = name_parts.str[0].fillna("")
        if "last_name" not in dataframe.columns:
            dataframe["last_name"] = name_parts.str[1:].str.join(" ").fillna("")
    return dataframe


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
