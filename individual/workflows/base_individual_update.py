import logging

import pandas as pd

from core.models import User
from individual.data_source import (
    KYC_DETAILS_WORKFLOW_NAME,
    optional_payment_details_fields,
    payment_details_context_columns,
    payment_details_required_columns,
)
from individual.models import IndividualDataUploadRecords
from individual.services import IndividualImportService
from individual.workflows.utils import DataUpdateWorkflow, PythonWorkflowHandlerException

logger = logging.getLogger(__name__)

def process_update_individuals_workflow(user_uuid, upload_uuid):
    _process_update_workflow(user_uuid, upload_uuid)


def process_kyc_details_workflow(user_uuid, upload_uuid):
    _process_update_workflow(
        user_uuid,
        upload_uuid,
        require_payment_details=True,
        require_approval=True,
    )


def _process_update_workflow(
        user_uuid,
        upload_uuid,
        require_payment_details=False,
        require_approval=False):
    user = User.objects.get(id=user_uuid)
    service = DataUpdateWorkflow(upload_uuid, user_uuid, require_approval=require_approval)
    if require_payment_details:
        _validate_kyc_details_file(service.df)
    else:
        service.validate_dataframe_headers(True)
    service.execute(update_sql)
    IndividualImportService(user).synchronize_data_for_reporting(upload_uuid)


def _validate_kyc_details_file(dataframe):
    required_columns = payment_details_required_columns()
    missing_columns = required_columns - set(dataframe.columns)
    if missing_columns:
        raise PythonWorkflowHandlerException(
            "KYC upload is missing required columns: {}. Recognized columns: {}. "
            "Use the KYC template or the KYC export column headings.".format(
                ", ".join(sorted(missing_columns)),
                ", ".join(sorted(map(str, dataframe.columns))),
            )
        )

    allowed_columns = (
        required_columns
        | optional_payment_details_fields()
        | payment_details_context_columns()
        | {"dob", "id", "ID"}
    )
    unexpected_columns = set(dataframe.columns) - allowed_columns
    if unexpected_columns:
        raise PythonWorkflowHandlerException(
            "KYC upload contains unsupported columns: {}".format(", ".join(sorted(unexpected_columns)))
        )

    if dataframe.empty:
        raise PythonWorkflowHandlerException(
            "KYC upload contains no data rows."
        )

    rows_with_missing_values = []
    for row_number, (_, row) in enumerate(dataframe.iterrows(), start=2):
        missing_fields = [
            field
            for field in sorted(required_columns)
            for value in [row.get(field)]
            if pd.isna(value) or not str(value).strip()
        ]
        if missing_fields:
            identifiers = ", ".join(
                "{}={}".format(field, _safe_kyc_identifier(row.get(field)))
                for field in ("form_number", "nid", "account_number")
            )
            rows_with_missing_values.append(
                "row {}: {} [{}]".format(
                    row_number, ", ".join(missing_fields), identifiers,
                )
            )

    if rows_with_missing_values:
        # KYC files are Phase-sized and users need the actual failing records,
        # not a row-count summary, in order to correct the upload.
        displayed_rows = rows_with_missing_values[:100]
        remaining_count = len(rows_with_missing_values) - len(displayed_rows)
        details = "; ".join(displayed_rows)
        if remaining_count:
            details += "; and {} additional invalid record(s)".format(remaining_count)
        raise PythonWorkflowHandlerException(
            "KYC upload has blank required values in these records: {}.".format(details)
        )


def _safe_kyc_identifier(value):
    if pd.isna(value):
        return "<blank>"
    value = str(value).strip()
    return value or "<blank>"


def is_kyc_details_upload(upload_uuid):
    return IndividualDataUploadRecords.objects.filter(
        data_upload_id=upload_uuid,
        workflow=KYC_DETAILS_WORKFLOW_NAME,
        is_deleted=False,
    ).exists()


