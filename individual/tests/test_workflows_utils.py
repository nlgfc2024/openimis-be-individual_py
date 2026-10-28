from django.test import TestCase
from unittest.mock import patch
from core.test_helpers import create_test_interactive_user
from individual.workflows.utils import SqlProcedurePythonWorkflow, PythonWorkflowHandlerException
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

    def test_validate_dataframe_headers_accepts_source_type_outside_schema(self):
        self.executor.df = pd.DataFrame(columns=['first_name', 'last_name', 'dob', 'id', 'location_name',
                                                 'location_code', 'source_type'])
        try:
            self.executor.validate_dataframe_headers()
        except PythonWorkflowHandlerException:
            self.fail("source_type is a system column and must not need to be in individual_schema")

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
