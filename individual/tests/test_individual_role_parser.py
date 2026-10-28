from django.test import SimpleTestCase

from individual.models import GroupIndividual
from individual.signals.on_validation_import_valid_items import BaseGroupColumnAggregationClass


class IndividualRoleParserTest(SimpleTestCase):
    parse = staticmethod(BaseGroupColumnAggregationClass._individual_role_parser)

    def test_missing_or_empty_role_is_none(self):
        self.assertIsNone(self.parse(None))
        self.assertIsNone(self.parse(""))

    def test_known_role_in_any_case(self):
        self.assertEqual(self.parse("head"), GroupIndividual.Role.HEAD)
        self.assertEqual(self.parse("HEAD"), GroupIndividual.Role.HEAD)

    def test_unknown_role_is_none(self):
        self.assertIsNone(self.parse("NEIGHBOUR"))
