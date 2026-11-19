from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import Mock, patch

import graphene
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase, override_settings

from core.models import User
from core.test_helpers import create_test_technical_user
from individual.custom_filters import IndividualCustomFilterWizard
from individual.models import (
    Group,
    IndividualDataSource,
    IndividualDataSourceUpload,
    IndividualDataUploadRecords,
)
from individual.schema import Query
from social_protection.apps import SocialProtectionConfig
from social_protection.models import (
    BenefitPlan,
    BenefitPlanDataUploadRecords,
    GroupBeneficiary,
    UserBenefitPlanAccess,
)


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


@override_settings(ROW_SECURITY=False)
class IndividualBenefitPlanIntegrationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_technical_user(username="track04-user")
        cls.audit_user = create_test_technical_user(username="track04-audit")
        cls.upg = BenefitPlan(
            code="T04UPG01",
            name="Track 04 UPG",
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
        )
        cls.upg.save(user=cls.audit_user)
        cls.rmep = BenefitPlan(
            code="T04RMEP",
            name="Track 04 RMEP",
            type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
        )
        cls.rmep.save(user=cls.audit_user)
        UserBenefitPlanAccess(
            user=cls.user,
            benefit_plan=cls.upg,
        ).save(user=cls.audit_user)

        cls.upg_group = Group(code="T04-UPG-GROUP")
        cls.upg_group.save(user=cls.audit_user)
        cls.rmep_group = Group(code="T04-RMEP-GROUP")
        cls.rmep_group.save(user=cls.audit_user)
        GroupBeneficiary(
            group=cls.upg_group,
            benefit_plan=cls.upg,
            status="ACTIVE",
        ).save(user=cls.audit_user)
        GroupBeneficiary(
            group=cls.rmep_group,
            benefit_plan=cls.rmep,
            status="ACTIVE",
        ).save(user=cls.audit_user)

        cls.upg_upload = cls._create_upload("track04-upg.csv", cls.upg)
        cls.rmep_upload = cls._create_upload("track04-rmep.csv", cls.rmep)

    @classmethod
    def _create_upload(cls, filename, benefit_plan):
        upload = IndividualDataSourceUpload(
            source_name=filename,
            source_type="text/csv",
            status=IndividualDataSourceUpload.Status.SUCCESS,
            error={},
            json_ext={},
        )
        upload.save(user=cls.audit_user)
        IndividualDataSource(
            upload=upload,
            validations={},
            json_ext={"private": filename},
        ).save(user=cls.audit_user)
        IndividualDataUploadRecords(
            data_upload=upload,
            workflow="track04",
        ).save(user=cls.audit_user)
        BenefitPlanDataUploadRecords(
            data_upload=upload,
            benefit_plan=benefit_plan,
            workflow="track04",
        ).save(user=cls.audit_user)
        return upload

    def setUp(self):
        self.original_enabled = (
            SocialProtectionConfig.enable_benefit_plan_access_control
        )
        self.original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]

    def tearDown(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = (
            self.original_enabled
        )
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = self.original_all

    def permission_patch(self, all_plan=False):
        return patch.object(
            User,
            "has_perms",
            side_effect=lambda permissions: all_plan or "160006" not in permissions,
        )

    def execute(self, query):
        schema = graphene.Schema(query=Track04Query)
        return schema.execute(
            query,
            context_value=SimpleNamespace(user=self.user, headers={}),
        )

    def test_real_assignment_scopes_group_kyc_graphql(self):
        assigned_query = """
            query {
              group(benefitPlanId: "%s") {
                edges { node { code } }
              }
            }
        """
        # The sensitive KYC path must reject access before constructing bank
        # and primary-recipient annotations for an unassigned programme.
        unassigned_kyc_query = """
            query {
              group(benefitPlanId: "%s", includePrimaryRecipientData: true) {
                edges { node { code primaryRecipientBank } }
              }
            }
        """
        registry_fields = (
            "primary_worker",
            "validation_status",
            "VERIFIED",
            "validation_status",
            "VERIFIED",
        )
        with self.permission_patch(), patch(
            "individual.primary_recipients._registry_fields",
            return_value=registry_fields,
        ):
            assigned = self.execute(assigned_query % self.upg.id)
            unassigned = self.execute(unassigned_kyc_query % self.rmep.id)
        self.assertFalse(assigned.errors, assigned.errors)
        self.assertEqual(
            [edge["node"]["code"] for edge in assigned.data["group"]["edges"]],
            [self.upg_group.code],
        )
        self.assertTrue(unassigned.errors)
        self.assertIn("Benefit Plan access", unassigned.errors[0].message)

    def test_real_assignment_scopes_upload_rows_uploads_and_history(self):
        query = """
            query {
              individualDataSource { edges { node { upload { sourceName } } } }
              individualDataSourceUpload { edges { node { sourceName } } }
              individualDataUploadHistory {
                edges { node { dataUpload { sourceName } } }
              }
            }
        """
        with self.permission_patch():
            result = self.execute(query)
        self.assertFalse(result.errors, result.errors)
        self.assertEqual(
            [edge["node"]["upload"]["sourceName"] for edge in
             result.data["individualDataSource"]["edges"]],
            [self.upg_upload.source_name],
        )
        self.assertEqual(
            [edge["node"]["sourceName"] for edge in
             result.data["individualDataSourceUpload"]["edges"]],
            [self.upg_upload.source_name],
        )
        self.assertEqual(
            [edge["node"]["dataUpload"]["sourceName"] for edge in
             result.data["individualDataUploadHistory"]["edges"]],
            [self.upg_upload.source_name],
        )

    def test_all_plan_and_disabled_modes_preserve_upload_visibility(self):
        query = """
            query {
              individualDataSourceUpload { edges { node { sourceName } } }
            }
        """
        with self.permission_patch(all_plan=True):
            all_plan = self.execute(query)
        self.assertFalse(all_plan.errors, all_plan.errors)
        self.assertTrue(
            {self.upg_upload.source_name, self.rmep_upload.source_name}
            .issubset({edge["node"]["sourceName"] for edge in
                       all_plan.data["individualDataSourceUpload"]["edges"]}),
        )

        SocialProtectionConfig.enable_benefit_plan_access_control = False
        with self.permission_patch():
            legacy = self.execute(query)
        self.assertFalse(legacy.errors, legacy.errors)
        self.assertTrue(
            {self.upg_upload.source_name, self.rmep_upload.source_name}
            .issubset({edge["node"]["sourceName"] for edge in
                       legacy.data["individualDataSourceUpload"]["edges"]}),
        )


class Track04Query(Query, graphene.ObjectType):
    node = graphene.relay.Node.Field()
