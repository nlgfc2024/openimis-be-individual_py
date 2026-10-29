import io
import json
from unittest.mock import patch

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from individual.apps import IndividualConfig
from individual.services import IndividualImportService

SCHEMA = json.dumps({"properties": {
    "type_of_business": {"type": "string"},
    "household_mobile_number": {"type": "string"},
    "ubr_id": {"type": "integer"},
}})
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@patch.object(IndividualConfig, "individual_schema", SCHEMA)
class ImportLoaderTest(SimpleTestCase):
    def _stored(self, content, content_type):
        dataframe = IndividualImportService(None)._load_import_file(
            SimpleUploadedFile("import", content, content_type=content_type),
        )
        # As _save_data_source stores each row
        return json.loads(dataframe.iloc[0].to_json())

    def test_csv_keeps_declared_strings_as_text(self):
        stored = self._stored(
            b"type_of_business,household_mobile_number,ubr_id,location_code,undeclared\n001,0991234567,123,0101,7\n",
            "text/csv",
        )

        self.assertEqual(stored["type_of_business"], "001")
        self.assertEqual(stored["household_mobile_number"], "0991234567")
        self.assertEqual(stored["location_code"], "0101")
        # Columns not declared as strings are still inferred
        self.assertEqual((stored["ubr_id"], stored["undeclared"]), (123, 7))

    def test_blank_declared_strings_stay_empty(self):
        stored = self._stored(b"type_of_business,ubr_id\n,123\n", "text/csv")

        self.assertIsNone(stored["type_of_business"])

    def test_excel_keeps_declared_strings_as_text(self):
        buffer = io.BytesIO()
        pd.DataFrame({"type_of_business": ["001"], "household_mobile_number": [991234567], "ubr_id": [123]}) \
            .to_excel(buffer, index=False)

        stored = self._stored(buffer.getvalue(), XLSX)

        self.assertEqual(stored["type_of_business"], "001")
        self.assertEqual(stored["household_mobile_number"], "991234567")
        self.assertEqual(stored["ubr_id"], 123)
