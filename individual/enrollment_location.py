"""Reserved household enrollment filters over the actual location relationship."""
import re

from django.core.exceptions import ValidationError

from location.models import Location


DISTRICT_FIELD = "district_id"


def district_from_condition(condition):
    """Return a validated district, or None for an unrelated custom filter."""
    if not isinstance(condition, str):
        raise ValidationError("Malformed enrollment operator filter.")
    expression, separator, value = condition.partition("=")
    if expression.split("__", 1)[0] != DISTRICT_FIELD:
        return None
    if (
        not separator
        or expression != "district_id__exact__integer"
        or not re.fullmatch(r"[1-9][0-9]*", value)
        or len(value) > 10
        or int(value) > 2147483647
    ):
        raise ValidationError("District filter requires Equals and a positive integer location ID.")
    district = Location.objects.filter(
        id=int(value), type="R", *Location.filter_validity()
    ).first()
    if district is None:
        raise ValidationError("Select an active District (R).")
    return district
