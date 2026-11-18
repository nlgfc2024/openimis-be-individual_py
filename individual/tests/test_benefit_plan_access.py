from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, override_settings

from individual.custom_filters import IndividualCustomFilterWizard
from individual.schema import Query


class IndividualBenefitPlanQueryAccessTest(SimpleTestCase):
    def _resolve(self, benefit_plan_id=None):
        user = Mock()
        info = SimpleNamespace(context=SimpleNamespace(user=user))
        queryset = Mock()
        queryset.filter.return_value = queryset
        kwargs = {}
        if benefit_plan_id is not None:
            kwargs["benefitPlanId"] = benefit_plan_id

        with patch.object(Query, "_check_permissions"), patch(
            "individual.schema.append_validity_filter", return_value=[]
        ), patch(
            "individual.schema.IndividualGQLType.get_queryset",
            return_value=queryset,
        ), patch(
            "individual.schema.gql_optimizer.query",
            side_effect=lambda query, _info: query,
        ):
            result = Query().resolve_individual(info, **kwargs)
        return result, queryset, user

    @patch("social_protection.models.BenefitPlan.objects.filter")
    @patch("social_protection.benefit_plan_access.require_benefit_plan_access")
    @patch(
        "social_protection.apps.SocialProtectionConfig.enable_benefit_plan_access_control",
        True,
    )
    @patch("individual.schema.apps.is_installed", return_value=True)
    def test_programme_query_delegates_to_shared_access_service(
        self,
        _is_installed,
        require_access,
        benefit_plans,
    ):
        benefit_plans.return_value.exists.return_value = True

        result, queryset, user = self._resolve("upg-plan")

        require_access.assert_called_once_with(user, "upg-plan")
        benefit_plans.assert_called_once_with(
            id="upg-plan",
            is_deleted=False,
        )
        self.assertIs(result, queryset)

    @patch(
        "social_protection.benefit_plan_access.require_benefit_plan_access",
        side_effect=PermissionDenied("Benefit Plan access is required."),
    )
    @patch("individual.schema.apps.is_installed", return_value=True)
    def test_unauthorised_programme_query_is_rejected(
        self,
        _is_installed,
        require_access,
    ):
        with self.assertRaises(PermissionDenied):
            self._resolve("rmep-plan")
        require_access.assert_called_once()

    @patch("social_protection.models.BenefitPlan.objects.filter")
    @patch("social_protection.benefit_plan_access.require_benefit_plan_access")
    @patch(
        "social_protection.apps.SocialProtectionConfig.enable_benefit_plan_access_control",
        True,
    )
    @patch("individual.schema.apps.is_installed", return_value=True)
    def test_nonexistent_programme_is_rejected_when_enforcement_is_enabled(
        self,
        _is_installed,
        _require_access,
        benefit_plans,
    ):
        benefit_plans.return_value.exists.return_value = False
        with self.assertRaisesMessage(PermissionDenied, "does not exist"):
            self._resolve("missing-plan")

    @override_settings(ROW_SECURITY=False)
    @patch("social_protection.benefit_plan_access.require_benefit_plan_access")
    @patch(
        "social_protection.apps.SocialProtectionConfig.enable_benefit_plan_access_control",
        True,
    )
    @patch("individual.schema.apps.is_installed", return_value=True)
    @patch(
        "social_protection.models.BenefitPlan.objects.filter",
    )
    def test_programme_access_remains_active_when_row_security_is_disabled(
        self,
        benefit_plans,
        _is_installed,
        require_access,
    ):
        benefit_plans.return_value.exists.return_value = True
        self._resolve("upg-plan")
        require_access.assert_called_once()

    @patch("social_protection.models.BenefitPlan.objects.filter")
    @patch("social_protection.benefit_plan_access.require_benefit_plan_access")
    @patch(
        "social_protection.apps.SocialProtectionConfig.enable_benefit_plan_access_control",
        False,
    )
    @patch("individual.schema.apps.is_installed", return_value=True)
    def test_disabled_feature_preserves_legacy_programme_filter(
        self,
        _is_installed,
        require_access,
        benefit_plans,
    ):
        result, queryset, _user = self._resolve("legacy-plan")
        require_access.assert_called_once()
        benefit_plans.assert_not_called()
        self.assertIs(result, queryset)

    @patch("social_protection.benefit_plan_access.require_benefit_plan_access")
    @patch("individual.schema.apps.is_installed", return_value=True)
    def test_query_without_programme_preserves_shared_registry_behaviour(
        self,
        _is_installed,
        require_access,
    ):
        result, queryset, _user = self._resolve()
        require_access.assert_not_called()
        self.assertIs(result, queryset)

    @patch("individual.schema.apps.is_installed", return_value=False)
    def test_query_remains_compatible_without_social_protection(
        self,
        _is_installed,
    ):
        result, queryset, _user = self._resolve("legacy-plan")
        self.assertIs(result, queryset)


class IndividualCustomFilterBenefitPlanAccessTest(SimpleTestCase):
    tuple_type = namedtuple("Individual", ["field", "filter", "type"])
    schema = {
        "properties": {
            "programme_field": {"type": "string"},
        },
    }

    @patch("social_protection.models.BenefitPlan.objects.get")
    @patch("social_protection.benefit_plan_access.require_benefit_plan_access")
    @patch("individual.custom_filters.get_current_user")
    @patch("individual.custom_filters.apps.is_installed", return_value=True)
    def test_authorised_definition_is_loaded_after_access_check(
        self,
        _is_installed,
        current_user,
        require_access,
        get_plan,
    ):
        user = Mock()
        current_user.return_value = user
        get_plan.return_value.beneficiary_data_schema = self.schema

        definitions = IndividualCustomFilterWizard().load_definition(
            self.tuple_type,
            additional_params={"benefitPlan": "upg-plan"},
        )

        require_access.assert_called_once_with(user, "upg-plan")
        get_plan.assert_called_once_with(id="upg-plan")
        self.assertEqual(definitions[0].field, "programme_field")

    @patch("social_protection.models.BenefitPlan.objects.get")
    @patch(
        "social_protection.benefit_plan_access.require_benefit_plan_access",
        side_effect=PermissionDenied("Benefit Plan access is required."),
    )
    @patch("individual.custom_filters.get_current_user", return_value=Mock())
    @patch("individual.custom_filters.apps.is_installed", return_value=True)
    def test_unauthorised_definition_is_rejected_before_plan_is_loaded(
        self,
        _is_installed,
        _current_user,
        _require_access,
        get_plan,
    ):
        with self.assertRaises(PermissionDenied):
            IndividualCustomFilterWizard().load_definition(
                self.tuple_type,
                additional_params={"benefitPlan": "rmep-plan"},
            )
        get_plan.assert_not_called()

    @patch("individual.custom_filters.apps.is_installed", return_value=False)
    @patch("individual.custom_filters.IndividualConfig.individual_schema", None)
    def test_missing_identifier_and_missing_social_protection_are_supported(
        self,
        _is_installed,
    ):
        wizard = IndividualCustomFilterWizard()
        self.assertEqual(wizard.load_definition(self.tuple_type), [])
        self.assertEqual(
            wizard.load_definition(
                self.tuple_type,
                additional_params={"benefitPlan": "legacy-plan"},
            ),
            [],
        )
