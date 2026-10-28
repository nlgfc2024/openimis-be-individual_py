import copy
import json
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from individual.models import Individual
from individual.data_source import benefit_plan_data_source, data_source_group_code, data_source_q
from individual.services import build_group_enrollment_queryset, build_individual_enrollment_queryset
from individual.tests.test_helpers import create_group, create_individual
from social_protection.apps import SocialProtectionConfig
from social_protection.tests.test_helpers import create_benefit_plan


class BenefitPlanDataSourceTest(SimpleTestCase):
    def test_defaults_to_msr(self):
        self.assertEqual(benefit_plan_data_source(SimpleNamespace(json_ext={})), "ubr")
        self.assertEqual(benefit_plan_data_source(SimpleNamespace(json_ext=None)), "ubr")

    def test_reads_json_ext_stored_as_text(self):
        plan = SimpleNamespace(json_ext=json.dumps({"data_source": "pwp"}))
        self.assertEqual(benefit_plan_data_source(plan), "pwp")

    def test_group_code_is_prefixed_only_for_non_default_sources(self):
        self.assertEqual(data_source_group_code("ubr", "30874398"), "30874398")
        self.assertEqual(data_source_group_code("pwp", "30874398"), "pwp:30874398")
        self.assertEqual(data_source_group_code("pwp", "pwp:30874398"), "pwp:30874398")


class EnrollmentDataSourceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.original_mandatory_criteria = copy.deepcopy(SocialProtectionConfig.mandatory_enrollment_criteria)
        SocialProtectionConfig.mandatory_enrollment_criteria = {"INDIVIDUAL": {}, "GROUP": {}}

    def tearDown(self):
        SocialProtectionConfig.mandatory_enrollment_criteria = self.original_mandatory_criteria

    def _plan(self, plan_type, data_source=None):
        json_ext = {"data_source": data_source} if data_source else {}
        return create_benefit_plan(self.user.username, payload_override={"type": plan_type, "json_ext": json_ext})

    # The helper's default plan requires able_bodied=False for ACTIVE, so records meet it and only the source decides
    def _individual(self, source_type=None):
        json_ext = {"able_bodied": False, **({"source_type": source_type} if source_type else {})}
        return create_individual(self.user.username, {"json_ext": json_ext})

    def _eligible_individuals(self, plan):
        return set(build_individual_enrollment_queryset([], str(plan.id), "ACTIVE").values_list("id", flat=True))

    def test_default_plan_enrolls_msr_and_untagged_people_only(self):
        untagged, msr = self._individual(), self._individual("ubr")
        self._individual("pwp")

        self.assertEqual(self._eligible_individuals(self._plan("INDIVIDUAL")), {untagged.id, msr.id})

    def test_plan_enrolls_only_its_own_source(self):
        self._individual(), self._individual("ubr")
        pwp = self._individual("pwp")

        self.assertEqual(self._eligible_individuals(self._plan("INDIVIDUAL", "pwp")), {pwp.id})

    def test_group_plan_enrolls_only_its_own_source(self):
        create_group(self.user.username, {"code": "30874398", "json_ext": {"able_bodied": False}})
        pwp_group = create_group(self.user.username, {
            "code": "pwp:30874398", "json_ext": {"able_bodied": False, "source_type": "pwp"},
        })

        eligible = build_group_enrollment_queryset([], str(self._plan("GROUP", "pwp").id), "ACTIVE")

        self.assertEqual(set(eligible.values_list("id", flat=True)), {pwp_group.id})

    def test_data_source_q_matches_untagged_only_for_default(self):
        untagged, pwp = self._individual(), self._individual("pwp")
        ids = {untagged.id, pwp.id}

        default = set(Individual.objects.filter(data_source_q("ubr"), id__in=ids).values_list("id", flat=True))
        other = set(Individual.objects.filter(data_source_q("pwp"), id__in=ids).values_list("id", flat=True))

        self.assertEqual(default, {untagged.id})
        self.assertEqual(other, {pwp.id})
