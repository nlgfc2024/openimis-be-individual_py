import json
import logging
import re

from collections import namedtuple
from django.apps import apps
from django.db.models.query import QuerySet
from typing import List

from core.custom_filters import CustomFilterWizardInterface
from individual.apps import IndividualConfig
from individual.models import Individual, Group, GroupIndividual


logger = logging.getLogger(__name__)


class IndividualCustomFilterWizard(CustomFilterWizardInterface):

    OBJECT_CLASS = Individual

    def get_type_of_object(self) -> str:
        return self.OBJECT_CLASS.__name__

    def load_definition(self, tuple_type: type, **kwargs) -> List[namedtuple]:
        individual_schema = IndividualConfig.individual_schema
        additional_params = kwargs.get('additional_params', None)
        benefit_plan_id = additional_params.get("benefitPlan", None)
        if benefit_plan_id and 'social_protection' in apps.app_configs:
            from social_protection.models import BenefitPlan
            benefit_plan = BenefitPlan.objects.get(id=benefit_plan_id)
            if benefit_plan.beneficiary_data_schema and benefit_plan.beneficiary_data_schema != '{}':
                return self.__process_schema_and_build_tuple(benefit_plan.beneficiary_data_schema, tuple_type)
        if individual_schema:
            individual_schema_dict = json.loads(individual_schema)
            return self.__process_schema_and_build_tuple(individual_schema_dict, tuple_type)
        return []

    def apply_filter_to_queryset(self, custom_filters: List[namedtuple], query: QuerySet, relation=None) -> QuerySet:
        for filter_part in custom_filters:
            field, value = filter_part.split('=')
            field, value_type = field.rsplit('__', 1)
            value = self.__cast_value(value, value_type)
            filter_kwargs = {f"{relation}__json_ext__{field}" if relation else f"json_ext__{field}": value}
            query = query.filter(**filter_kwargs)
        return query

    def __process_schema_and_build_tuple(
            self,
            individual_schema: dict,
            tuple_type: type
    ) -> List[namedtuple]:
        tuples_with_definitions = []

        properties = individual_schema.get('properties', {})
        for key, value in properties.items():
            tuple_with_definition = tuple_type(
                field=key,
                filter=self.FILTERS_BASED_ON_FIELD_TYPE[value['type']],
                type=value['type']
            )
            tuples_with_definitions.append(tuple_with_definition)

        else:
            logger.warning('Cannot retrieve definitions of filters based '
                           'on the provided schema due to either empty schema '
                           'or missing properties in schema file')

        return tuples_with_definitions

    def __cast_value(self, value: str, value_type: str):
        if value_type == 'integer':
            return int(value)
        elif value_type == 'string':
            try:
                decoded = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return str(value)
            return decoded if isinstance(decoded, str) else str(value)
        elif value_type == 'numeric':
            return float(value)
        elif value_type == 'boolean':
            cleaned_value = self.__remove_unexpected_chars(value)
            if cleaned_value.lower() == 'true':
                return True
            elif cleaned_value.lower() == 'false':
                return False
        elif value_type == 'date':
            # Perform date parsing logic here
            # Assuming you have a specific date format, you can use datetime.strptime
            # Example: return datetime.strptime(value, '%Y-%m-%d').date()
            pass

        # Return None if the value type is not recognized
        return None

    def __remove_unexpected_chars(self, string: str):
        pattern = r'[^\w\s]'  # Remove any character that is not alphanumeric or whitespace

        # Use re.sub() to remove the unwanted characters
        cleaned_string = re.sub(pattern, '', string)

        return cleaned_string


class GroupCustomFilterWizard(IndividualCustomFilterWizard):

    OBJECT_CLASS = Group


class GroupIndividualCustomFilterWizard(IndividualCustomFilterWizard):

    OBJECT_CLASS = GroupIndividual
