import copy
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from individual.services import (
    IndividualService,
    _criterion_to_condition,
    build_group_enrollment_queryset,
    build_individual_enrollment_queryset,
)
from individual.custom_filters import GroupCustomFilterWizard, IndividualCustomFilterWizard
from individual.tests.test_helpers import create_group, create_individual
from social_protection.tests.test_helpers import create_benefit_plan
from social_protection.apps import SocialProtectionConfig


class EnrollmentCriterionNormalizationTest(SimpleTestCase):
    def test_custom_filter_string_cast_decodes_json_quoting(self):
        wizard = IndividualCustomFilterWizard()
        cast_value = wizard._IndividualCustomFilterWizard__cast_value

        self.assertEqual(cast_value('"Karonga"', 'string'), "Karonga")
        self.assertEqual(
            cast_value('"Nkhata \\"Bay\\""', 'string'),
            'Nkhata "Bay"',
        )

    def test_custom_filter_string_cast_preserves_legacy_unquoted_values(self):
        wizard = IndividualCustomFilterWizard()
        cast_value = wizard._IndividualCustomFilterWizard__cast_value

        self.assertEqual(cast_value("Karonga", "string"), "Karonga")

    def test_individual_custom_filter_preserves_equals_in_string_value(self):
        query = Mock()
        query.filter.return_value = query

        result = IndividualCustomFilterWizard().apply_filter_to_queryset(
            ['district__exact__string="a=b"'],
            query,
        )

        query.filter.assert_called_once_with(json_ext__district__exact="a=b")
        self.assertIs(result, query)

    def test_group_custom_filter_preserves_equals_in_string_value(self):
        query = Mock()
        query.filter.return_value = query

        result = GroupCustomFilterWizard().apply_filter_to_queryset(
            ['district__exact__string="a=b"'],
            query,
        )

        query.filter.assert_called_once_with(json_ext__district__exact="a=b")
        self.assertIs(result, query)

    def test_canonical_string_value_is_quoted_for_custom_filter_casting(self):
        condition = _criterion_to_condition({
            "field": "validation_status",
            "filter": "exact",
            "type": "string",
            "value": "VERIFIED",
        })
        self.assertEqual(
            condition,
            'validation_status__exact__string="VERIFIED"',
        )

    def test_existing_condition_is_preserved(self):
        condition = "score__gte__integer=10"
        self.assertEqual(
            _criterion_to_condition({"custom_filter_condition": condition}),
            condition,
        )

    @patch("individual.services.build_individual_enrollment_queryset")
    def test_confirmation_signal_result_contains_authoritative_queryset(
        self,
        build_queryset,
    ):
        authoritative_queryset = Mock()
        assigned_queryset = Mock()
        not_assigned_queryset = Mock()
        authoritative_queryset.filter.return_value = assigned_queryset
        authoritative_queryset.exclude.return_value = not_assigned_queryset
        assigned_queryset.values_list.return_value = []
        build_queryset.return_value = authoritative_queryset

        service = IndividualService(Mock())
        result = IndividualService.select_individuals_to_benefit_plan.__wrapped__(
            service,
            [],
            "plan-id",
            "ACTIVE",
            service.user,
        )

        self.assertIs(
            result["individual_query_with_filters"],
            authoritative_queryset,
        )
        self.assertIs(
            result["individuals_not_assigned_to_selected_programme"],
            not_assigned_queryset,
        )


class AuthoritativeEnrollmentTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def setUp(self):
        self.original_mandatory_criteria = copy.deepcopy(
            SocialProtectionConfig.mandatory_enrollment_criteria
        )
        SocialProtectionConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {},
        }

    def tearDown(self):
        SocialProtectionConfig.mandatory_enrollment_criteria = (
            self.original_mandatory_criteria
        )

    def _benefit_plan(self, benefit_plan_type):
        return create_benefit_plan(self.user.username, payload_override={
            "type": benefit_plan_type,
            "beneficiary_data_schema": {
                "properties": {
                    "validation_status": {"type": "string"},
                    "district": {"type": "string"},
                }
            },
            "json_ext": {
                "advanced_criteria": {
                    "ACTIVE": [{
                        "field": "validation_status",
                        "filter": "exact",
                        "type": "string",
                        "value": "VERIFIED",
                    }]
                }
            },
        })

    def test_individual_confirmation_cannot_omit_phase_criteria(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        eligible = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {eligible.id})

    def test_operator_filter_can_only_narrow_phase_results(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")
        selected = create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Lilongwe",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "VERIFIED",
                "district": "Blantyre",
            }
        })
        create_individual(self.user.username, {
            "json_ext": {
                "validation_status": "PENDING",
                "district": "Lilongwe",
            }
        })

        result = build_individual_enrollment_queryset(
            custom_filters=['district__exact__string="Lilongwe"'],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {selected.id})

    def test_rejects_operator_filter_outside_phase_schema(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")

        with self.assertRaisesMessage(ValidationError, "is not allowed"):
            build_individual_enrollment_queryset(
                custom_filters=['unconfigured__exact__string="value"'],
                benefit_plan_id=str(benefit_plan.id),
                status="ACTIVE",
            )

    def test_group_confirmation_uses_group_json_ext(self):
        benefit_plan = self._benefit_plan("GROUP")
        eligible = create_group(self.user.username, {
            "json_ext": {"validation_status": "VERIFIED"}
        })
        create_group(self.user.username, {
            "json_ext": {"validation_status": "PENDING"}
        })

        result = build_group_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="ACTIVE",
        )

        selected_ids = set(result.values_list("id", flat=True))
        self.assertEqual(selected_ids, {eligible.id})

    def test_group_immutable_rule_cannot_be_removed_from_phase_or_payload(self):
        SocialProtectionConfig.mandatory_enrollment_criteria = {
            "INDIVIDUAL": {},
            "GROUP": {
                "POTENTIAL": [{
                    "field": "validation_status",
                    "filter": "exact",
                    "type": "string",
                    "value": "VERIFIED",
                }]
            },
        }
        benefit_plan = create_benefit_plan(self.user.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {
                "properties": {"validation_status": {"type": "string"}}
            },
            "json_ext": {"advanced_criteria": {"POTENTIAL": []}},
        })
        eligible = create_group(self.user.username, {
            "json_ext": {"validation_status": "VERIFIED"}
        })
        create_group(self.user.username, {
            "json_ext": {"validation_status": "NOT_VERIFIED"}
        })

        result = build_group_enrollment_queryset(
            custom_filters=[],
            benefit_plan_id=str(benefit_plan.id),
            status="POTENTIAL",
        )

        self.assertEqual(set(result.values_list("id", flat=True)), {eligible.id})

        attempted_replacement = build_group_enrollment_queryset(
            custom_filters=[
                'validation_status__exact__string="NOT_VERIFIED"'
            ],
            benefit_plan_id=str(benefit_plan.id),
            status="POTENTIAL",
        )
        self.assertFalse(attempted_replacement.exists())

    def test_rejects_benefit_plan_type_mismatch(self):
        group_plan = self._benefit_plan("GROUP")

        with self.assertRaisesMessage(ValidationError, "cannot be used"):
            build_individual_enrollment_queryset(
                custom_filters=[],
                benefit_plan_id=str(group_plan.id),
                status="ACTIVE",
            )

    def test_rejects_unknown_status(self):
        benefit_plan = self._benefit_plan("INDIVIDUAL")

        with self.assertRaisesMessage(ValidationError, "Unsupported"):
            build_individual_enrollment_queryset(
                custom_filters=[],
                benefit_plan_id=str(benefit_plan.id),
                status="UNKNOWN",
            )
