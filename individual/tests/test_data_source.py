import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from individual.data_source import (
    benefit_plan_data_source, data_source_expression, data_source_q, record_data_source,
)
from individual.models import GroupIndividual, Individual
from individual.signals.on_validation_import_valid_items import BaseGroupColumnAggregationClass
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

    def test_blank_tags_belong_to_the_default_source(self):
        # Blank import cells arrive as JSON null or ""; both resolve to the default source
        blanks = [create_individual(self.user.username, {"json_ext": {"data_source": value}}) for value in (None, "")]
        bare = create_individual(self.user.username, {"json_ext": {}})
        ids = {record.id for record in blanks + [bare]}

        self.assertEqual({record_data_source(record) for record in blanks + [bare]}, {"ubr"})
        self.assertEqual(set(Individual.objects.filter(data_source_q("ubr"), id__in=ids).values_list("id", flat=True)), ids)
        annotated = Individual.objects.filter(id__in=ids).annotate(source=data_source_expression())
        self.assertEqual(set(annotated.values_list("source", flat=True)), {"ubr"})


class ImportedHouseholdSourceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api()

    def _member(self, group_code, data_source=None):
        json_ext = {"group_code": group_code, **({"data_source": data_source} if data_source else {})}
        return create_individual(self.user.username, {"json_ext": json_ext})

    def _group_payloads(self, members, aggregation_column="group_code"):
        event = BaseGroupColumnAggregationClass("w.f", None, None, self.user)
        event.set_group_aggregation_column(aggregation_column)
        event.individuals = Individual.objects.filter(id__in=[member.id for member in members])
        event.grouped_individuals = event._get_grouped_individuals()
        payloads = []
        with patch.object(event, "_create_group_data_source", side_effect=payloads.append):
            if aggregation_column == "group_code":
                event._create_or_update_groups_using_group_code()
            else:
                event._create_groups()
        return payloads

    @staticmethod
    def _members_by_source(payloads):
        return {
            payload.get("json_ext", {}).get("data_source"): {row["individual_id"] for row in payload["individuals_data"]}
            for payload in payloads
        }

    def test_one_code_in_two_sources_makes_two_households(self):
        pwp, sctp, untagged = self._member("123", "pwp"), self._member("123", "sctp"), self._member("123")

        for column in ("group_code", "custom_code"):
            members = [pwp, sctp, untagged]
            if column == "custom_code":
                for member in members:
                    member.json_ext["custom_code"] = "123"
                    member.save(username=self.user.username)
            self.assertEqual(self._members_by_source(self._group_payloads(members, column)), {
                "pwp": {str(pwp.id)}, "sctp": {str(sctp.id)}, "ubr": {str(untagged.id)},
            })

    def test_existing_household_is_matched_only_within_its_source(self):
        existing = create_group(self.user.username, {"code": "123", "json_ext": {"data_source": "pwp"}})

        payloads = self._group_payloads([self._member("123", "pwp"), self._member("123", "sctp")])

        self.assertEqual({payload.get("id") for payload in payloads}, {str(existing.id), None})
        self.assertEqual(next(p for p in payloads if not p.get("id"))["json_ext"], {"data_source": "sctp"})
