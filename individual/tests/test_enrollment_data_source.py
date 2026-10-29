import copy

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.services import build_group_enrollment_queryset, build_individual_enrollment_queryset
from individual.tests.test_helpers import create_group, create_individual
from social_protection.apps import SocialProtectionConfig
from social_protection.tests.test_helpers import create_benefit_plan


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
    def _individual(self, data_source=None):
        json_ext = {"able_bodied": False, **({"data_source": data_source} if data_source else {})}
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

    def _eligible_groups(self, plan):
        return set(build_group_enrollment_queryset([], str(plan.id), "ACTIVE").values_list("id", flat=True))

    def test_group_plans_enroll_only_households_of_their_source(self):
        # Same code in both sources: households are told apart by their tag, not their code
        msr_group = create_group(self.user.username, {"code": "30874398", "json_ext": {"able_bodied": False}})
        pwp_group = create_group(self.user.username, {
            "code": "30874398", "json_ext": {"able_bodied": False, "data_source": "pwp"},
        })

        self.assertEqual(self._eligible_groups(self._plan("GROUP", "pwp")), {pwp_group.id})
        self.assertEqual(self._eligible_groups(self._plan("GROUP")), {msr_group.id})
