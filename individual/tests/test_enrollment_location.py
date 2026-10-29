from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from individual.custom_filters import GroupCustomFilterWizard, IndividualCustomFilterWizard
from individual.enrollment_location import district_from_condition
from individual.services import _validate_operator_filters

from core.test_helpers import LogInHelper, create_manager_role, create_test_interactive_user, create_test_language
from individual.models import Group
from individual.services import build_group_enrollment_queryset
from individual.tests.test_helpers import create_group, create_sp_role
from location.models import Location, UserDistrict, cache, free_cache_for_user
from social_protection.tests.test_helpers import create_benefit_plan


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


@override_settings(ROW_SECURITY=True)
class DistrictEnrollmentQueryTest(TestCase):
    def setUp(self):
        cache.clear()
        create_test_language(code="en")
        self.admin = LogInHelper().get_or_create_user_api()
        self.user = create_test_interactive_user(
            username="enrollment-district-user", password="Test-only-123!",
            roles=[create_sp_role(self.admin).id],
        )
        self.a = self.location("R", "ENR-A")
        self.b = self.location("R", "ENR-B")
        self.ta = self.location("D", "ENR-TA", self.a)
        self.other_ta = self.location("D", "ENR-OTHER-TA", self.a)
        self.gvh = self.location("W", "ENR-GVH", self.ta)
        self.village = self.location("V", "ENR-V", self.gvh)
        self.direct = self.group(self.a)
        self.selected = self.group(self.village)
        self.pending = self.group(self.village, "PENDING")
        self.other = self.group(self.b)
        self.sibling = self.group(self.other_ta)
        self.missing = self.group(None)
        UserDistrict.objects.create(user=self.user.i_user, location=self.ta, audit_user_id=-1)
        free_cache_for_user(self.user.i_user.id)
        self.plan = create_benefit_plan(self.admin.username, payload_override={
            "type": "GROUP",
            "beneficiary_data_schema": {"properties": {
                "district_id": {"type": "integer"}, "validation_status": {"type": "string"}
            }},
            "json_ext": {"advanced_criteria": {"ACTIVE": [{
                "field": "validation_status", "filter": "exact", "type": "string", "value": "VERIFIED"
            }]}},
        })

    def tearDown(self):
        cache.clear()

    def location(self, kind, code, parent=None):
        return Location.objects.create(type=kind, code=code, name=code, parent=parent, audit_user_id=-1)

    def group(self, location, status="VERIFIED"):
        return create_group(self.admin.username, {
            "location": location, "json_ext": {"validation_status": status, "district_id": self.b.id}
        })

    def conditions(self, district):
        return [f"district_id__exact__integer={district.id}"]

    def query(self, district=None, user=None):
        return build_group_enrollment_queryset(
            self.conditions(district) if district else [], str(self.plan.id), "ACTIVE", user or self.user
        )

    def test_partial_district_intersects_user_scope_and_ignores_json_district(self):
        self.assertEqual(set(self.query(self.a).values_list("id", flat=True)), {self.selected.id})

    def test_cleared_district_preserves_existing_group_scope_including_missing_location(self):
        expected = Group.get_queryset(Group.objects.filter(is_deleted=False), self.user).filter(
            json_ext__validation_status="VERIFIED"
        )
        self.assertEqual(set(self.query().values_list("id", flat=True)), set(expected.values_list("id", flat=True)))
        self.assertFalse(self.query().filter(id=self.other.id).exists())

    @override_settings(ROW_SECURITY=False)
    def test_unrestricted_query_includes_direct_and_all_descendant_levels(self):
        self.assertEqual(set(self.query(self.a).values_list("id", flat=True)),
                         {self.direct.id, self.selected.id, self.sibling.id})
        self.assertEqual(set(self.query(self.b).values_list("id", flat=True)), {self.other.id})

    def test_outside_scope_is_rejected_even_with_no_eligible_households(self):
        with self.assertRaises(PermissionDenied):
            self.query(self.b)

    def test_missing_user_cannot_bypass_scope(self):
        with self.assertRaises(PermissionDenied):
            build_group_enrollment_queryset([], str(self.plan.id), "ACTIVE", None)

    def test_non_r_or_inactive_selection_rejected(self):
        with self.assertRaises(ValidationError):
            self.query(self.ta)
        from django.utils import timezone
        Location.objects.filter(pk=self.b.pk).update(validity_to=timezone.now())
        with self.assertRaises(ValidationError):
            self.query(self.b)

    def test_graphql_summary_and_preview_share_scope(self):
        import graphene
        from individual.schema import Query
        from individual.services import build_group_enrollment_selection
        schema = graphene.Schema(query=Query)
        context = RequestFactory().get("/graphql")
        context.user = self.user
        query = '''query($plan: String!, $filters: [String]) {
          groupEnrollmentSummary(benefitPlanId: $plan, status: "ACTIVE", customFilters: $filters) {
            numberOfSelectedGroups totalNumberOfGroups willEnrol
          }
          group(benefitPlanToEnroll: $plan, enrollmentPreviewStatus: "ACTIVE", customFilters: $filters) {
            edges { node { uuid } }
          }
        }'''
        for filters in (self.conditions(self.a), []):
            result = schema.execute(query, variable_values={"plan": str(self.plan.id), "filters": filters}, context_value=context)
            self.assertFalse(result.errors, result.errors)
            selection = build_group_enrollment_selection(filters, str(self.plan.id), "ACTIVE", self.user)
            expected = selection["groups_not_assigned_to_selected_programme"]
            self.assertEqual({x["node"]["uuid"] for x in result.data["group"]["edges"]},
                             {str(x.uuid) for x in expected})
            summary = result.data["groupEnrollmentSummary"]
            self.assertEqual(summary["willEnrol"], expected.count())
            self.assertEqual(int(summary["totalNumberOfGroups"]), Group.get_queryset(Group.objects.filter(is_deleted=False), self.user).count())
        rejected = schema.execute(query, variable_values={"plan": str(self.plan.id), "filters": self.conditions(self.b)}, context_value=context)
        self.assertTrue(rejected.errors)

    def test_confirmation_recomputes_verified_eligibility_and_retains_scope(self):
        from individual.gql_mutations import ConfirmGroupEnrollmentMutation
        from individual.models import GroupIndividual
        from individual.tests.test_helpers import create_individual
        from social_protection.apps import SocialProtectionConfig
        from social_protection.models import GroupBeneficiary
        for group in (self.selected, self.other):
            head = create_individual(self.admin.username, {
                "location": group.location, "json_ext": {"validation_status": "VERIFIED"}
            })
            GroupIndividual(group=group, individual=head, role=GroupIndividual.Role.HEAD).save(username=self.admin.username)
        self.assertTrue(self.query(self.a).filter(pk=self.selected.pk).exists())
        with patch.object(SocialProtectionConfig, "enable_maker_checker_logic_enrollment", False):
            Group.objects.filter(pk=self.selected.pk).update(json_ext={"validation_status": "PENDING"})
            ConfirmGroupEnrollmentMutation._mutate(self.user, custom_filters=self.conditions(self.a), benefit_plan_id=str(self.plan.id), status="ACTIVE")
            self.assertFalse(GroupBeneficiary.objects.filter(benefit_plan=self.plan).exists())
            Group.objects.filter(pk=self.selected.pk).update(json_ext={"validation_status": "VERIFIED"})
            ConfirmGroupEnrollmentMutation._mutate(self.user, custom_filters=self.conditions(self.a), benefit_plan_id=str(self.plan.id), status="ACTIVE")
            self.assertEqual(set(GroupBeneficiary.objects.filter(benefit_plan=self.plan).values_list("group_id", flat=True)), {self.selected.id})

    def test_capacity_is_phase_wide_and_district_filter_does_not_reset_it(self):
        from social_protection.models import GroupBeneficiary
        from individual.services import build_group_enrollment_selection
        self.plan.max_beneficiaries = 1
        self.plan.json_ext["enrolment_ranking"] = {"ACTIVE": {"order_by": ["id"], "limit": {"percentage": 100}}}
        self.plan.save(username=self.admin.username)
        GroupBeneficiary(group=self.other, benefit_plan=self.plan, status="ACTIVE").save(username=self.admin.username)
        selected = build_group_enrollment_selection(self.conditions(self.a), str(self.plan.id), "ACTIVE", self.user)
        self.assertEqual(selected["pool_size"], 1)
        self.assertEqual(selected["will_enrol"], 0)
