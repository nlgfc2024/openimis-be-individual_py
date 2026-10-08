import json
import math

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import DataError
from django.db.models import (
    Case,
    CharField,
    DateField,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Subquery,
    Value,
    When,
)
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
WEIGHTED_SCORE_FIELD = "_weighted_score"
PRIMARY_WORKER_SOURCE = "PRIMARY_WORKER"


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


def _validate_model_path(model, path, extra_fields=None):
    if not isinstance(path, str) or not path:
        raise ValidationError("Ranking fields must be non-empty strings.")
    if path in (extra_fields or set()):
        return
    parts = path.split("__")
    current_model = model
    for index, part in enumerate(parts):
        try:
            field = current_model._meta.get_field(part)
        except (FieldDoesNotExist, AttributeError) as exc:
            raise ValidationError(f"Unsupported ranking field: {path}.") from exc
        if index == 0 and part == "json_ext" and len(parts) > 1:
            return
        if index < len(parts) - 1:
            current_model = field.related_model
            if current_model is None:
                raise ValidationError(f"Unsupported ranking field: {path}.")


def _normalise_order_item(item, model, index, extra_fields=None):
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

    _validate_model_path(model, result.get("field"), extra_fields)
    if result["direction"] not in SUPPORTED_DIRECTIONS:
        raise ValidationError(f"Unsupported ranking direction: {result['direction']}.")
    if result.get("cast") not in ({None} | set(SUPPORTED_CASTS)):
        raise ValidationError(f"Unsupported ranking cast: {result.get('cast')}.")
    if result.get("nulls") not in ({None} | SUPPORTED_NULLS):
        raise ValidationError(f"Unsupported ranking null placement: {result.get('nulls')}.")
    return result


def _is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _positive_number(value, path):
    if not _is_number(value) or value <= 0:
        raise ValidationError(f"{path} must be a positive number.")


def _validate_score_source(score_source, model):
    if score_source is None:
        return
    if not isinstance(score_source, dict):
        raise ValidationError("enrolment_ranking.score_source must be an object.")
    unexpected = set(score_source) - {"type", "data_field"}
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking.score_source keys: "
            + ", ".join(sorted(unexpected))
        )
    if score_source.get("type") != PRIMARY_WORKER_SOURCE:
        raise ValidationError(
            "enrolment_ranking.score_source.type must be PRIMARY_WORKER."
        )
    if model._meta.model_name != "group":
        raise ValidationError(
            "PRIMARY_WORKER score_source is only supported for GROUP enrollment."
        )
    if score_source.get("data_field") != "individual.json_ext":
        raise ValidationError(
            "PRIMARY_WORKER score_source.data_field must be individual.json_ext."
        )


def _validate_scoring(scoring, path):
    if not isinstance(scoring, dict):
        raise ValidationError(f"{path} must be an object.")
    scoring_type = scoring.get("type")
    if scoring_type == "mapping":
        unexpected = set(scoring) - {
            "type", "values", "default", "maximum_points"
        }
        if unexpected:
            raise ValidationError(
                f"Unsupported {path} keys: {', '.join(sorted(unexpected))}."
            )
        values = scoring.get("values")
        if not isinstance(values, dict) or not values:
            raise ValidationError(f"{path}.values must be a non-empty object.")
        if not all(
            isinstance(key, str) and _is_number(points)
            for key, points in values.items()
        ):
            raise ValidationError(
                f"{path}.values must map strings to numeric points."
            )
        if not _is_number(scoring.get("default", 0)):
            raise ValidationError(f"{path}.default must be numeric.")
        maximum_points = scoring.get(
            "maximum_points",
            max([scoring.get("default", 0), *values.values()]),
        )
        configured_points = [scoring.get("default", 0), *values.values()]
    elif scoring_type == "numeric_bands":
        unexpected = set(scoring) - {
            "type", "bands", "default", "maximum_points"
        }
        if unexpected:
            raise ValidationError(
                f"Unsupported {path} keys: {', '.join(sorted(unexpected))}."
            )
        bands = scoring.get("bands")
        if not isinstance(bands, list) or not bands:
            raise ValidationError(f"{path}.bands must be a non-empty list.")
        points = []
        for index, band in enumerate(bands):
            band_path = f"{path}.bands[{index}]"
            if not isinstance(band, dict):
                raise ValidationError(f"{band_path} must be an object.")
            unexpected_band = set(band) - {"min", "max", "points"}
            if unexpected_band:
                raise ValidationError(
                    f"Unsupported {band_path} keys: "
                    f"{', '.join(sorted(unexpected_band))}."
                )
            if "points" not in band or not _is_number(band["points"]):
                raise ValidationError(f"{band_path}.points must be numeric.")
            if "min" not in band and "max" not in band:
                raise ValidationError(
                    f"{band_path} must define min, max, or both."
                )
            for bound in ("min", "max"):
                if bound in band and not _is_number(band[bound]):
                    raise ValidationError(f"{band_path}.{bound} must be numeric.")
            if (
                "min" in band
                and "max" in band
                and band["min"] > band["max"]
            ):
                raise ValidationError(f"{band_path}.min cannot exceed max.")
            points.append(band["points"])
        if not _is_number(scoring.get("default", 0)):
            raise ValidationError(f"{path}.default must be numeric.")
        maximum_points = scoring.get(
            "maximum_points",
            max([scoring.get("default", 0), *points]),
        )
        configured_points = [scoring.get("default", 0), *points]
    else:
        raise ValidationError(
            f"{path}.type must be mapping or numeric_bands."
        )
    _positive_number(maximum_points, f"{path}.maximum_points")
    if any(points < 0 for points in configured_points):
        raise ValidationError(f"{path} points cannot be negative.")
    if maximum_points < max(configured_points):
        raise ValidationError(
            f"{path}.maximum_points cannot be smaller than configured points."
        )


