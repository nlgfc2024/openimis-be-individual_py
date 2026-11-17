import json
from unittest.mock import call, patch

from django.apps import apps
from django.test import TestCase

from core.models import ModuleConfiguration
from individual.apps import IndividualConfig
from individual.workflows.base_individual_update import process_kyc_details_workflow


class ModuleConfigTest(TestCase):
    def test_kyc_workflow_is_registered_with_python_workflows(self):
        individual_app = apps.get_app_config("individual")
        with patch("workflow.systems.python.PythonWorkflowAdaptor.register_workflow") as register_workflow:
            with patch.object(IndividualConfig, "enable_python_workflows", True), \
                    patch.object(IndividualConfig, "validation_import_valid_items_workflow", "custom"), \
                    patch.object(IndividualConfig, "validation_upload_valid_items_workflow", "custom"):
                individual_app._set_up_workflows()

        self.assertIn(
            call("Python KYC Details", "individual", process_kyc_details_workflow),
            register_workflow.call_args_list,
        )

    def test_config_reloading(self):
        # First set the individual config to be empty
        config = ModuleConfiguration.objects.filter(module='individual', layer='be').first()
        if config is None:
            config = ModuleConfiguration(module='individual', layer='be', config='{}')
        else:
            config.config = '{}'
        config.save()

        self.assertTrue(IndividualConfig.enable_maker_checker_for_individual_upload)
        self.assertTrue(IndividualConfig.enable_maker_checker_for_individual_update)

        # Update config should trigger a reload
        updated_config = {
            "enable_maker_checker_for_individual_upload": False,
            "enable_maker_checker_for_individual_update": False,
        }
        config.config = json.dumps(updated_config)
        config.save()

        self.assertFalse(IndividualConfig.enable_maker_checker_for_individual_upload)
        self.assertFalse(IndividualConfig.enable_maker_checker_for_individual_update)
