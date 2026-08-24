import json
import math

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db.models import CharField, DateField, F, FloatField, IntegerField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast


SUPPORTED_CASTS = {
    "int": IntegerField,
    "float": FloatField,
    "date": DateField,
    "str": CharField,
}
SUPPORTED_DIRECTIONS = {"asc", "desc"}
SUPPORTED_NULLS = {"first", "last"}


def load_ranking_spec(benefit_plan, status):
    """Return the status-specific ranking configuration stored on a Phase."""
    json_ext = benefit_plan.json_ext or {}
    if isinstance(json_ext, str):
        try:
            json_ext = json.loads(json_ext)
        except (TypeError, ValueError) as exc:
            raise ValidationError("BenefitPlan json_ext must be valid JSON.") from exc
    if not isinstance(json_ext, dict):
        raise ValidationError("BenefitPlan json_ext must be an object.")

    rankings = json_ext.get("enrolment_ranking")
    if rankings is None:
        return None
    if not isinstance(rankings, dict):
        raise ValidationError("enrolment_ranking must be an object keyed by beneficiary status.")
    ranking = rankings.get(status, rankings.get("*"))
    if ranking is None:
        return None
    if not isinstance(ranking, dict):
        raise ValidationError(f"enrolment_ranking.{status} must be an object.")
    return ranking


def _validate_model_path(model, path):
    if not isinstance(path, str) or not path:
        raise ValidationError("Ranking fields must be non-empty strings.")
    parts = path.split("__")
    current_model = model
    for index, part in enumerate(parts):
        if index > 0 and parts[0] == "json_ext":
            return
        try:
            field = current_model._meta.get_field(part)
        except (FieldDoesNotExist, AttributeError) as exc:
            raise ValidationError(f"Unsupported ranking field: {path}.") from exc
        if index < len(parts) - 1:
            current_model = field.related_model
            if current_model is None:
                raise ValidationError(f"Unsupported ranking field: {path}.")


def _normalise_order_item(item, model, index):
    if isinstance(item, str):
        direction = "desc" if item.startswith("-") else "asc"
        field = item[1:] if item.startswith("-") else item
        result = {"field": field, "direction": direction}
    elif isinstance(item, dict):
        unexpected = set(item) - {"field", "direction", "cast", "nulls"}
        if unexpected:
            raise ValidationError(
                f"Unsupported enrolment_ranking.order_by[{index}] keys: "
                + ", ".join(sorted(unexpected))
            )
        result = dict(item)
        result.setdefault("direction", "asc")
    else:
        raise ValidationError(f"enrolment_ranking.order_by[{index}] must be a string or object.")

    _validate_model_path(model, result.get("field"))
    if result["direction"] not in SUPPORTED_DIRECTIONS:
        raise ValidationError(f"Unsupported ranking direction: {result['direction']}.")
    if result.get("cast") not in ({None} | set(SUPPORTED_CASTS)):
        raise ValidationError(f"Unsupported ranking cast: {result.get('cast')}.")
    if result.get("nulls") not in ({None} | SUPPORTED_NULLS):
        raise ValidationError(f"Unsupported ranking null placement: {result.get('nulls')}.")
    return result


def validate_ranking_spec(ranking, model):
    unexpected = set(ranking) - {"order_by", "tie_breaker", "limit"}
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking keys: " + ", ".join(sorted(unexpected))
        )
    order_by = ranking.get("order_by", [])
    if not isinstance(order_by, list):
        raise ValidationError("enrolment_ranking.order_by must be a list.")
    normalised = [_normalise_order_item(item, model, index) for index, item in enumerate(order_by)]

    tie_breaker = ranking.get("tie_breaker", "id")
    _validate_model_path(model, tie_breaker)
    if all(item["field"] != tie_breaker for item in normalised):
        normalised.append({"field": tie_breaker, "direction": "asc"})

    limit = ranking.get("limit", {})
    if limit is None:
        limit = {}
    if not isinstance(limit, dict):
        raise ValidationError("enrolment_ranking.limit must be an object.")
    unexpected_limit = set(limit) - {"percentage", "respect_max_beneficiaries"}
    if unexpected_limit:
        raise ValidationError(
            "Unsupported enrolment_ranking.limit keys: " + ", ".join(sorted(unexpected_limit))
        )
    percentage = limit.get("percentage")
    if percentage is not None and (
        isinstance(percentage, bool)
        or not isinstance(percentage, (int, float))
        or not 1 <= percentage <= 100
    ):
        raise ValidationError("enrolment_ranking.limit.percentage must be between 1 and 100.")
    respect_max = limit.get("respect_max_beneficiaries", True)
    if not isinstance(respect_max, bool):
        raise ValidationError("enrolment_ranking.limit.respect_max_beneficiaries must be boolean.")
    return normalised, percentage, respect_max


def calculate_cap(
    pool_size,
    percentage=None,
    max_beneficiaries=None,
    current_enrolment_count=0,
    respect_max_beneficiaries=True,
):
    limits = []
    if percentage is not None:
        limits.append(math.ceil(pool_size * percentage / 100))
    if respect_max_beneficiaries and max_beneficiaries is not None:
        limits.append(max(max_beneficiaries - current_enrolment_count, 0))
    return min(limits) if limits else pool_size, bool(limits)


def build_order_by(queryset, order_items):
    """Annotate ordering values so PostgreSQL DISTINCT queries remain valid."""
    annotations = {}
    ordering = []
    for index, item in enumerate(order_items):
        alias = f"_enrolment_rank_{index}"
        expression = F(item["field"])
        if item.get("cast"):
            if item["field"].startswith("json_ext__"):
                expression = KeyTextTransform.from_lookup(item["field"])
            expression = Cast(expression, output_field=SUPPORTED_CASTS[item["cast"]]())
        annotations[alias] = expression
        order_expression = F(alias)
        options = {}
        if item.get("nulls") == "first":
            options["nulls_first"] = True
        elif item.get("nulls") == "last":
            options["nulls_last"] = True
        ordering.append(
            order_expression.desc(**options)
            if item["direction"] == "desc"
            else order_expression.asc(**options)
        )
    return queryset.annotate(**annotations).order_by(*ordering)


def rank_and_cap_queryset(queryset, benefit_plan, status, current_enrolment_count):
    """Apply deterministic ordering and the configured intake ceiling.

    The returned metadata is shared by preview and execution. ``pool_size`` is the
    unassigned eligible pool, while ``will_enrol`` is the sliced queryset size.
    """
    ranking = load_ranking_spec(benefit_plan, status)
    pool_size = queryset.count()
    if ranking is None:
        return queryset, {
            "pool_size": pool_size,
            "cap_applied": None,
            "will_enrol": pool_size,
            "ranking": None,
        }

    order_items, percentage, respect_max = validate_ranking_spec(
        ranking, queryset.model
    )
    ranked = build_order_by(queryset, order_items)
    cap, has_limit = calculate_cap(
        pool_size,
        percentage,
        benefit_plan.max_beneficiaries,
        current_enrolment_count,
        respect_max,
    )
    will_enrol = min(pool_size, cap)
    return ranked[:will_enrol], {
        "pool_size": pool_size,
        "cap_applied": cap if has_limit else None,
        "will_enrol": will_enrol,
        "ranking": ranking,
    }


# British-spelling compatibility for callers introduced during development.
load_enrolment_ranking = load_ranking_spec
validate_and_normalise_ranking = validate_ranking_spec
