from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from individual.custom_filters import GroupCustomFilterWizard, IndividualCustomFilterWizard
from individual.enrollment_location import district_from_condition
from individual.services import _validate_operator_filters


class DistrictFilterContractTest(SimpleTestCase):
    def test_only_exact_integer_is_advertised(self):
        definition = namedtuple("Definition", "field filter type")
        original = [definition("district_id", ["exact", "gt"], "integer"),
                    definition("validation_status", ["exact"], "string")]
        with patch.object(IndividualCustomFilterWizard, "load_definition", return_value=original):
            result = GroupCustomFilterWizard().load_definition(definition)
        self.assertEqual(result[0], definition("district_id", ["exact"], "integer"))
        self.assertEqual(result[1], original[1])

    def test_unrelated_filter_is_not_interpreted_as_location(self):
        self.assertIsNone(district_from_condition('district__exact__string="A"'))

    def test_malformed_district_conditions_are_rejected_before_querying(self):
        for condition in (
            "district_id__exact__integer=", "district_id__exact__integer=null",
            "district_id__exact__integer=0", "district_id__exact__integer=-1",
            "district_id__exact__integer=1.5", "district_id__exact__integer=2147483648",
            "district_id__exact__integer=1=2", "district_id__gt__integer=1",
            "district_id__exact__string=1", "district_id__integer=1",
        ):
            with self.subTest(condition=condition), self.assertRaises(ValidationError):
                district_from_condition(condition)

    @patch("individual.enrollment_location.Location.objects.filter")
    def test_validates_active_r_location(self, query):
        district = object()
        query.return_value.first.return_value = district
        self.assertIs(district_from_condition("district_id__exact__integer=123"), district)
        self.assertEqual(query.call_args.kwargs, {"id": 123, "type": "R"})
        query.return_value.first.return_value = None
        with self.assertRaisesMessage(ValidationError, "active District"):
            district_from_condition("district_id__exact__integer=123")

    def test_schema_and_group_type_are_required(self):
        condition = "district_id__exact__integer=123"
        plan = SimpleNamespace(type="GROUP", beneficiary_data_schema={
            "properties": {"validation_status": {"type": "string"}}
        })
        with self.assertRaisesMessage(ValidationError, "not allowed"):
            _validate_operator_filters([condition], plan)
        plan.type = "INDIVIDUAL"
        plan.beneficiary_data_schema["properties"]["district_id"] = {"type": "integer"}
        with self.assertRaisesMessage(ValidationError, "only supported for households"):
            _validate_operator_filters([condition], plan)
