import copy
from unittest.mock import patch

from django.test import TestCase

from tasks_management.services import UpdateCheckerLogicServiceMixin
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

    def test_create_cannot_set_payment_details_outside_kyc(self):
        payload = copy.deepcopy(service_add_individual_payload)
        payload["json_ext"]["account_number"] = "001234"

        result = self.service.create(payload)

        self.assertFalse(result.get("success", False))
        self.assertIn("Python KYC Details workflow", str(result))

    def test_direct_update_preserves_payment_details(self):
        result = self.service.create(service_add_individual_payload)
        individual = Individual.objects.get(uuid=result["data"]["uuid"])
        individual.json_ext.update({
            "account_number": "001234",
            "account_name": "Test Recipient",
            "bank": "TNM Mpamba",
            "payment_provider_code": "TNM",
        })
        individual.save(user=self.user)
        payload = copy.deepcopy(service_update_individual_payload)
        payload.update({
            "id": individual.id,
            "json_ext": {
                **individual.json_ext,
                "account_number": "009999",
            },
        })

        result = self.service.update(payload)

        self.assertTrue(result.get("success", False), result.get("detail", "No details provided"))
        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["account_number"], "001234")

    def test_direct_update_cannot_override_payment_details_from_top_level(self):
        result = self.service.create(service_add_individual_payload)
        individual = Individual.objects.get(uuid=result["data"]["uuid"])
        individual.json_ext["account_number"] = "001234"
        individual.save(user=self.user)
        payload = copy.deepcopy(service_update_individual_payload)
        payload.update({
            "id": individual.id,
            "json_ext": dict(individual.json_ext),
            "account_number": "009999",
        })

        result = self.service.update(payload)

        self.assertTrue(result.get("success", False), result.get("detail", "No details provided"))
        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["account_number"], "001234")

    def test_update_task_cannot_change_payment_details(self):
        result = self.service.create(service_add_individual_payload)
        individual = Individual.objects.get(uuid=result["data"]["uuid"])
        individual.json_ext["account_number"] = "001234"
        individual.save(user=self.user)
        payload = copy.deepcopy(service_update_individual_payload)
        payload.update({
            "id": individual.id,
            "json_ext": {
                **individual.json_ext,
                "account_number": "009999",
            },
        })

        result = self.service.create_update_task(payload)

        self.assertFalse(result.get("success", False))
        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["account_number"], "001234")

    def test_update_task_does_not_snapshot_payment_details_for_approval(self):
        result = self.service.create(service_add_individual_payload)
        individual = Individual.objects.get(uuid=result["data"]["uuid"])
        individual.json_ext.update({
            "account_number": "001234",
            "account_name": "Test Recipient",
            "bank": "TNM Mpamba",
            "payment_provider_code": "TNM",
        })
        individual.save(user=self.user)
        payload = copy.deepcopy(service_update_individual_payload)
        payload.update({
            "id": individual.id,
            "json_ext": {
                **individual.json_ext,
                "key": "updated",
            },
        })

        with patch.object(
            UpdateCheckerLogicServiceMixin,
            "create_update_task",
            autospec=True,
            return_value={"success": True},
        ) as create_update_task:
            result = self.service.create_update_task(payload)

        self.assertTrue(result["success"])
        task_payload = create_update_task.call_args.args[1]
        self.assertFalse(
            any(field in task_payload["json_ext"] for field in (
                "account_number",
                "account_name",
                "bank",
                "payment_provider_code",
            ))
        )

        individual.json_ext["account_number"] = "009999"
        individual.save(user=self.user)
        result = self.service.update(task_payload)

        self.assertTrue(result.get("success", False), result.get("detail", "No details provided"))
        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["account_number"], "009999")
        self.assertEqual(individual.json_ext["key"], "updated")

    def test_generic_update_preserves_existing_payment_details(self):
        result = self.service.create(service_add_individual_payload)
        individual = Individual.objects.get(uuid=result["data"]["uuid"])
        individual.json_ext.update({
            "account_number": "001234",
            "account_name": "Test Recipient",
            "bank": "TNM Mpamba",
            "payment_provider_code": "TNM",
        })
        individual.save(user=self.user)
        payload = copy.deepcopy(service_update_individual_payload)
        payload.update({
            "id": individual.id,
            "json_ext": {"key": "updated"},
        })

        result = self.service.update(payload)

        self.assertTrue(result.get("success", False), result.get("detail", "No details provided"))
        individual.refresh_from_db()
        self.assertEqual(individual.json_ext["account_number"], "001234")
        self.assertEqual(individual.json_ext["payment_provider_code"], "TNM")
        self.assertEqual(individual.json_ext["key"], "updated")

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
