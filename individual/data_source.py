import json
import re

from django.db.models import Q, TextField, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, NullIf

DATA_SOURCE_FIELD = "data_source"
DEFAULT_DATA_SOURCE = "ubr"
KYC_DETAILS_WORKFLOW_NAME = "Python KYC Details"
KYC_PAYMENT_DETAILS_FIELDS = (
    "account_number",
    "account_name",
    "bank",
    "payment_provider_code",
)
SUPPORTED_KYC_PAYMENT_FIELDS = frozenset(KYC_PAYMENT_DETAILS_FIELDS)
KYC_PAYMENT_RECONCILIATION_FIELDS = {
    "gender": "Gender",
    **{
        field: field.replace("_", " ").title()
        for field in KYC_PAYMENT_DETAILS_FIELDS
        if field != "payment_provider_code"
    },
}
DEFAULT_KYC_PAYMENT_DETAILS_CONFIG = {
    "payment_fields": ["account_number", "account_name", "bank"],
    "optional_payment_fields": ["payment_provider_code"],
    "location_columns": ["district", "ta", "gvh", "village"],
    "context_columns": ["catchment", "gender", "phase_name"],
    "template_columns": [
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
        "payment_provider_code",
    ],
    "header_aliases": {
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
        "payment provider code": "payment_provider_code",
        "provider code": "payment_provider_code",
        "id": "ID",
    },
}


def get_kyc_payment_details_config():
    from individual.apps import IndividualConfig

    configured = IndividualConfig.kyc_payment_details or {}
    config = {**DEFAULT_KYC_PAYMENT_DETAILS_CONFIG, **configured}
    config["header_aliases"] = {
        **DEFAULT_KYC_PAYMENT_DETAILS_CONFIG["header_aliases"],
        **(configured.get("header_aliases") or {}),
    }
    return config


def payment_details_fields():
    return set(get_kyc_payment_details_config()["payment_fields"])


def optional_payment_details_fields():
    return set(get_kyc_payment_details_config().get("optional_payment_fields", []))


def all_payment_details_fields():
    return payment_details_fields() | optional_payment_details_fields()


def payment_details_location_columns():
    return set(get_kyc_payment_details_config()["location_columns"])


def payment_details_context_columns():
    config = get_kyc_payment_details_config()
    return payment_details_location_columns() | set(config["context_columns"])


def payment_details_template_columns():
    return list(get_kyc_payment_details_config()["template_columns"])


def payment_details_required_columns():
    return {
        "form_number",
        "nid",
        "first_name",
        "last_name",
        *payment_details_fields(),
    }


def payment_details_text_fields():
    return {
        "form_number",
        "nid",
        *all_payment_details_fields(),
    }


def has_payment_details_columns(columns):
    return bool(all_payment_details_fields().intersection(columns))


def _normalized_header_aliases():
    return {
        re.sub(r"[^a-z0-9]+", " ", str(alias).casefold()).strip(): target
        for alias, target in get_kyc_payment_details_config()["header_aliases"].items()
    }


def normalize_kyc_header(header):
    cleaned = str(header).lstrip("\ufeff").strip()
    normalized = re.sub(r"[^a-z0-9]+", " ", cleaned.casefold()).strip()
    aliases = _normalized_header_aliases()
    if normalized in aliases:
        return aliases[normalized]
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
