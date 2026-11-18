import logging

from core.models import User
from individual.workflows.utils import SqlProcedurePythonWorkflow
from individual.services import IndividualImportService
from individual.workflows.base_individual_update import _validate_kyc_details_file, is_kyc_details_upload

logger = logging.getLogger(__name__)


def process_update_valid_individuals_workflow(user_uuid, upload_uuid, accepted=None):
    user = User.objects.get(id=user_uuid)
    IndividualImportService(user).validate_payment_details_upload(upload_uuid)
    service = SqlProcedurePythonWorkflow(upload_uuid, user_uuid, accepted)
    if is_kyc_details_upload(upload_uuid):
        _validate_kyc_details_file(service.df)
    else:
        service.validate_dataframe_headers(True)
    if isinstance(accepted, list):
        service.execute(upload_sql_partial, [upload_uuid, user_uuid, accepted])
    else:
        service.execute(upload_sql, [upload_uuid, user_uuid])
    import_service = IndividualImportService(user)
    if is_kyc_details_upload(upload_uuid):
        import_service.synchronize_kyc_primary_recipients(upload_uuid, accepted)
    import_service.synchronize_data_for_reporting(upload_uuid)


upload_sql = """
-- Setup
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

-- Update procedure
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
            -- Update individual_individual
          with updated_individuals as ( UPDATE individual_individual
            SET first_name = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    ) THEN first_name
                    ELSE COALESCE(ids."Json_ext"->>'first_name', first_name)
                END,
                last_name = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    ) THEN last_name
                    ELSE COALESCE(ids."Json_ext"->>'last_name', last_name)
                END,
                dob = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    ) THEN dob
                    ELSE COALESCE(to_date(ids."Json_ext"->>'dob', 'YYYY-MM-DD'), dob)
                END,
                location_id = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    )
                    THEN individual_individual.location_id
                    ELSE loc."LocationId"
                END,
                "DateUpdated" = NOW(),
                "Json_ext" = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    )
                    THEN COALESCE(individual_individual."Json_ext", '{}'::jsonb) || jsonb_build_object(
                        'account_number', ids."Json_ext"->>'account_number',
                        'account_name', ids."Json_ext"->>'account_name',
                        'bank', ids."Json_ext"->>'bank',
                        'national_id', COALESCE(
                            NULLIF(ids."Json_ext"->>'nid', ''),
                            individual_individual."Json_ext"->>'national_id'
                        )
                    )
                    ELSE COALESCE(ids."Json_ext", '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
                        'account_number', COALESCE(
                            NULLIF(ids."Json_ext"->>'account_number', ''),
                            individual_individual."Json_ext"->>'account_number'
                        ),
                        'account_name', COALESCE(
                            NULLIF(ids."Json_ext"->>'account_name', ''),
                            individual_individual."Json_ext"->>'account_name'
                        ),
                        'bank', COALESCE(
                            NULLIF(ids."Json_ext"->>'bank', ''),
                            individual_individual."Json_ext"->>'bank'
                        )
                    ))
                END
            FROM individual_individualdatasource ids
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationName" = ids."Json_ext"->>'location_name'
                    AND loc."LocationCode" = ids."Json_ext"->>'location_code'
                    AND loc."LocationType"='V'
                    AND loc."ValidityTo" IS NULL
            WHERE individual_individual."UUID" = (ids."Json_ext" ->> 'ID')::UUID
            AND ids.upload_id = current_upload_id
            AND validations ->> 'validation_errors' = '[]'
            returning individual_individual."UUID", ids."UUID" as "individualdatasource_id")

            UPDATE individual_individualdatasource
      SET individual_id = u."UUID"
      FROM updated_individuals u
      WHERE upload_id=current_upload_id
        and individual_individualdatasource.individual_id is null
        and "isDeleted"=False
        and individual_individualdatasource."UUID" = u.individualdatasource_id
        and validations ->> 'validation_errors' = '[]';

            -- Change status to SUCCESS if no invalid items, change to PARTIAL_SUCCESS otherwise
            UPDATE individual_individualdatasourceupload
            SET
                status = CASE
                    WHEN (
                        SELECT count(*)
                        FROM individual_individualdatasource
                        WHERE upload_id=current_upload_id
                            AND "isDeleted"=FALSE
                            AND validations ->> 'validation_errors' = '[]'
                    ) = (
                        SELECT count(*)
                        FROM individual_individualdatasource
                        WHERE upload_id=current_upload_id
                            AND "isDeleted"=FALSE
                    ) THEN 'SUCCESS'
                    ELSE 'PARTIAL_SUCCESS'
                END,
                error = '{}'
            WHERE "UUID" = current_upload_id;
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

upload_sql_partial = """
-- Setup
DO $$ BEGIN
            CREATE TYPE failing_entry_individual_upload AS (
            uuids TEXT[],
            ordinals INT[]
            );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
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

DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    failing_entries UUID[];
    json_schema jsonb;
    accepted UUID[] := %s::UUID[];
    failing_entries_invalid_id failing_entry_individual_upload;
