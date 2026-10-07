"""Run directly; uses only an isolated in-memory database."""
import sys
import types
from unittest import TestCase
from unittest.mock import patch
from django.conf import settings
settings.configure(INSTALLED_APPS=[], DATABASES={"default":{"ENGINE":"django.db.backends.sqlite3","NAME":":memory:"}}, USE_TZ=True, TIME_ZONE="UTC", SECRET_KEY="test")
import django
django.setup()
from django.db import models, connection

class Household(models.Model):
    json_ext = models.JSONField(default=dict)
    class Meta:
        app_label = "quota_checks"

class Person(models.Model):
    dob = models.DateField()
    json_ext = models.JSONField(default=dict)
    is_deleted = models.BooleanField(default=False)
    class Meta:
        app_label = "quota_checks"

class Membership(models.Model):
    group = models.ForeignKey(Household, on_delete=models.CASCADE)
    individual = models.ForeignKey(Person, on_delete=models.CASCADE)
    role = models.CharField(max_length=30)
    is_deleted = models.BooleanField(default=False)
    class Meta:
        app_label = "quota_checks"

class RuleChecks(TestCase):
    @classmethod
    def setUpClass(cls):
        with connection.schema_editor() as editor:
            for model in (Household, Person, Membership): editor.create_model(model)
        fake = types.ModuleType("individual.models")
        fake.GroupIndividual = Membership
        cls.patcher = patch.dict(sys.modules, {"individual.models":fake})
        cls.patcher.start()
    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
    def setUp(self):
        Membership.objects.all().delete()
        Person.objects.all().delete()
        Household.objects.all().delete()

from types import SimpleNamespace
from datetime import date
from unittest import main
from django.core.exceptions import ValidationError
from individual.gender_quota import apply_gender_quota, quota_counts
from social_protection.upg_options import upg_gender_options


class GenderQuotaChecks(RuleChecks):
    def head(self, gender):
        household = Household.objects.create()
        person = Person.objects.create(dob=date(1980, 1, 1), json_ext={"gender": gender})
        Membership.objects.create(group=household, individual=person, role="HEAD")
        return household

    def select_quota(self, maximum=10, current=0, female=60, male=40, materialize=True):
        plan = SimpleNamespace(json_ext={}, max_beneficiaries=maximum)
        return apply_gender_quota(Household.objects.all(), plan, "ACTIVE", current,
                                 {"female_percentage": female, "male_percentage": male}, materialize)

    def test_remaining_places_split_and_same_preview(self):
        females = [self.head("F").pk for _ in range(10)]
        males = [self.head("M").pk for _ in range(10)]
        selected, metadata = self.select_quota(current=5)
        ids = list(selected.values_list("id", flat=True))
        self.assertEqual(ids, females[:3] + males[:2])
        self.assertEqual(metadata["will_enrol"], 5)
        preview, meta = self.select_quota(current=5, materialize=False)
        self.assertEqual(list(preview.values_list("id", flat=True)), ids)
        self.assertEqual(meta["will_enrol"], 5)

    def test_shortfall_not_redistributed(self):
        self.head("F")
        for _ in range(10): self.head("M")
        selected, metadata = self.select_quota()
        self.assertEqual(selected.count(), 5)
        self.assertEqual(metadata["will_enrol"], 5)

    def test_zero_remaining_and_zero_percentage(self):
        self.head("F")
        self.head("M")
        self.assertEqual(self.select_quota(current=10)[0].count(), 0)
        self.assertEqual(self.select_quota(maximum=1, female=0, male=100)[0].count(), 1)
        self.assertEqual(quota_counts(3, 50), (2, 1))

    def test_invalid_percentages_and_missing_capacity(self):
        for args in ({"female":60,"male":60}, {"female":-1,"male":101}, {"maximum":None}):
            with self.assertRaises(ValidationError): self.select_quota(**args)

    def test_conflicting_heads_rejected(self):
        h = self.head("F")
        p = Person.objects.create(dob=date(1980,1,1),json_ext={"gender":"M"})
        Membership.objects.create(group=h,individual=p,role="HEAD")
        with self.assertRaises(ValidationError): self.select_quota()

    def test_options_are_configured(self):
        plan = SimpleNamespace(json_ext={"upg_head_gender_options":["FEMALE","BOTH"]})
        self.assertEqual(upg_gender_options(plan), ["FEMALE","BOTH"])
        plan.json_ext["upg_head_gender_options"] = ["OTHER"]
        with self.assertRaises(ValidationError): upg_gender_options(plan)


if __name__ == "__main__":
    main(verbosity=2)
