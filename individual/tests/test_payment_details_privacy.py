from django.test import SimpleTestCase
import pandas as pd

from individual.data_source import all_payment_details_fields
from individual.gql_queries import _json_ext_without_payment_details
from individual.schema import patch_details


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
