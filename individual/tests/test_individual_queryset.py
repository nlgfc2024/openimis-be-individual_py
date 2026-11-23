from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db.models import Q
from django.test import SimpleTestCase, override_settings

from core.models import TechnicalUser, User
from individual.models import Individual
from location.models import LocationManager


@override_settings(ROW_SECURITY=True)
class IndividualQuerysetAccessTest(SimpleTestCase):
    def test_technical_superuser_keeps_standalone_individuals(self):
        user = User(t_user=TechnicalUser(username="standalone-query-test", is_superuser=True))
        queryset = Individual.objects.filter(first_name="Standalone")

        self.assertFalse(user.is_imis_admin)
        self.assertTrue(user.is_superuser)
        # The real location manager returns an empty Q for technical users.
        result = Individual.get_queryset(queryset, user)
        self.assertIs(result, queryset)
        self.assertNotIn("individual_groupindividual", str(result.query))

    def test_unrestricted_location_policy_does_not_add_household_requirement(self):
        user = SimpleNamespace(is_anonymous=False, is_imis_admin=False, _u=object())
        queryset = Individual.objects.all()

        with patch.object(LocationManager, "build_user_location_filter_query", return_value=Q()) as location_filter:
            self.assertIs(Individual.get_queryset(queryset, user), queryset)

        location_filter.assert_called_once_with(user._u)

    def test_restricted_users_keep_own_location_or_household_location_access(self):
        user = SimpleNamespace(is_anonymous=False, is_imis_admin=False, _u=object())
        queryset = Individual.objects.filter(is_deleted=False)
        own_location = Q(location_id=101)
        household_location = Q(groupindividuals__group__location_id=101)
        has_household = Q(groupindividuals__group__isnull=False)
        expected = queryset.filter(own_location | (has_household & household_location))

        with patch.object(LocationManager, "build_user_location_filter_query",
                          side_effect=[own_location, household_location]) as location_filter:
            result = Individual.get_queryset(queryset, user)

        self.assertEqual(result.query.where, expected.query.where)
        self.assertEqual(location_filter.call_count, 2)

    def test_imis_admin_does_not_need_household_membership(self):
        user = SimpleNamespace(is_anonymous=False, is_imis_admin=True)
        queryset = Individual.objects.all()
        self.assertIs(Individual.get_queryset(queryset, user), queryset)

    def test_anonymous_access_remains_restricted(self):
        queryset = Mock()
        result = Individual.get_queryset(queryset, SimpleNamespace(is_anonymous=True))
        self.assertIs(result, queryset.filter.return_value)
        queryset.filter.assert_called_once_with(id=-1)

    @override_settings(ROW_SECURITY=False)
    def test_disabled_row_security_keeps_original_queryset(self):
        queryset = Individual.objects.all()
        self.assertIs(Individual.get_queryset(queryset, SimpleNamespace()), queryset)