def _validate_weighted_score(weighted_score):
    if not isinstance(weighted_score, dict):
        raise ValidationError("enrolment_ranking.weighted_score must be an object.")
    unexpected = set(weighted_score) - {"normalise_to", "components"}
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking.weighted_score keys: "
            + ", ".join(sorted(unexpected))
        )
    _positive_number(
        weighted_score.get("normalise_to", 100),
        "enrolment_ranking.weighted_score.normalise_to",
    )
    components = weighted_score.get("components")
    if not isinstance(components, list) or not components:
        raise ValidationError(
            "enrolment_ranking.weighted_score.components must be a non-empty list."
        )
    fields = set()
    for index, component in enumerate(components):
        path = f"enrolment_ranking.weighted_score.components[{index}]"
        if not isinstance(component, dict):
            raise ValidationError(f"{path} must be an object.")
        unexpected_component = set(component) - {"field", "weight", "scoring"}
        if unexpected_component:
            raise ValidationError(
                f"Unsupported {path} keys: "
                f"{', '.join(sorted(unexpected_component))}."
            )
        field = component.get("field")
        if not isinstance(field, str) or not field:
            raise ValidationError(f"{path}.field must be a non-empty string.")
        if field in fields:
            raise ValidationError(f"Duplicate weighted score field: {field}.")
        fields.add(field)
        _positive_number(component.get("weight"), f"{path}.weight")
        _validate_scoring(component.get("scoring"), f"{path}.scoring")


def validate_ranking_spec(ranking, model):
    unexpected = set(ranking) - {
        "order_by",
        "tie_breaker",
        "limit",
        "score_source",
        "weighted_score",
    }
    if unexpected:
        raise ValidationError(
            "Unsupported enrolment_ranking keys: " + ", ".join(sorted(unexpected))
        )
    weighted_score = ranking.get("weighted_score")
    score_source = ranking.get("score_source")
    if score_source is not None and weighted_score is None:
        raise ValidationError(
            "enrolment_ranking.score_source requires weighted_score."
        )
    _validate_score_source(score_source, model)
    if weighted_score is not None:
        _validate_weighted_score(weighted_score)

    order_by = ranking.get("order_by", [])
    if not isinstance(order_by, list):
        raise ValidationError("enrolment_ranking.order_by must be a list.")
    extra_fields = {WEIGHTED_SCORE_FIELD} if weighted_score is not None else set()
    normalised = [
        _normalise_order_item(item, model, index, extra_fields)
        for index, item in enumerate(order_by)
    ]
    if weighted_score is not None and not any(
        item["field"] == WEIGHTED_SCORE_FIELD for item in normalised
    ):
        raise ValidationError(
            "enrolment_ranking.order_by must include _weighted_score when "
            "weighted_score is configured."
        )

    tie_breaker = ranking.get("tie_breaker", "id")
    _validate_model_path(model, tie_breaker)
    normalised = [item for item in normalised if item["field"] != tie_breaker]
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


def _maximum_points(scoring):
    if scoring.get("maximum_points") is not None:
        return scoring["maximum_points"]
    if scoring["type"] == "mapping":
        points = list(scoring["values"].values())
    else:
        points = [band["points"] for band in scoring["bands"]]
    return max([scoring.get("default", 0), *points])


def _score_source_expression(field, score_source):
    if not score_source:
        return KeyTextTransform(field, "json_ext")

    from individual.models import GroupIndividual

    primary_worker = GroupIndividual.objects.filter(
        group_id=OuterRef("pk"),
        is_deleted=False,
        json_ext__primary_worker=True,
    ).order_by("id")
    value = KeyTextTransform(field, "individual__json_ext")
    return Subquery(
        primary_worker.annotate(_score_value=value).values("_score_value")[:1],
        output_field=CharField(),
    )