update_sql = """
CREATE OR REPLACE FUNCTION filter_jsonb(data jsonb, schema jsonb)
RETURNS jsonb AS $$
DECLARE
  key text;
  value text;
  result jsonb := '{}';
BEGIN
  FOR key, value IN SELECT * FROM jsonb_each_text(data)
  LOOP
    IF schema ? key THEN
      result := result || jsonb_build_object(key, value);
    END IF;
  END LOOP;
  RETURN result;
END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
            CREATE TYPE failing_entry_individual_upload AS (
            uuids TEXT[],
            ordinals INT[]
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;

DO $$
declare
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    failing_entries UUID[];
    json_schema jsonb;

    failing_entries_invalid_id failing_entry_individual_upload;
BEGIN
    -- existing code for finding failing_entries_first_name, failing_entries_last_name, failing_entries_dob
    -- Check if any entries have invalid Json_ext according to the schema
    SELECT ARRAY_AGG("UUID") AS "UUID", ARRAY_AGG("ordinal") AS "ORDINALS" INTO failing_entries_invalid_id
    FROM (
        SELECT ("Json_ext" ->> 'ID')::UUID as individual_uuid,  row_number() OVER (ORDER BY "UUID") AS ordinal, "UUID"
        FROM individual_individualdatasource
        WHERE upload_id = current_upload_id
    ) AS f
    WHERE not individual_uuid in (select "UUID" from individual_individual ii);

    IF failing_entries_invalid_id IS NOT NULL THEN
        UPDATE individual_individualdatasourceupload
        SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                            'error', 'Invalid entries',
                            'timestamp', NOW()::text,
                            'upload_id', current_upload_id::text,
                            'failing_entries_invalid_id', failing_entries_invalid_id
                        ))
        WHERE "UUID" = current_upload_id;

       update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
    -- If no invalid entries, then proceed with the data manipulation
    ELSE
        begin
          with updated_individuals as ( UPDATE individual_individual
            SET first_name = CASE
                    WHEN (
                        NULLIF(BTRIM(f."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'bank'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'payment_provider_code'), '') IS NOT NULL
                    ) THEN first_name
                    ELSE COALESCE(f."Json_ext"->>'first_name', first_name)
                END,
            last_name = CASE
                    WHEN (
                        NULLIF(BTRIM(f."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'bank'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'payment_provider_code'), '') IS NOT NULL
                    ) THEN last_name
                    ELSE COALESCE(f."Json_ext"->>'last_name', last_name)
                END,
            dob = CASE
                    WHEN (
                        NULLIF(BTRIM(f."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'bank'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'payment_provider_code'), '') IS NOT NULL
                    ) THEN dob
                    ELSE COALESCE(to_date(f."Json_ext"->>'dob', 'YYYY-MM-DD'), dob)
                END,
            location_id = CASE
                    WHEN (
                        NULLIF(BTRIM(f."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'bank'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'payment_provider_code'), '') IS NOT NULL
                    )
                    THEN individual_individual.location_id
                    ELSE loc."LocationId"
                END,
            "DateUpdated" = NOW(),
            "Json_ext" = CASE
                    WHEN (
                        NULLIF(BTRIM(f."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'bank'), '') IS NOT NULL
                        OR NULLIF(BTRIM(f."Json_ext"->>'payment_provider_code'), '') IS NOT NULL
                    )
                    THEN COALESCE(individual_individual."Json_ext", '{}'::jsonb) || jsonb_build_object(
                        'account_number', f."Json_ext"->>'account_number',
                        'account_name', f."Json_ext"->>'account_name',
                        'bank', f."Json_ext"->>'bank',
                        'payment_provider_code', COALESCE(
                            NULLIF(f."Json_ext"->>'payment_provider_code', ''),
                            individual_individual."Json_ext"->>'payment_provider_code'
                        )
                    )
                    ELSE COALESCE(f."Json_ext", '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
                        'account_number', COALESCE(
                            NULLIF(f."Json_ext"->>'account_number', ''),
                            individual_individual."Json_ext"->>'account_number'
                        ),
                        'account_name', COALESCE(
                            NULLIF(f."Json_ext"->>'account_name', ''),
                            individual_individual."Json_ext"->>'account_name'
                        ),
                        'bank', COALESCE(
                            NULLIF(f."Json_ext"->>'bank', ''),
                            individual_individual."Json_ext"->>'bank'
                        ),
                        'payment_provider_code', COALESCE(
                            NULLIF(f."Json_ext"->>'payment_provider_code', ''),
                            individual_individual."Json_ext"->>'payment_provider_code'
                        )
                    ))
                END
            FROM individual_individualdatasource f
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationName" = f."Json_ext"->>'location_name'
                    AND loc."LocationCode" = f."Json_ext"->>'location_code'
                    AND loc."LocationType"='V'
                    AND loc."ValidityTo" IS NULL
            WHERE individual_individual."UUID" = (f."Json_ext" ->> 'ID')::UUID
            AND (
                NOT (
                    NULLIF(BTRIM(f."Json_ext"->>'account_number'), '') IS NOT NULL
                    OR NULLIF(BTRIM(f."Json_ext"->>'account_name'), '') IS NOT NULL
                    OR NULLIF(BTRIM(f."Json_ext"->>'bank'), '') IS NOT NULL
                    OR NULLIF(BTRIM(f."Json_ext"->>'payment_provider_code'), '') IS NOT NULL
                )
                OR f.validations ->> 'validation_errors' = '[]'
            )
            returning individual_individual."UUID", f."UUID" as "individualdatasource_id")

            UPDATE individual_individualdatasource
      SET individual_id = u."UUID"
      FROM updated_individuals u
      WHERE upload_id=current_upload_id
        and individual_individualdatasource.individual_id is null
        and "isDeleted"=False
        and individual_individualdatasource."UUID" = u.individualdatasource_id;


            update individual_individualdatasourceupload set status='PARTIAL_SUCCESS', error='{}' where "UUID" = current_upload_id;
            EXCEPTION
              WHEN OTHERS then

              update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
                  UPDATE individual_individualdatasourceupload
                  SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                                      'error', SQLERRM,
                                      'timestamp', NOW()::text,
                                      'upload_id', current_upload_id::text
                                  ))
                  WHERE "UUID" = current_upload_id;
                END;
        END IF;
        end $$
        """
