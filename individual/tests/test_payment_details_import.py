import json
import uuid
import zipfile
from io import BytesIO
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from core.models import Role
from individual.models import (
    Group,
    GroupIndividual,
    Individual,
    IndividualDataUploadRecords,
    IndividualDataSource,
    IndividualDataSourceUpload,
)
from individual.data_source import (
    KYC_DETAILS_WORKFLOW_NAME,
    normalize_kyc_dataframe_headers,
    payment_details_template_columns,
)
from individual.apps import IndividualConfig
from individual.services import (
    INVALID_NUMERIC_EXCEL_CELL,
    IndividualImportService,
    IndividualTaskCreatorService,
    _load_ods_with_text_columns,
    _load_xlsx_with_text_columns,
    _mark_numeric_ods_account_cells,
)
from individual.tests.test_helpers import create_test_interactive_user
from individual.utils import load_dataframe
from individual.workflows.base_individual_update import _validate_kyc_details_file


class PaymentDetailsExcelLoaderTests(SimpleTestCase):
    def test_kyc_columns_and_aliases_follow_module_configuration(self):
        config = {
            "payment_fields": ["account_number"],
            "location_columns": ["ward"],
            "context_columns": [],
            "template_columns": ["form_number", "ward", "first_name", "last_name", "nid", "account_number"],
            "header_aliases": {
                "household identifier": "form_number",
                "wallet account no": "account_number",
            },
        }
        dataframe = pd.DataFrame([{
            "Household Identifier": "F-100",
            "ward": "Ward 1",
            "first_name": "Alice",
            "last_name": "Recipient",
            "nid": "NID-100",
            "Wallet Account No": "000100",
        }])

        with patch.object(IndividualConfig, "kyc_payment_details", config):
            normalized = normalize_kyc_dataframe_headers(dataframe)
            _validate_kyc_details_file(normalized)
            template_columns = payment_details_template_columns()

        self.assertIn("ward", normalized.columns)
        self.assertEqual(normalized.loc[0, "account_number"], "000100")
        self.assertEqual(template_columns, config["template_columns"])

    def test_schema_uniqueness_skips_fields_absent_from_upload(self):
        dataframe = pd.DataFrame([{"first_name": "Alice"}])
        schema = json.dumps({
            "properties": {
                "national_id": {"type": "string", "uniqueness": True},
            },
        })

        with patch.object(IndividualConfig, "individual_schema", schema), \
                patch.object(IndividualImportService, "process_chunk", return_value=[]), \
                patch.object(IndividualImportService, "save_validation_error_in_data_source_bulk"), \
                patch("individual.services.fetch_summary_of_broken_items", return_value=[]):
            validated, invalid = IndividualImportService(None)._validate_possible_individuals(
                dataframe,
                uuid.uuid4(),
            )

        self.assertEqual(validated, [])
        self.assertEqual(invalid, [])

    def test_kyc_export_headers_normalize_to_upload_fields(self):
        dataframe = pd.DataFrame(columns=[
            "District",
            "Traditional Authority",
            "Group Village Head",
            "Village",
            "Catchment",
            "UBR household form number",
            "First Name",
            "Last Name",
            "National ID",
            "Date of Birth",
            "Account number",
            "Account name",
            "Bank / provider",
        ])

        normalized = normalize_kyc_dataframe_headers(dataframe)

        self.assertEqual(list(normalized.columns), [
            "district",
            "ta",
            "gvh",
            "village",
            "catchment",
            "form_number",
            "first_name",
            "last_name",
            "nid",
            "dob",
            "account_number",
            "account_name",
            "bank",
        ])

    def test_kyc_header_normalization_reports_alias_collisions(self):
        dataframe = pd.DataFrame(columns=["form_number", "UBR household form number"])
        with self.assertRaisesRegex(ValueError, "duplicate columns"):
            normalize_kyc_dataframe_headers(dataframe)

    def test_completed_kyc_export_row_is_accepted_for_upload(self):
        dataframe = pd.DataFrame([{
            "District": "Balaka",
            "Traditional Authority": "Chanthunya",
            "Group Village Head": "MANJANJA",
            "Village": "SAMI",
            "Catchment": "Chete_micro",
            "UBR household form number": "91748763",
            "First Name": "Maureen",
            "Last Name": "Banda",
            "National ID": "MZJKAJ2P",
            "Date of Birth": "1990-01-01",
            "Account number": "00012345",
            "Account name": "MAUREEN BANDA",
            "Bank / provider": "TNM Mpamba",
        }])

        _validate_kyc_details_file(normalize_kyc_dataframe_headers(dataframe))

    def test_household_kyc_export_headers_are_accepted(self):
        dataframe = pd.DataFrame([{
            "district_name": "Karonga",
            "traditional_authority_name": "Kilipula",
            "catchment_name": "Benjamin",
            "phase_name": "202623082 phase anavili1000",
            "group_village_head_name": "Mweniwisi",
            "village_name": "Kenani",
            "form_number": "30201307",
            "full_name": "Mofat Nkhonya",
            "national_id": "NID-001",
            "date_of_birth": "2003-05-20",
            "Gender": "Male",
            "account_number": "00012345",
            "account_name": "Mofat Nkhonya",
            "bank": "Mpamba",
        }])

        normalized = normalize_kyc_dataframe_headers(dataframe)

        self.assertEqual(normalized.loc[0, "first_name"], "Mofat")
        self.assertEqual(normalized.loc[0, "last_name"], "Nkhonya")
        self.assertNotIn("full_name", normalized.columns)
        _validate_kyc_details_file(normalized)

    def test_xlsx_preserves_text_account_numbers_and_marks_numeric_cells(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append(["account_number"])
        worksheet.append(["00012345"])
        worksheet.append([12345])
        file_buffer = BytesIO()
        workbook.save(file_buffer)
        file_buffer.seek(0)

        dataframe = _load_xlsx_with_text_columns(file_buffer)

        self.assertEqual(dataframe.loc[0, "account_number"], "00012345")
        self.assertEqual(dataframe.loc[1, "account_number"], "__INVALID_NUMERIC_EXCEL_CELL__")

    def test_xlsx_checks_the_first_worksheet_used_by_pandas(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "KYC data"
        worksheet.append(["account_number"])
        worksheet.append(["00012345"])
        other_sheet = workbook.create_sheet("Other")
        other_sheet.append(["account_number"])
        other_sheet.append([12345])
        workbook.active = 1
        file_buffer = BytesIO()
        workbook.save(file_buffer)
        file_buffer.seek(0)

        dataframe = _load_xlsx_with_text_columns(file_buffer)

        self.assertEqual(dataframe.loc[0, "account_number"], "00012345")

    def test_ods_preserves_text_account_number_and_marks_numeric_cell(self):
        content = b'''<?xml version="1.0" encoding="UTF-8"?>
        <office:document-content
            xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
            xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
            xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
          <office:body><office:spreadsheet>
            <table:table table:name="Sheet1">
              <table:table-row>
                <table:table-cell office:value-type="string"><text:p>account_number</text:p></table:table-cell>
              </table:table-row>
              <table:table-row>
                <table:table-cell office:value-type="string"><text:p>00012345</text:p></table:table-cell>
              </table:table-row>
              <table:table-row>
                <table:table-cell office:value-type="float" office:value="12345"><text:p>12345</text:p></table:table-cell>
              </table:table-row>
            </table:table>
          </office:spreadsheet></office:body>
        </office:document-content>'''
        file_buffer = BytesIO()
        with zipfile.ZipFile(file_buffer, "w") as ods_file:
            ods_file.writestr("content.xml", content)
        file_buffer.seek(0)
        dataframe = pd.DataFrame({"account_number": ["00012345", "12345"]})

        result = _mark_numeric_ods_account_cells(dataframe, file_buffer)

        self.assertEqual(result.loc[0, "account_number"], "00012345")
        self.assertEqual(result.loc[1, "account_number"], INVALID_NUMERIC_EXCEL_CELL)

        file_buffer.seek(0)
        with patch(
            "individual.services.pd.read_excel",
            side_effect=[dataframe.copy(), dataframe.copy()],
        ):
            loaded = _load_ods_with_text_columns(file_buffer)
        self.assertEqual(loaded.loc[0, "account_number"], "00012345")
        self.assertEqual(loaded.loc[1, "account_number"], INVALID_NUMERIC_EXCEL_CELL)


class PaymentDetailsImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        admin_role = Role.objects.filter(is_system=64, *Role.filter_validity()).first()
        cls.user = create_test_interactive_user(
            username=f"paydet-{uuid.uuid4().hex[:8]}",
            roles=[admin_role.id] if admin_role else [],
        )
        cls.recipient = Individual(
            first_name="Alice",
            last_name="Recipient",
            dob="1980-01-01",
            json_ext={"national_id": "NID-001", "form_number": "F-001"},
        )
        cls.recipient.save(user=cls.user)
        cls.group = Group(
            code=f"PAYMENT-{uuid.uuid4()}",
            json_ext={"form_number": "F-001", "validation_status": "VERIFIED"},
        )
        cls.group.save(user=cls.user)
        cls.membership = GroupIndividual(
            group=cls.group,
            individual=cls.recipient,
            recipient_type=GroupIndividual.RecipientType.PRIMARY,
            json_ext={"primary_worker": True},
        )
        cls.membership.save(user=cls.user)
        # Membership saves refresh the denormalized household data from the
        # head. Restore the validation result that the validation workflow
        # persists after household membership has been established.
        cls.group.json_ext["validation_status"] = "VERIFIED"
        cls.group.save(user=cls.user)

    def _create_upload(self, rows):
        upload = IndividualDataSourceUpload(
            source_name="payment-details.csv",
            source_type="individual update",
        )
        upload.save(user=self.user)
        sources = []
        for row in rows:
            source = IndividualDataSource(
                upload=upload,
                json_ext={
                    "ID": str(self.recipient.id),
                    "form_number": "F-001",
                    "nid": "NID-001",
                    "first_name": "Alicia",
                    "last_name": "Recipient",
                    "account_number": "00012345",
                    "account_name": "Alice Recipient",
                    "bank": "TNM Mpamba",
                    **row,
                },
                validations={},
                user_created=self.user,
                user_updated=self.user,
            )
            source.save(user=self.user)
            sources.append(source)
        return upload, sources

    def _validate(self, upload, sources):
        dataframe = load_dataframe(sources)
        return IndividualImportService(self.user)._validate_possible_individuals(dataframe, upload.id)

    def test_matching_nid_accepts_payment_details_and_records_name_warning(self):
        upload, sources = self._create_upload([{}])

        validated, invalid = self._validate(upload, sources)
        sources[0].refresh_from_db()

        self.assertEqual(invalid, [])
        self.assertTrue(validated[0]["validations"]["payment_details_first_name_warning"]["warning"])
        self.assertEqual(
            sources[0].json_ext.get("account_number"),
            "00012345",
        )
        self.assertEqual(sources[0].validations["validation_errors"], [])
        self.assertEqual(sources[0].validations["validation_warnings"][0]["field_name"], "first_name")

    def test_primary_recipient_change_is_explained_in_validation_summary(self):
        self.membership.json_ext = {"primary_worker": False}
        self.membership.save(user=self.user)
        proposed_recipient = Individual(
            first_name="Bob",
            last_name="New",
            dob="1985-01-01",
            json_ext={"national_id": "NID-002"},
        )
        proposed_recipient.save(user=self.user)
        GroupIndividual(
            group=self.group,
            individual=proposed_recipient,
            json_ext={"primary_worker": True},
        ).save(user=self.user)
        self.group.json_ext["validation_status"] = "VERIFIED"
        self.group.save(user=self.user)
        upload, sources = self._create_upload([{
            "ID": str(proposed_recipient.id),
            "nid": "NID-002",
            "first_name": "Bob",
            "last_name": "New",
        }])

        validated, invalid = self._validate(upload, sources)
        sources[0].refresh_from_db()
        summary = IndividualImportService._kyc_upload_summary(upload)

        self.assertEqual(invalid, [])
        change = validated[0]["validations"]["primary_recipient_change"]["details"]
        self.assertEqual(change["current_recipient"]["id"], str(self.recipient.id))
        self.assertEqual(change["proposed_recipient"]["id"], str(proposed_recipient.id))
        self.assertEqual(summary["primary_recipient_changes"], [change])
        self.assertEqual(
            sources[0].validations["validation_warnings"][-1]["field_name"],
            "primary_recipient",
        )
        upload_record = IndividualDataUploadRecords(
            data_upload=upload,
            workflow=KYC_DETAILS_WORKFLOW_NAME,
            json_ext={},
        )
        upload_record.save(user=self.user)
        with patch("tasks_management.services.TaskService.create") as create_task, patch.object(
            IndividualTaskCreatorService,
            "_IndividualTaskCreatorService__calculate_percentage_of_invalid_items",
            return_value=0,
        ):
            IndividualTaskCreatorService(self.user)._create_task(upload.id, "test.event")
        task_json_ext = create_task.call_args.args[0]["json_ext"]
        self.assertEqual(task_json_ext["primary_recipient_changes"], [change])

    def test_kyc_upload_summary_reports_rows_ready_for_approval(self):
        upload, _ = self._create_upload([{}])

        summary = IndividualImportService._kyc_upload_summary(upload)

        self.assertEqual(summary["rows_read"], 1)
        self.assertEqual(summary["records_ready_for_approval"], 1)
        self.assertEqual(summary["records_rejected"], 0)
        self.assertEqual(summary["primary_recipients_matched"], 1)
        self.assertEqual(summary["payment_accounts_submitted"], 1)
        self.assertEqual(summary["national_ids_submitted"], 1)

    def test_nid_replacement_is_accepted_for_payment_details_row(self):
        upload, sources = self._create_upload([{"nid": "NID-OTHER"}])

        validated, invalid = self._validate(upload, sources)
        sources[0].refresh_from_db()

        self.assertEqual(invalid, [])
        self.assertEqual(validated[0]["row"]["ID"], str(self.recipient.id))
        self.assertEqual(sources[0].validations["validation_errors"], [])

    def test_approved_kyc_aligns_recipient_with_verified_primary_worker(self):
        other_member = Individual(
            first_name="Other",
            last_name="Member",
            dob="1985-01-01",
            json_ext={"form_number": "F-ALIGN"},
        )
        other_member.save(user=self.user)
        worker = Individual(
            first_name="Validated",
            last_name="Worker",
            dob="1985-01-01",
            json_ext={"form_number": "F-ALIGN"},
        )
        worker.save(user=self.user)
        group = Group(
            code=f"ALIGN-{uuid.uuid4()}",
            json_ext={"form_number": "F-ALIGN", "validation_status": "VERIFIED"},
        )
        group.save(user=self.user)
        old_recipient = GroupIndividual(
            group=group,
            individual=other_member,
            recipient_type=GroupIndividual.RecipientType.PRIMARY,
        )
        old_recipient.save(user=self.user)
        worker_membership = GroupIndividual(
            group=group,
            individual=worker,
            recipient_type=GroupIndividual.RecipientType.SECONDARY,
            json_ext={"primary_worker": True},
        )
        worker_membership.save(user=self.user)
        group.json_ext["validation_status"] = "VERIFIED"
        group.save(user=self.user)
        upload = IndividualDataSourceUpload(source_name="align.csv", source_type="individual update")
        upload.save(user=self.user)
        IndividualDataSource(
            upload=upload,
            json_ext={"ID": str(worker.id)},
            validations={"validation_errors": []},
            user_created=self.user,
            user_updated=self.user,
        ).save(user=self.user)

        IndividualImportService(self.user).synchronize_kyc_primary_recipients(upload.id)

        worker_membership.refresh_from_db()
        old_recipient.refresh_from_db()
        group.refresh_from_db()
        self.assertEqual(worker_membership.recipient_type, GroupIndividual.RecipientType.PRIMARY)
        self.assertIsNone(old_recipient.recipient_type)
        self.assertEqual(group.json_ext["primary_recipient_id"], str(worker.id))

    def test_numeric_form_number_and_blank_existing_nid_are_accepted(self):
        numeric_form_number = int(f"9{uuid.uuid4().int % 10**8:08d}")
        recipient = Individual(
            first_name="Numeric",
            last_name="Recipient",
            dob="1980-01-01",
            json_ext={"form_number": numeric_form_number, "national_id": None},
        )
        recipient.save(user=self.user)
        group = Group(
            code=f"PAYMENT-{uuid.uuid4()}",
            json_ext={"form_number": numeric_form_number, "validation_status": "VERIFIED"},
        )
        group.save(user=self.user)
        GroupIndividual(
            group=group,
            individual=recipient,
            recipient_type=GroupIndividual.RecipientType.PRIMARY,
            json_ext={"primary_worker": True},
        ).save(user=self.user)
        group.json_ext["validation_status"] = "VERIFIED"
        group.save(user=self.user)
        upload = IndividualDataSourceUpload(
            source_name="numeric-form-payment-details.csv",
            source_type="individual update",
        )
        upload.save(user=self.user)
        source = IndividualDataSource(
            upload=upload,
            json_ext={
                "form_number": str(numeric_form_number),
                "nid": "NEW-NID-001",
                "first_name": "Numeric",
                "last_name": "Recipient",
                "account_number": "00012345",
                "account_name": "Numeric Recipient",
                "bank": "TNM Mpamba",
            },
            validations={},
            user_created=self.user,
            user_updated=self.user,
        )
        source.save(user=self.user)

        # This test isolates the JSON numeric/text form-number representation;
        # district scope is covered by the import-service scope tests.
        with patch("individual.services.Group.get_queryset", side_effect=lambda queryset, user: queryset):
            validated, invalid = self._validate(upload, [source])
        source.refresh_from_db()

        self.assertEqual(invalid, [])
        self.assertEqual(validated[0]["row"]["ID"], str(recipient.id))
        self.assertEqual(source.json_ext["ID"], str(recipient.id))
        self.assertEqual(source.validations["validation_errors"], [])

    def test_conflicting_duplicate_form_number_rows_are_both_rejected(self):
        upload, sources = self._create_upload([{}, {"account_number": "00098765"}])

        validated, invalid = self._validate(upload, sources)
        for source in sources:
            source.refresh_from_db()

        self.assertEqual(len(invalid), 2)
        self.assertTrue(all(
            not row["validations"]["payment_details_form_number"]["success"]
            for row in validated
        ))

    def test_duplicate_form_number_with_different_national_ids_is_rejected(self):
        upload, sources = self._create_upload([{}, {"nid": "NID-OTHER"}])

        validated, invalid = self._validate(upload, sources)

        self.assertEqual(len(invalid), 2)
        self.assertTrue(all(
            not row["validations"]["payment_details_form_number"]["success"]
            for row in validated
        ))

    def test_excel_numeric_account_number_is_rejected(self):
        upload, sources = self._create_upload([{"account_number": 12345}])

        validated, invalid = self._validate(upload, sources)
        sources[0].refresh_from_db()

        self.assertEqual(len(invalid), 1)
        self.assertFalse(validated[0]["validations"]["payment_details_account_number"]["success"])

    def test_kyc_upload_can_force_maker_checker_when_module_setting_is_disabled(self):
        with patch("individual.apps.IndividualConfig.enable_maker_checker_for_individual_update", False), \
                patch("individual.services.IndividualTaskCreatorService") as task_creator:
            IndividualImportService(self.user).create_task_with_update_valid_items(
                uuid.uuid4(),
                force_maker_checker=True,
            )

        task_creator.return_value.create_task_with_update_valid_items.assert_called_once()
