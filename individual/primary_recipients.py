"""Shared definition of the household's authoritative KYC recipient.

Household Phase enrolment selects the member marked as the verified primary
worker. KYC must use that identical member; ``recipient_type`` alone is a
separate editable membership attribute.
"""

from django.apps import apps
from django.db.models import Q

from individual.models import GroupIndividual


def _registry_fields():
    """Use the enrolment configuration, retaining safe import defaults."""
    if apps.is_installed("enrolment"):
        from enrolment.apps import EnrolmentConfig

        return (
            EnrolmentConfig.registry_primary_worker_field,
            EnrolmentConfig.registry_household_verification_field,
            EnrolmentConfig.registry_verified_household_value,
        )
    return "primary_worker", "validation_status", "VERIFIED"


def primary_worker_membership_filter(prefix=""):
    """Build a filter for active workers in verified households."""
    primary_worker_field, verification_field, verified_value = _registry_fields()
    separator = "__" if prefix else ""
    return Q(**{
        f"{prefix}{separator}is_deleted": False,
        f"{prefix}{separator}individual__is_deleted": False,
        f"{prefix}{separator}json_ext__{primary_worker_field}": True,
        f"{prefix}{separator}group__json_ext__{verification_field}": verified_value,
    })


def authoritative_primary_worker_memberships():
    """Memberships used by enrolment, KYC and payment workflows."""
    return GroupIndividual.objects.filter(primary_worker_membership_filter())
