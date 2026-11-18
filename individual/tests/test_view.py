import os
import json
import csv
from unittest.mock import patch, MagicMock
from rest_framework.test import APITestCase
from rest_framework import status
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from core.test_helpers import create_test_interactive_user
from core.models import ModuleConfiguration
from individual.apps import IndividualConfig
from individual.views import is_valid_file


class TestView(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_file_path = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'individual_upload.csv'
        )
        cls.test_config_path = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'individual_config.json'
        )

    def setUp(self):
        self.download_url = reverse('download_template_file')
        self.upload_url = reverse('import_individuals')
        self.upload_data = {
            'workflow_name': 'Test Workflow',
            'workflow_group': 'Test Group',
            'group_aggregation_column': 'group_code'
        }
        self.admin_user = create_test_interactive_user()
        self.client.force_authenticate(user=self.admin_user)

    def test_ods_upload_is_an_allowed_spreadsheet(self):
        uploaded_file = SimpleUploadedFile(
            "accounts.ods",
            b"ods-content",
            content_type="application/vnd.oasis.opendocument.spreadsheet",
        )

        valid, error = is_valid_file(uploaded_file)

        self.assertTrue(valid)
        self.assertIsNone(error)

    def test_download_template_file(self):
        response = self.client.get(self.download_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertIn(
            'attachment; filename="individual_upload_template.csv"',
            response['Content-Disposition']
        )

        expected_base_csv_header = 'first_name,last_name,dob,location_name,location_code,id'
        content = b"".join(response.streaming_content).decode('utf-8')
        header = next(csv.reader(content.splitlines()))
        expected_base_fields = [
            'first_name', 'last_name', 'dob', 'location_name', 'location_code', 'id',
        ]
        self.assertEqual(
            header[:len(expected_base_fields)],
            expected_base_fields,
            f'Expect csv template header to contain {expected_base_csv_header}, but got {content}',
        )
        for kyc_only_column in ('nid', 'account_number', 'account_name', 'bank'):
            self.assertNotIn(kyc_only_column, header)

    def test_download_kyc_details_template_file(self):
        response = self.client.get(
            self.download_url,
            {"workflow_name": "Python KYC Details"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            'attachment; filename="individual_kyc_details_template.csv"',
            response['Content-Disposition'],
        )
        content = b"".join(response.streaming_content).decode('utf-8')
        self.assertEqual(next(csv.reader(content.splitlines())), [
            "form_number", "district", "ta", "gvh", "village", "catchment",
            "first_name", "last_name", "nid", "account_number", "account_name", "bank",
        ])

    def test_download_template_file_on_individual_schema_update(self):
        with open(self.test_config_path, 'rb') as test_file:
            fixture_config = json.load(test_file)

        # Module configuration is loaded when Django starts. Patch the active
        # configuration for this request instead of relying on a model save to
        # reload AppConfig state during a test.
        with patch.object(IndividualConfig, 'individual_schema', fixture_config['individual_schema']):
            response = self.client.get(self.download_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check the template file contains individual schema fields from fixture config
        content = b"".join(response.streaming_content).decode('utf-8')
        header = next(csv.reader(content.splitlines()))
        for expected_column in ('poor', 'educated_level', 'number_of_children'):
            self.assertIn(
                expected_column,
                header,
                f'Expect csv template header to contain {expected_column}, but got {content}'
            )

    @patch('individual.views.WorkflowService')
    @patch('individual.views.IndividualImportService')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_success(
            self, mock_file_handler, mock_individual_import_service, mock_workflow_service):

        mock_workflow_service.get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': 'Test Workflow'}]
            }
        }

        mock_individual_import_service.return_value.import_individuals.return_value = {
            'success': True,
            'message': 'Import successful',
            'details': None
        }

        mock_handler_instance = MagicMock()
        mock_file_handler.return_value = mock_handler_instance

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                self.upload_url,
                data={**self.upload_data, 'file': test_file},
                format='multipart'
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        self.assertIn('message', response.data)

        mock_handler_instance.save_file.assert_called_once()

    @patch('individual.views._remove_file')
    @patch('individual.views.WorkflowService.get_workflows')
    @patch('individual.views.IndividualImportService.import_individuals')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_workflow_failure_returns_error_details(
            self, mock_file_handler, mock_import, mock_get_workflows, mock_remove_file):
        mock_get_workflows.return_value = {
            'success': True,
            'data': {'workflows': [{'id': 1, 'name': 'Test Workflow'}]},
        }
        mock_import.return_value = {
            'success': False,
            'message': 'The upload workflow failed.',
            'error': {'workflow': 'KYC upload is missing required columns: bank.'},
            'data': {'upload_uuid': 'upload-uuid', 'status': 'FAIL'},
        }

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                self.upload_url,
                data={**self.upload_data, 'file': test_file},
                format='multipart',
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data['error']['workflow'],
            'KYC upload is missing required columns: bank.',
        )
        mock_file_handler.return_value.save_file.assert_called_once()
        mock_remove_file.assert_not_called()

    @patch('individual.views.WorkflowService.get_workflows')
    @patch('individual.views.IndividualImportService.import_individuals')
    def test_import_individuals_upload_same_file_twice_triggers_rename(
        self, mock_import_service, mock_get_workflows
    ):
        mock_import_service.return_value = {'success': True}
        mock_get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': 'Test Workflow'}]
            }
        }

        test_file_name = "test_individuals.csv"
        dir_path = os.path.dirname(IndividualConfig.get_individual_upload_file_path(test_file_name))
        if default_storage.exists(dir_path):
            for filename in default_storage.listdir(dir_path)[1]:  # files only
                path = os.path.join(dir_path, filename)
                if default_storage.exists(path):
                    default_storage.delete(path)

        with open(self.test_file_path, 'rb') as test_file:
            upload_content = test_file.read()

        # First upload, freeze time at T0
        with patch("individual.views.timezone.now") as mock_now:
            mock_now.return_value = timezone.datetime(2025, 9, 11, 12, 0, 0)
            first_upload = SimpleUploadedFile(
                test_file_name,
                upload_content,
                content_type='text/csv'
            )

            response1 = self.client.post(
                self.upload_url,
                data={**self.upload_data, 'file': first_upload},
                format='multipart'
            )
            self.assertEqual(response1.status_code, status.HTTP_200_OK)

        first_path = IndividualConfig.get_individual_upload_file_path(test_file_name)
        self.assertTrue(default_storage.exists(first_path))

        # Second upload, tick time forward by +1 second (same filename, should be renamed automatically)
        with patch("individual.views.timezone.now") as mock_now:
            mock_now.return_value = timezone.datetime(2025, 9, 11, 12, 0, 1)
            second_upload = SimpleUploadedFile(
                name=test_file_name,
                content=upload_content,
                content_type='text/csv'
            )

            response2 = self.client.post(
                self.upload_url,
                data={**self.upload_data, 'file': second_upload},
                format='multipart'
            )
            self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # Check that the renamed file exists and matches the expected suffix pattern
        renamed_files = [
            f for f in default_storage.listdir(os.path.dirname(
                IndividualConfig.get_individual_upload_file_path(test_file_name)))[1]
            if f.startswith("test_individuals_") and f.endswith(".csv")
        ]
        self.assertEqual(len(renamed_files), 1)
        self.assertRegex(renamed_files[0], r"^test_individuals_\d{14}\.csv$")

    @patch('individual.views.WorkflowService')
    @patch('individual.views.IndividualImportService')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_import_service_failure(
            self, mock_file_handler, mock_individual_import_service, mock_workflow_service):

        mock_handler_instance = MagicMock()
        mock_file_handler.return_value = mock_handler_instance

        mock_individual_import_service.return_value.import_individuals.return_value = {
            'success': False,
            'message': 'Import service not available',
            'details': 'Dummy import service issue'
        }

        mock_workflow_service.get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': 'Test Workflow'}]
            }
        }

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                self.upload_url,
                data={**self.upload_data, 'file': test_file},
                format='multipart'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

        mock_handler_instance.save_file.assert_called_once()
        mock_handler_instance.remove_file.assert_called_once()

    @patch('individual.views.WorkflowService')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_workflow_service_failure(
            self, mock_file_handler, mock_workflow_service):

        mock_handler_instance = MagicMock()
        mock_file_handler.return_value = mock_handler_instance

        mock_workflow_service.get_workflows.return_value = {
            'success': False,
            'message': 'Workflow service not available',
            'details': 'Dummy workflow service issue'
        }

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                self.upload_url,
                data={**self.upload_data, 'file': test_file},
                format='multipart'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

        mock_handler_instance.save_file.assert_not_called()
        mock_handler_instance.remove_file.assert_not_called()