def build_weighted_score(queryset, weighted_score, score_source=None):
    """Annotate a normalized score in SQL without loading the pool into Python."""
    source_annotations = {}
    for index, component in enumerate(weighted_score["components"]):
        expression = _score_source_expression(component["field"], score_source)
        if component["scoring"]["type"] == "numeric_bands":
            expression = Cast(expression, output_field=FloatField())
        source_annotations[f"_enrolment_score_source_{index}"] = expression
    scored = queryset.annotate(**source_annotations)

    component_annotations = {}
    for index, component in enumerate(weighted_score["components"]):
        scoring = component["scoring"]
        source_alias = f"_enrolment_score_source_{index}"
        if scoring["type"] == "mapping":
            cases = [
                When(**{source_alias: value}, then=Value(float(points)))
                for value, points in scoring["values"].items()
            ]
        else:
            cases = []
            for band in scoring["bands"]:
                conditions = {}
                if "min" in band:
                    conditions[f"{source_alias}__gte"] = band["min"]
                if "max" in band:
                    conditions[f"{source_alias}__lte"] = band["max"]
                cases.append(
                    When(**conditions, then=Value(float(band["points"])))
                )
        component_annotations[f"_enrolment_score_component_{index}"] = Case(
            *cases,
            default=Value(float(scoring.get("default", 0))),
            output_field=FloatField(),
        )
    scored = scored.annotate(**component_annotations)

    weighted_total = Value(0.0, output_field=FloatField())
    total_weight = 0.0
    for index, component in enumerate(weighted_score["components"]):
        weight = float(component["weight"])
        total_weight += weight
        weighted_total += (
            F(f"_enrolment_score_component_{index}")
            * Value(weight / float(_maximum_points(component["scoring"])))
        )
    final_score = weighted_total * Value(
        float(weighted_score.get("normalise_to", 100)) / total_weight
    )
    return scored.annotate(
        **{
            WEIGHTED_SCORE_FIELD: ExpressionWrapper(
                final_score,
                output_field=FloatField(),
            )
        }
    )


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


def _cast_error(order_items, exc, weighted_score=None):
    casts = ", ".join(
        f"{item['field']} ({item['cast']})"
        for item in order_items
        if item.get("cast")
    )
    if weighted_score:
        weighted_fields = ", ".join(
            component["field"]
            for component in weighted_score.get("components", [])
            if component.get("scoring", {}).get("type") == "numeric_bands"
        )
        casts = ", ".join(filter(None, [casts, weighted_fields]))
    raise ValidationError(
        "Enrollment ranking could not cast stored values for "
        f"{casts}. Check that the data matches beneficiary_data_schema."
    ) from exc


def rank_and_cap_queryset(
    queryset,
    benefit_plan,
    status,
    current_enrolment_count,
    materialize_selected_ids=True,
):
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
            "percentage": None,
            "selected_ids": None,
        }

    order_items, percentage, respect_max = validate_ranking_spec(
        ranking, queryset.model
    )
    weighted_score = ranking.get("weighted_score")
    score_source = ranking.get("score_source")

    def apply_ranking(source_queryset):
        if weighted_score is not None:
            source_queryset = build_weighted_score(
                source_queryset,
                weighted_score,
                score_source,
            )
        return build_order_by(source_queryset, order_items)

    ranked = apply_ranking(queryset)
    cap, has_limit = calculate_cap(
        pool_size,
        percentage,
        benefit_plan.max_beneficiaries,
        current_enrolment_count,
        respect_max,
    )
    will_enrol = min(pool_size, cap)
    if materialize_selected_ids:
        try:
            ranked_ids = list(ranked.values_list("id", flat=True)[:will_enrol])
        except DataError as exc:
            _cast_error(order_items, exc, weighted_score)
        capped = apply_ranking(queryset.filter(id__in=ranked_ids))
    else:
        # Keep the LIMIT inside a subquery so the outer queryset remains open to
        # row-security, GraphQL filters, and client-requested ordering.  This avoids
        # materialising the complete cohort on every preview page without handing a
        # sliced queryset to the connection field.
        has_numeric_cast = any(item.get("cast") for item in order_items) or any(
            component.get("scoring", {}).get("type") == "numeric_bands"
            for component in (weighted_score or {}).get("components", [])
        )
        if has_numeric_cast and will_enrol:
            try:
                list(ranked.values_list("id", flat=True)[:1])
            except DataError as exc:
                _cast_error(order_items, exc, weighted_score)
        ranked_ids = None
        capped_ids = ranked.values("id")[:will_enrol]
        capped = apply_ranking(queryset.filter(id__in=capped_ids))
    return capped, {
        "pool_size": pool_size,
        "cap_applied": cap if has_limit else None,
        "will_enrol": will_enrol,
        "ranking": ranking,
        "percentage": percentage,
        "selected_ids": ranked_ids,
    }


# British-spelling compatibility for callers introduced during development.
load_enrolment_ranking = load_ranking_spec
validate_and_normalise_ranking = validate_ranking_spec
