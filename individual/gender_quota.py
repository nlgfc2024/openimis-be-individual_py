"""Gender quotas applied after eligibility, using the phase's remaining places."""
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Q
from individual.enrolment_ranking import load_ranking_spec, validate_ranking_spec, build_order_by, calculate_cap


def quota_counts(target, female_percentage):
    # Largest remainder; a half-place tie goes to female-headed households.
    female = (target * female_percentage + 50) // 100
    return female, target - female


def apply_gender_quota(query, plan, status, current_count, criteria, materialize=True):
    from individual.models import GroupIndividual
    if plan.max_beneficiaries is None:
        raise ValidationError("Set the phase maximum beneficiaries before using Both genders.")
    female = criteria.get("female_percentage")
    male = criteria.get("male_percentage")
    if (type(female) is not int or type(male) is not int
            or not 0 <= female <= 100 or not 0 <= male <= 100 or female + male != 100):
        raise ValidationError("Female and male percentages must be whole numbers from 0 to 100 totaling 100.")
    heads = GroupIndividual.objects.filter(
        group_id=OuterRef("pk"), role="HEAD", is_deleted=False, individual__is_deleted=False,
    )
    def gender(values):
        condition = Q()
        for value in values:
            condition |= Q(individual__json_ext__gender__iexact=value)
        return heads.filter(condition)
    query = query.annotate(
        _quota_female=Exists(gender(("F", "FEMALE"))),
        _quota_male=Exists(gender(("M", "MALE"))),
    )
    if query.filter(_quota_female=True, _quota_male=True).exists():
        raise ValidationError("A household has conflicting male and female head records. Correct its head before applying quotas.")
    pool_size = query.count()
    ranking = load_ranking_spec(plan, status)
    order, percentage, _ = validate_ranking_spec(ranking or {}, query.model)
    cap, _ = calculate_cap(pool_size, percentage, plan.max_beneficiaries, current_count, True)
    female_cap, male_cap = quota_counts(cap, female)
    female_ids = build_order_by(query.filter(_quota_female=True), order).values("id")[:female_cap]
    male_ids = build_order_by(query.filter(_quota_male=True), order).values("id")[:male_cap]
    selected = build_order_by(query.filter(Q(id__in=female_ids) | Q(id__in=male_ids)), order)
    ids = list(selected.values_list("id", flat=True)) if materialize else None
    if ids is not None:
        selected = build_order_by(query.filter(id__in=ids), order)
    return selected, {
        "pool_size": pool_size, "cap_applied": cap,
        "will_enrol": len(ids) if ids is not None else selected.count(),
        "ranking": ranking, "percentage": percentage, "selected_ids": ids,
    }