BEGIN
    -- existing code for finding failing_entries_first_name, failing_entries_last_name, failing_entries_dob
    SELECT ARRAY_AGG("UUID") AS "UUID", ARRAY_AGG("ordinal") AS "ORDINALS" INTO failing_entries_invalid_id
    FROM (
        SELECT ("Json_ext" ->> 'ID')::UUID as individual_uuid,  row_number() OVER (ORDER BY "UUID") AS ordinal, "UUID"
        FROM individual_individualdatasource
        WHERE upload_id = current_upload_id
        AND ("UUID" = ANY(accepted)) /* Filter based on accepted if not NULL */
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

       UPDATE individual_individualdatasourceupload SET status='FAIL' WHERE "UUID" = current_upload_id;
    ELSE
      BEGIN
          WITH updated_individuals AS (
            UPDATE individual_individual
            SET first_name = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    ) THEN first_name
                    ELSE COALESCE(ids."Json_ext"->>'first_name', first_name)
                END,
                last_name = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    ) THEN last_name
                    ELSE COALESCE(ids."Json_ext"->>'last_name', last_name)
                END,
                dob = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    ) THEN dob
                    ELSE COALESCE(to_date(ids."Json_ext"->>'dob', 'YYYY-MM-DD'), dob)
                END,
                location_id = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    )
                    THEN individual_individual.location_id
                    ELSE loc."LocationId"
                END,
                "DateUpdated" = NOW(),
                "Json_ext" = CASE
                    WHEN (
                        NULLIF(BTRIM(ids."Json_ext"->>'account_number'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'account_name'), '') IS NOT NULL
                        OR NULLIF(BTRIM(ids."Json_ext"->>'bank'), '') IS NOT NULL
                    )
                    THEN COALESCE(individual_individual."Json_ext", '{}'::jsonb) || jsonb_build_object(
                        'account_number', ids."Json_ext"->>'account_number',
                        'account_name', ids."Json_ext"->>'account_name',
                        'bank', ids."Json_ext"->>'bank',
                        'national_id', COALESCE(
                            NULLIF(ids."Json_ext"->>'nid', ''),
                            individual_individual."Json_ext"->>'national_id'
                        )
                    )
                    ELSE COALESCE(ids."Json_ext", '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
                        'account_number', COALESCE(
                            NULLIF(ids."Json_ext"->>'account_number', ''),
                            individual_individual."Json_ext"->>'account_number'
                        ),
                        'account_name', COALESCE(
                            NULLIF(ids."Json_ext"->>'account_name', ''),
                            individual_individual."Json_ext"->>'account_name'
                        ),
                        'bank', COALESCE(
                            NULLIF(ids."Json_ext"->>'bank', ''),
                            individual_individual."Json_ext"->>'bank'
                        )
                    ))
                END
            FROM individual_individualdatasource ids
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationName" = ids."Json_ext"->>'location_name'
                    AND loc."LocationCode" = ids."Json_ext"->>'location_code'
                    AND loc."LocationType"='V'
                    AND loc."ValidityTo" IS NULL
            WHERE individual_individual."UUID" = (ids."Json_ext" ->> 'ID')::UUID
            AND ids.upload_id = current_upload_id
            AND (ids."UUID" = ANY(accepted))
            AND validations ->> 'validation_errors' = '[]'
            RETURNING individual_individual."UUID", ids."UUID" as individualdatasource_id)

          UPDATE individual_individualdatasource
          SET individual_id = u."UUID"
          FROM updated_individuals u
          WHERE upload_id = current_upload_id
            AND individual_individualdatasource.individual_id IS NULL
            AND "isDeleted" = False
            AND individual_individualdatasource."UUID" = u.individualdatasource_id
            AND (individual_individualdatasource."UUID" = ANY(accepted))
            AND validations ->> 'validation_errors' = '[]';

          UPDATE individual_individualdatasourceupload
          SET
              status = CASE
                  WHEN COALESCE(array_length(accepted, 1), 0) = 0
                      OR EXISTS (
                      SELECT 1
                      FROM individual_individualdatasource
                      WHERE upload_id = current_upload_id
                          AND "isDeleted" = FALSE
                          AND individual_id IS NULL
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM individual_individualdatasource
                          WHERE upload_id = current_upload_id
                              AND "isDeleted" = TRUE
                      ) THEN 'PARTIAL_SUCCESS'
                  ELSE 'SUCCESS'
              END,
              error = CASE
                  WHEN COALESCE(array_length(accepted, 1), 0) = 0
                      OR EXISTS (
                          SELECT 1
                          FROM individual_individualdatasource
                          WHERE upload_id = current_upload_id
                              AND "isDeleted" = FALSE
                              AND individual_id IS NULL
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM individual_individualdatasource
                          WHERE upload_id = current_upload_id
                              AND "isDeleted" = TRUE
                      ) THEN jsonb_build_object(
                      'error', CASE
                          WHEN COALESCE(array_length(accepted, 1), 0) = 0
                              THEN 'No rows were approved.'
                          ELSE 'Some rows were not applied or were rejected during approval. Review the task and download invalid items for validation details.'
                      END,
                      'unprocessed_items', (
                          SELECT count(*)
                          FROM individual_individualdatasource
                          WHERE upload_id = current_upload_id
                              AND (
                                  "isDeleted" = TRUE
                                  OR individual_id IS NULL
                              )
                      )
                  )
                  ELSE '{}'
              END
          WHERE "UUID" = current_upload_id;

          EXCEPTION
            WHEN OTHERS THEN
              UPDATE individual_individualdatasourceupload SET status = 'FAIL' WHERE "UUID" = current_upload_id;
              UPDATE individual_individualdatasourceupload
              SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                              'error', SQLERRM,
                              'timestamp', NOW()::text,
                              'upload_id', current_upload_id::text
                          ))
              WHERE "UUID" = current_upload_id;
      END;
    END IF;

END $$
"""
