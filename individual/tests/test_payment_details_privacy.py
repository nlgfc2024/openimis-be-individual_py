from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
import pandas as pd

from individual.apps import IndividualConfig
from individual.data_source import all_payment_details_fields
from individual.gql_queries import (
    GroupDataSourceGQLType,
    IndividualDataSourceGQLType,
    _json_ext_without_payment_details,
    _payment_details_for_user,
)
from individual.schema import Query, patch_details


class PaymentDetailsPrivacyTest(SimpleTestCase):
    def test_generic_json_ext_responses_exclude_payment_details(self):
        json_ext = {
            "account_number": "001234",
            "account_name": "Test Recipient",
            "bank": "TNM Mpamba",
            "payment_provider_code": "TNM",
            "national_id": "NID-001",
        }

        result = _json_ext_without_payment_details(json_ext)

        self.assertTrue(all(field not in result for field in all_payment_details_fields()))
        self.assertEqual(result, {"national_id": "NID-001"})

    def test_generic_exports_exclude_payment_details(self):
        dataframe = pd.DataFrame([{
            "json_ext": {
                "account_number": "001234",
                "account_name": "Test Recipient",
                "bank": "TNM Mpamba",
                "payment_provider_code": "TNM",
                "national_id": "NID-001",
            },
        }])

        result = patch_details(dataframe)

        self.assertTrue(all(field not in result.columns for field in all_payment_details_fields()))
        self.assertEqual(result.loc[0, "national_id"], "NID-001")

    def test_import_data_source_json_ext_is_sanitized(self):
        source = SimpleNamespace(json_ext={
            "account_number": "001234",
            "national_id": "NID-001",
        })
        user = Mock()
        user.has_perms.return_value = True
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        with patch.object(IndividualConfig, "gql_individual_search_perms", ["159001"]):
            individual_result = IndividualDataSourceGQLType.resolve_json_ext(source, info)
            group_result = GroupDataSourceGQLType.resolve_json_ext(source, info)

        self.assertEqual(individual_result, {"national_id": "NID-001"})
        self.assertEqual(group_result, {"national_id": "NID-001"})

    def test_payment_details_field_requires_its_permission(self):
        json_ext = {"account_number": "001234", "bank": "TNM Mpamba"}
        user = Mock()

        with patch.object(IndividualConfig, "gql_kyc_payment_details_perms", ["159006"]):
            user.has_perms.return_value = False
            self.assertIsNone(_payment_details_for_user(json_ext, user))

            user.has_perms.return_value = True
            self.assertEqual(
                _payment_details_for_user(json_ext, user),
                {"account_number": "001234", "bank": "TNM Mpamba"},
            )

    def test_import_data_source_payment_details_requires_its_permission(self):
        source = SimpleNamespace(json_ext={
            "account_number": "001234",
            "account_name": "Test Recipient",
            "bank": "TNM Mpamba",
            "payment_provider_code": "TNM",
        })
        user = Mock()
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        with patch.object(IndividualConfig, "gql_kyc_payment_details_perms", ["159006"]):
            user.has_perms.return_value = False
            self.assertIsNone(
                IndividualDataSourceGQLType.resolve_payment_details(source, info)
            )

            user.has_perms.return_value = True
            self.assertEqual(
                IndividualDataSourceGQLType.resolve_payment_details(source, info),
                {
                    "account_number": "001234",
                    "account_name": "Test Recipient",
                    "bank": "TNM Mpamba",
                    "payment_provider_code": "TNM",
                },
            )

    def test_individual_export_rejects_payment_json_path_even_with_permission(self):
        user = Mock()
        user.has_perms.return_value = True
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        with patch.object(IndividualConfig, "gql_kyc_payment_details_perms", ["159006"]):
            with self.assertRaises(ValidationError):
                Query.resolve_individual_export(
                    None,
                    info,
                    fields=["json_ext__account_number"],
                )

    def test_group_export_rejects_payment_json_paths_even_with_permission(self):
        user = Mock()
        user.has_perms.return_value = True
        info = SimpleNamespace(context=SimpleNamespace(user=user))

        with patch.object(IndividualConfig, "gql_kyc_payment_details_perms", ["159006"]):
            with self.assertRaises(ValidationError):
                Query.resolve_group_export(
                    None,
                    info,
                    fields=["head__json_ext__account_number"],
                )

    def test_narrowed_kyc_config_does_not_expose_legacy_payment_fields(self):
        with patch.object(
            IndividualConfig,
            "kyc_payment_details",
            {
                "payment_fields": ["account_number"],
                "optional_payment_fields": [],
            },
        ):
            self.assertEqual(all_payment_details_fields(), {"account_number"})
            result = _json_ext_without_payment_details({
                "account_number": "001234",
                "bank": "TNM Mpamba",
            })

        self.assertEqual(result, {})
