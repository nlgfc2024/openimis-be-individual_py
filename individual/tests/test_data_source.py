import json
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from individual.data_source import benefit_plan_data_source, data_source_q, record_data_source
from individual.models import GroupIndividual, Individual
from individual.tests.test_helpers import create_group, create_individual


class DataSourceHelpersTest(SimpleTestCase):
    def test_plan_source_defaults_to_msr(self):
        self.assertEqual(benefit_plan_data_source(SimpleNamespace(json_ext={})), "ubr")
        self.assertEqual(benefit_plan_data_source(SimpleNamespace(json_ext=None)), "ubr")

    def test_plan_source_reads_json_ext_stored_as_text(self):
        plan = SimpleNamespace(json_ext=json.dumps({"data_source": "pwp"}))
        self.assertEqual(benefit_plan_data_source(plan), "pwp")

    def test_record_source_is_its_tag_or_the_default(self):
        self.assertEqual(record_data_source(SimpleNamespace(json_ext={"data_source": "sctp"})), "sctp")
        self.assertEqual(record_data_source(SimpleNamespace(json_ext=None)), "ubr")


class DataSourceRecordsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def _individual(self, data_source=None):
        json_ext = {"data_source": data_source} if data_source else {}
        return create_individual(self.user.username, {"json_ext": json_ext})

    def test_household_keeps_its_source_through_head_changes(self):
        # Saving a membership re-copies the household's json_ext from its head
        group = create_group(self.user.username, {"code": "47407473", "json_ext": {"data_source": "sctp"}})
        GroupIndividual(group=group, individual=self._individual("sctp"), role=None).save(username=self.user.username)
        group.refresh_from_db()
        self.assertEqual(group.json_ext["data_source"], "sctp")  # no head

        head = self._individual("ubr")  # a head tagged otherwise must not overwrite it
        GroupIndividual(group=group, individual=head, role=GroupIndividual.Role.HEAD).save(username=self.user.username)
        group.refresh_from_db()
        self.assertEqual(group.json_ext["data_source"], "sctp")

    def test_data_source_q_matches_untagged_only_for_default(self):
        untagged, pwp = self._individual(), self._individual("pwp")
        ids = {untagged.id, pwp.id}

        default = set(Individual.objects.filter(data_source_q("ubr"), id__in=ids).values_list("id", flat=True))
        other = set(Individual.objects.filter(data_source_q("pwp"), id__in=ids).values_list("id", flat=True))

        self.assertEqual(default, {untagged.id})
        self.assertEqual(other, {pwp.id})
