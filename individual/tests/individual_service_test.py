import copy

from django.test import TestCase

from individual.models import Individual
from individual.services import IndividualService
from individual.tests.data import (
    service_add_individual_payload,
    service_add_individual_payload_no_ext,
    service_update_individual_payload
)
from core.test_helpers import LogInHelper


class IndividualServiceTest(TestCase):
    user = None
    service = None
    query_all = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.user = LogInHelper().get_or_create_user_api()
        cls.service = IndividualService(cls.user)
        cls.query_all = Individual.objects.filter(is_deleted=False)

    def test_add_individual(self):
        result = self.service.create(service_add_individual_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid', None)
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        json_ext = query.first().json_ext
        self.assertEqual(json_ext['key'], 'value')
        self.assertEqual(json_ext['key2'], 'value2')

    def test_add_individual_no_ext(self):
        result = self.service.create(service_add_individual_payload_no_ext)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid')
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)

    def test_update_individual(self):
        result = self.service.create(service_add_individual_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid')
        update_payload = copy.deepcopy(service_update_individual_payload)
        update_payload['id'] = uuid
        result = self.service.update(update_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 1)
        self.assertEqual(query.first().first_name, update_payload.get('first_name'))
        json_ext = query.first().json_ext
        self.assertEqual(json_ext['key'], 'value')
        self.assertEqual(json_ext['key2'], 'value2 updated')

    def test_delete_individual(self):
        result = self.service.create(service_add_individual_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        uuid = result.get('data', {}).get('uuid')
        delete_payload = {'id': uuid}
        result = self.service.delete(delete_payload)
        self.assertTrue(result.get('success', False), result.get('detail', "No details provided"))
        query = self.query_all.filter(uuid=uuid)
        self.assertEqual(query.count(), 0)
