from django.test import TestCase
from unittest.mock import patch
from core.test_helpers import create_test_interactive_user
from individual.data_source import PAYMENT_DETAILS_REQUIRED_COLUMNS
from individual.workflows.base_individual_update import _validate_kyc_details_file
from individual.workflows.utils import (
    DataUpdateWorkflow,
    PythonWorkflowHandlerException,
    SqlProcedurePythonWorkflow,
)
from opensearch_reports.service import BaseSyncDocument
import pandas as pd
import json
import uuid


class TestBasePythonWorkflowExecutor(TestCase):

    def setUp(self):
        self.user = create_test_interactive_user(username='Admin')
        self.upload_id = uuid.uuid4()

        # Patch IndividualConfig schema and load_dataframe function
        self.individual_schema = {
            "properties": {
                "email": {"type": "string", "uniqueness": True},
                "able_bodied": {"type": "boolean"},
            }
        }
        self.mock_load_dataframe = patch('individual.workflows.utils.load_dataframe').start()
        self.mock_load_dataframe.return_value = pd.DataFrame()
        patch(
            'individual.workflows.utils.IndividualConfig.individual_schema',
            json.dumps(self.individual_schema)
        ).start()

        # patch opensearch document update so it doesn't try to connect & sync
        self.doc_update_patcher = patch.object(BaseSyncDocument, "update")
        self.doc_update_patcher.start()

        self.executor = SqlProcedurePythonWorkflow(self.upload_id, self.user.id)

    def tearDown(self):
        patch.stopall()
        super().tearDown()

    def test_validate_dataframe_headers_valid(self):
        self.executor.df = pd.DataFrame(columns=['first_name', 'last_name', 'dob', 'id', 'location_name', 'location_code'])
        try:
            self.executor.validate_dataframe_headers()
        except PythonWorkflowHandlerException:
            self.fail("validate_dataframe_headers() raised PythonWorkflowHandlerException unexpectedly!")

    def test_validate_dataframe_headers_accepts_data_source_outside_schema(self):
        self.executor.df = pd.DataFrame(columns=['first_name', 'last_name', 'dob', 'id', 'location_name',
                                                 'location_code', 'data_source'])
        try:
            self.executor.validate_dataframe_headers()
        except PythonWorkflowHandlerException:
            self.fail("data_source is a system column and must not need to be in individual_schema")

    def test_validate_dataframe_headers_missing_required_fields(self):
        # DataFrame missing required 'first_name' column
        self.executor.df = pd.DataFrame(columns=['last_name', 'dob', 'id', 'location_name', 'location_code'])

        with self.assertRaises(PythonWorkflowHandlerException) as cm:
            self.executor.validate_dataframe_headers()

        self.assertIn("Uploaded individuals missing essential header: first_name", str(cm.exception))

    def test_validate_dataframe_headers_invalid_headers(self):
        # DataFrame with an invalid column
        self.executor.df = pd.DataFrame(columns=['first_name', 'last_name', 'dob', 'id', 'location_name', 'location_code', 'unexpected_column'])

        with self.assertRaises(PythonWorkflowHandlerException) as cm:
            self.executor.validate_dataframe_headers()

        self.assertIn("Uploaded individuals contains invalid columns: {'unexpected_column'}", str(cm.exception))

    def test_validate_dataframe_headers_update_missing_id(self):
        # DataFrame missing 'ID' when is_update=True
        columns = ['first_name', 'last_name', 'dob', 'id', 'location_name', 'location_code']
        self.executor.df = pd.DataFrame(columns=columns)

        with self.assertRaises(PythonWorkflowHandlerException) as cm:
            self.executor.validate_dataframe_headers(is_update=True)

        self.assertIn("Uploaded individuals missing essential header: ID", str(cm.exception))

        # adding ID should pass the validation
        columns.append('ID')
        self.executor.df = pd.DataFrame(columns=columns)
        try:
            self.executor.validate_dataframe_headers(is_update=True)
        except PythonWorkflowHandlerException:
            self.fail("validate_dataframe_headers() raised PythonWorkflowHandlerException unexpectedly!")

    def test_standard_workflows_reject_payment_detail_columns(self):
        columns = [
            'first_name', 'last_name', 'dob', 'id', 'location_name', 'location_code',
            'form_number', 'nid', 'account_number', 'account_name', 'bank',
        ]
        self.executor.df = pd.DataFrame([{column: '' for column in columns}], columns=columns)

        with self.assertRaisesRegex(PythonWorkflowHandlerException, "only allowed through the Python KYC Details workflow"):
            self.executor.validate_dataframe_headers()
        columns.append('ID')
        self.executor.df = pd.DataFrame([{column: '' for column in columns}], columns=columns)
        with self.assertRaisesRegex(PythonWorkflowHandlerException, "only allowed through the Python KYC Details workflow"):
            self.executor.validate_dataframe_headers(is_update=True)

    def test_payment_details_values_are_only_allowed_in_kyc_workflow(self):
        columns = [
            'first_name', 'last_name', 'dob', 'id', 'location_name', 'location_code',
            'ID', 'form_number', 'nid', 'account_number', 'account_name', 'bank',
        ]
        row = {column: '' for column in columns}
        row.update({
            'ID': str(uuid.uuid4()),
            'form_number': 'F-001',
            'nid': 'NID-001',
            'account_number': '001234',
            'account_name': 'Test Recipient',
            'bank': 'TNM Mpamba',
        })
        self.executor.df = pd.DataFrame([row], columns=columns)

        with self.assertRaises(PythonWorkflowHandlerException):
            self.executor.validate_dataframe_headers()
        with self.assertRaisesRegex(PythonWorkflowHandlerException, "only allowed through the Python KYC Details workflow"):
            self.executor.validate_dataframe_headers(is_update=True)
        self.executor.validate_dataframe_headers(is_update=True, allow_payment_details=True)

    def test_kyc_details_upload_requires_payment_columns_and_values(self):
        incomplete = pd.DataFrame([{"form_number": "F-001"}])
        with self.assertRaisesRegex(PythonWorkflowHandlerException, "missing required columns"):
            _validate_kyc_details_file(incomplete)

        empty_payment_details = pd.DataFrame([
            {field: " " for field in PAYMENT_DETAILS_REQUIRED_COLUMNS}
        ])
        with self.assertRaisesRegex(
                PythonWorkflowHandlerException,
                r"row 2: .*account_name.*account_number.*bank.*form_number.*nid"):
            _validate_kyc_details_file(empty_payment_details)

    def test_kyc_details_upload_identifies_rows_with_missing_values(self):
        complete_row = {field: "value" for field in PAYMENT_DETAILS_REQUIRED_COLUMNS}
        incomplete_row = {**complete_row, "account_number": ""}

        with self.assertRaisesRegex(
                PythonWorkflowHandlerException,
                r"row 3: account_number"):
            _validate_kyc_details_file(pd.DataFrame([complete_row, incomplete_row]))

    def test_kyc_details_upload_accepts_complete_payment_row(self):
        row = {field: "value" for field in PAYMENT_DETAILS_REQUIRED_COLUMNS}
        row.update({
            "district": "Balaka",
            "ta": "Chanthunya",
            "gvh": "MANJANJA",
            "village": "SAMI",
            "catchment": "Chete_micro",
        })
        _validate_kyc_details_file(pd.DataFrame([row]))

    def test_kyc_details_upload_rejects_unexpected_columns(self):
        row = {field: "value" for field in PAYMENT_DETAILS_REQUIRED_COLUMNS}
        row["unexpected"] = "value"
        with self.assertRaisesRegex(PythonWorkflowHandlerException, "unsupported columns"):
            _validate_kyc_details_file(pd.DataFrame([row]))

    def test_update_task_does_not_repeat_payment_detail_validation(self):
        with patch("individual.workflows.utils.IndividualImportService") as service_class, \
                patch("individual.workflows.utils.IndividualDataSource.objects.filter"):
            service = service_class.return_value
            service.validate_import_individuals.return_value = {"summary_invalid_items": []}
            update_workflow = DataUpdateWorkflow(
                self.upload_id,
                self.user.id,
                import_service=service_class,
            )

            self.assertTrue(update_workflow.should_create_task)
            service.validate_import_individuals.assert_called_once()
            service.validate_payment_details_upload.assert_not_called()

    def test_kyc_update_workflow_forces_maker_checker(self):
        with patch("individual.workflows.utils.IndividualImportService") as service_class:
            update_workflow = DataUpdateWorkflow(
                self.upload_id,
                self.user.id,
                import_service=service_class,
                require_approval=True,
            )

            update_workflow._create_task_function()

            service_class.return_value.create_task_with_update_valid_items.assert_called_once_with(
                self.upload_id,
                force_maker_checker=True,
            )
