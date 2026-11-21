import json
import graphene
import graphene_django_optimizer as gql_optimizer
import pandas as pd
from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q, OuterRef, Subquery, Value
from django.db.models.functions import Concat

from core.custom_filters import CustomFilterWizardStorage
from core.gql.export_mixin import ExportableQueryMixin
from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter, is_valid_uuid
from individual.apps import IndividualConfig
from individual.data_source import SUPPORTED_KYC_PAYMENT_FIELDS
from individual.gql_mutations import CreateIndividualMutation, UpdateIndividualMutation, DeleteIndividualMutation, \
    CreateGroupMutation, UpdateGroupMutation, DeleteGroupMutation, CreateGroupIndividualMutation, \
    UpdateGroupIndividualMutation, DeleteGroupIndividualMutation, \
    CreateGroupIndividualsMutation, CreateGroupAndMoveIndividualMutation, UndoDeleteIndividualMutation
from individual.gql_queries import IndividualGQLType, IndividualHistoryGQLType, IndividualDataSourceGQLType, \
    GroupGQLType, GroupIndividualGQLType, \
    IndividualDataSourceUploadGQLType, GroupHistoryGQLType, IndividualDataUploadQGLType, \
    GroupIndividualHistoryGQLType, GlobalSchemaType, \
    GroupDataSourceGQLType, _json_ext_without_payment_details, \
    has_kyc_payment_details_access
from individual.models import Individual, IndividualDataSource, Group, \
    GroupIndividual, IndividualDataSourceUpload, IndividualDataUploadRecords, GroupDataSource
from individual.primary_recipients import (
    authoritative_primary_worker_memberships,
    primary_worker_membership_filter,
)
from location.apps import LocationConfig


def _require_benefit_plan_access(user, benefit_plan_id):
    """Authorize a programme-scoped query when Social Protection is present."""
    if not apps.is_installed("social_protection"):
        return

    from social_protection.apps import SocialProtectionConfig
    from social_protection.benefit_plan_access import require_benefit_plan_access

    require_benefit_plan_access(user, benefit_plan_id)
    if SocialProtectionConfig.enable_benefit_plan_access_control:
        from social_protection.models import BenefitPlan

        if not BenefitPlan.objects.filter(
            id=benefit_plan_id,
            is_deleted=False,
        ).exists():
            raise PermissionDenied("Benefit Plan does not exist.")


def patch_details(data_df: pd.DataFrame):
    # Transform extension to DF columns
    if 'json_ext' in data_df:
        data_df = data_df.copy()
        data_df['json_ext'] = data_df['json_ext'].map(_json_ext_without_payment_details)
        df_unfolded = pd.json_normalize(data_df['json_ext'])
        # Merge unfolded DataFrame with the original DataFrame
        df_final = pd.concat([data_df, df_unfolded], axis=1)
        df_final = df_final.drop('json_ext', axis=1)
        return df_final
    return data_df


def _is_payment_details_json_column(column):
    parts = column.split("__")
    return any(
        part == "json_ext"
        and index + 1 < len(parts)
        and parts[index + 1] in SUPPORTED_KYC_PAYMENT_FIELDS
        for index, part in enumerate(parts)
    )


def _is_primary_recipient_payment_column(column):
    return column in {
        f"primary_recipient_{field}"
        for field in SUPPORTED_KYC_PAYMENT_FIELDS
    }


class Query(ExportableQueryMixin, graphene.ObjectType):
    export_patches = {
        'group': [
            patch_details
        ],
        'individual': [
            patch_details
        ],
        'group_individual': [
            patch_details
        ]
    }
    exportable_fields = ['group', 'individual', 'group_individual']
    module_name = "individual"
    object_type = "Individual"
    object_type_group = "Group"
    related_field_individual = "groupindividuals__individual"

    @classmethod
    def create_export_function(cls, field_name):
        super().create_export_function(field_name)
        resolver_name = f"resolve_{field_name}_export"
        export_resolver = getattr(cls, resolver_name)

        def guarded_export(root, info, **kwargs):
            requested_fields = kwargs.get("fields")
            normalized_fields = [
                cls._adjust_notation(field)
                for field in requested_fields
                if isinstance(field, str)
            ] if isinstance(requested_fields, (list, tuple)) else []
            sensitive_json_fields = [
                field for field in normalized_fields
                if _is_payment_details_json_column(field)
            ]
            primary_recipient_payment_fields = [
                field for field in normalized_fields
                if _is_primary_recipient_payment_column(field)
            ]

            if sensitive_json_fields:
                raise ValidationError(
                    "Payment details cannot be selected through jsonExt export paths."
                )
            if primary_recipient_payment_fields:
                if not has_kyc_payment_details_access(info.context.user):
                    raise PermissionDenied(
                        "You do not have permission to export KYC payment details."
                    )
            if field_name == "group" and (
                primary_recipient_payment_fields
            ) and not (
                kwargs.get("benefitPlanId")
                and kwargs.get("primary_recipient_only")
                and kwargs.get("include_primary_recipient_data")
            ):
                raise ValidationError(
                    "Group payment details can only be exported for a "
                    "phase-scoped primary-recipient export."
                )
            return export_resolver(root, info, **kwargs)

        setattr(cls, resolver_name, guarded_export)

    individual = OrderedDjangoFilterConnectionField(
        IndividualGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        groupId=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(),
        filterNotAttachedToGroup=graphene.Boolean(),
        primary_recipient_only=graphene.Boolean(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
    )

    individual_history = OrderedDjangoFilterConnectionField(
        IndividualHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        groupId=graphene.String()
    )

    individual_data_source = OrderedDjangoFilterConnectionField(
        IndividualDataSourceGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    group_data_source = OrderedDjangoFilterConnectionField(
        GroupDataSourceGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    individual_data_source_upload = OrderedDjangoFilterConnectionField(
        IndividualDataSourceUploadGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    group = OrderedDjangoFilterConnectionField(
        GroupGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        first_name=graphene.String(),
        last_name=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        name=graphene.String(),
        validation_status=graphene.String(),
        sector=graphene.String(),
        micro_catchment_uuid=graphene.String(),
        hotspot_id=graphene.Int(),
        benefitPlanId=graphene.String(),
        primary_recipient_only=graphene.Boolean(),
        include_primary_recipient_data=graphene.Boolean(),
    )

    group_history = OrderedDjangoFilterConnectionField(
        GroupHistoryGQLType,
        json_ext_head__icontains=graphene.String(),
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    group_individual = OrderedDjangoFilterConnectionField(
        GroupIndividualGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    group_individual_history = OrderedDjangoFilterConnectionField(
        GroupIndividualHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
    )

    individual_data_upload_history = OrderedDjangoFilterConnectionField(
        IndividualDataUploadQGLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String()
    )

    global_schema = graphene.Field(GlobalSchemaType)

    def resolve_individual(self, info, **kwargs):
        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_individual_search_perms)

        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        group_id = kwargs.get("groupId")
        if group_id:
            filters.append(Q(groupindividuals__group__id=group_id))

        benefit_plan_id = kwargs.get("benefitPlanId")
        if benefit_plan_id:
            _require_benefit_plan_access(info.context.user, benefit_plan_id)
            filters.append(
                Q(is_deleted=False) & Q(
                    beneficiary__benefit_plan_id=benefit_plan_id
                )
            )

        filter_not_attached_to_group = kwargs.get("filterNotAttachedToGroup")
        if filter_not_attached_to_group:
            subquery = GroupIndividual.objects.filter(
                individual=OuterRef('pk')
            ).exclude(
                is_deleted=True
            ).values('individual')
            filters.append(~Q(pk__in=Subquery(subquery)))

        primary_recipient_only = kwargs.get("primary_recipient_only", False)
        if primary_recipient_only:
            filters.append(primary_worker_membership_filter("groupindividuals"))

        parent_location = kwargs.get('parent_location')
        parent_location_level = kwargs.get('parent_location_level')
        if parent_location is not None and parent_location_level is not None:
            filters.append(Query._get_location_filters(parent_location, parent_location_level))

        query = IndividualGQLType.get_queryset(None, info)
        query = query.filter(*filters)
        if primary_recipient_only:
            from location.models import MicroCatchmentGVH

            catchment_query = MicroCatchmentGVH.objects.filter(
                location_id=OuterRef("location__parent_id"),
            ).filter(*MicroCatchmentGVH.filter_validity())
            query = query.annotate(
                kyc_catchment=Subquery(
                    catchment_query.order_by(
                        "micro_catchment__name",
                        "micro_catchment_id",
                    ).values("micro_catchment__name")[:1]
                )
            ).distinct()

        custom_filters = kwargs.get("customFilters", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                Query.object_type,
                custom_filters,
                query,
            )

        return gql_optimizer.query(query, info)

    def resolve_individual_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_individual_search_perms)
        query = Individual.history.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_individual_data_source(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_individual_search_perms)
        query = IndividualDataSource.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group_data_source(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_individual_search_perms)
        query = GroupDataSource.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_individual_data_source_upload(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_individual_search_perms)
        query = IndividualDataSourceUpload.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group(self, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            IndividualConfig.gql_group_search_perms
        )
        filters = append_validity_filter(**kwargs)
        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        first_name = kwargs.get("first_name", None)
        if first_name:
            filters.append(Q(groupindividuals__individual__first_name__icontains=first_name))

        last_name = kwargs.get("last_name", None)
        if last_name:
            filters.append(Q(groupindividuals__individual__last_name__icontains=last_name))

        name = kwargs.get("name")
        if name:
            filters.append(
                Q(code__icontains=name)
                | Q(json_ext__form_number__icontains=name)
                | Q(groupindividuals__individual__first_name__icontains=name)
                | Q(groupindividuals__individual__last_name__icontains=name)
            )

        validation_status = kwargs.get("validation_status")
        if validation_status:
            filters.append(Q(json_ext__validation_status__iexact=validation_status))

        sector = kwargs.get("sector")
        if sector:
            filters.append(
                Q(json_ext__sector__icontains=sector)
                | Q(
                    groupindividuals__is_deleted=False,
                    groupindividuals__recipient_type=GroupIndividual.RecipientType.PRIMARY,
                    groupindividuals__individual__json_ext__sector__icontains=sector,
                )
            )

        micro_catchment_uuid = kwargs.get("micro_catchment_uuid")
        if micro_catchment_uuid:
            from location.models import MicroCatchmentGVH, MicroCatchmentTA

            gvh_ids = MicroCatchmentGVH.objects.filter(
                micro_catchment__uuid=micro_catchment_uuid,
                validity_to__isnull=True,
                micro_catchment__validity_to__isnull=True,
            ).values("location_id")
            ta_ids = MicroCatchmentTA.objects.filter(
                micro_catchment__uuid=micro_catchment_uuid,
                validity_to__isnull=True,
                micro_catchment__validity_to__isnull=True,
            ).values("location_id")
            filters.append(
                Q(location_id__in=gvh_ids)
                | Q(location__parent_id__in=gvh_ids)
                | Q(location_id__in=ta_ids)
                | Q(location__parent_id__in=ta_ids)
                | Q(location__parent__parent_id__in=ta_ids)
            )

        hotspot_id = kwargs.get("hotspot_id")
        if hotspot_id:
            from location.models import HotspotVillage

            hotspot_village_ids = HotspotVillage.objects.filter(
                hotspot_id=hotspot_id,
                validity_to__isnull=True,
                hotspot__validity_to__isnull=True,
            ).values("location_id")
            filters.append(Q(location_id__in=hotspot_village_ids))

        if kwargs.get("primary_recipient_only"):
            filters.append(primary_worker_membership_filter(
                "groupindividuals",
                require_verified_individual=True,
            ))

        benefit_plan_id = kwargs.get("benefitPlanId")
        if benefit_plan_id:
            _require_benefit_plan_access(info.context.user, benefit_plan_id)
            beneficiary_filters = Q(
                groupbeneficiary__benefit_plan_id=benefit_plan_id,
                groupbeneficiary__is_deleted=False,
            )
            if kwargs.get("primary_recipient_only"):
                # KYC payment exports select a Phase and must contain only
                # verified households that are active beneficiaries of it.
                # Without these constraints, potential/old Phase links were
                # exported as well, making the workbook larger than the Phase
                # beneficiary count.
                beneficiary_filters &= Q(groupbeneficiary__status="ACTIVE")
                beneficiary_filters &= Q(json_ext__validation_status__iexact="VERIFIED")
            filters.append(beneficiary_filters)

        parent_location = kwargs.get('parent_location')
        parent_location_level = kwargs.get('parent_location_level')
        if parent_location is not None and parent_location_level is not None:
            filters.append(Query._get_location_filters(parent_location, parent_location_level))

        query = Group.get_queryset(None, info.context.user)
        query = query.filter(*filters).distinct()
        if benefit_plan_id and kwargs.get("primary_recipient_only"):
            from social_protection.models import BenefitPlan

            phase_name_query = BenefitPlan.objects.filter(
                id=benefit_plan_id,
                is_deleted=False,
            ).annotate(
                export_phase_name=Concat("code", Value(" "), "name"),
            ).values("export_phase_name")[:1]
            query = query.annotate(kyc_phase=Subquery(phase_name_query))
        if kwargs.get("include_primary_recipient_data"):
            from location.models import MicroCatchmentGVH

            primary_recipient_query = authoritative_primary_worker_memberships(
                require_verified_individual=bool(kwargs.get("primary_recipient_only")),
            ).filter(
                group_id=OuterRef("pk"),
            ).order_by("id")
            primary_recipient_name_query = primary_recipient_query.annotate(
                full_name=Concat(
                    "individual__first_name",
                    Value(" "),
                    "individual__last_name",
                )
            )
            catchment_query = primary_recipient_query.annotate(
                primary_catchment=Subquery(
                    MicroCatchmentGVH.objects.filter(
                        location_id=OuterRef("individual__location__parent_id"),
                    ).filter(*MicroCatchmentGVH.filter_validity()).order_by(
                        "micro_catchment__name",
                        "micro_catchment_id",
                    ).values("micro_catchment__name")[:1]
                )
            ).values("primary_catchment")[:1]
            primary_recipient_annotations = {
                "kyc_catchment": Subquery(catchment_query),
                "primary_recipient_first_name": Subquery(
                    primary_recipient_query.values("individual__first_name")[:1]
                ),
                "primary_recipient_last_name": Subquery(
                    primary_recipient_query.values("individual__last_name")[:1]
                ),
                "primary_recipient_full_name": Subquery(
                    primary_recipient_name_query.values("full_name")[:1]
                ),
                "primary_recipient_dob": Subquery(
                    primary_recipient_query.values("individual__dob")[:1]
                ),
                "primary_recipient_gender": Subquery(
                    primary_recipient_query.values("individual__json_ext__gender")[:1]
                ),
                "primary_recipient_national_id": Subquery(
                    primary_recipient_query.values("individual__json_ext__national_id")[:1]
                ),
            }
            if (
                benefit_plan_id
                and kwargs.get("primary_recipient_only")
                and has_kyc_payment_details_access(info.context.user)
            ):
                primary_recipient_annotations.update({
                    "primary_recipient_account_number": Subquery(
                        primary_recipient_query.values("individual__json_ext__account_number")[:1]
                    ),
                    "primary_recipient_account_name": Subquery(
                        primary_recipient_query.values("individual__json_ext__account_name")[:1]
                    ),
                    "primary_recipient_bank": Subquery(
                        primary_recipient_query.values("individual__json_ext__bank")[:1]
                    ),
                })
            query = query.annotate(
                **primary_recipient_annotations,
            )

        custom_filters = kwargs.get("customFilters", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                "Group",
                custom_filters,
                query
            )
        return gql_optimizer.query(query, info)

    def resolve_group_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        json_ext_head_icontains = kwargs.get("json_ext_head__icontains")
        if json_ext_head_icontains:
            filters.append(Q(json_ext__head__icontains=json_ext_head_icontains))

        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_group_search_perms)
        query = Group.history.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group_individual(self, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            IndividualConfig.gql_group_search_perms
        )
        filters = append_validity_filter(**kwargs)

        group_id = kwargs.get("group__id")
        if not group_id or group_id and not is_valid_uuid(group_id):
            # it will result in empty query
            filters.append(Q(id__lt=0))

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = GroupIndividual.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group_individual_history(self, info, **kwargs):
        Query._check_permissions(info.context.user,
                                 IndividualConfig.gql_group_search_perms)
        filters = append_validity_filter(**kwargs)
        query = GroupIndividual.history.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_individual_data_upload_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        Query._check_permissions(
            info.context.user,
            IndividualConfig.gql_individual_search_perms
        )
        query = IndividualDataUploadRecords.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_global_schema(self, info):
        individual_schema = IndividualConfig.individual_schema
        if individual_schema:
            individual_schema_dict = json.loads(individual_schema)
            return GlobalSchemaType(schema=individual_schema_dict)
        return GlobalSchemaType(schema={})

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError("Unauthorized")

    @staticmethod
    def _get_location_filters(parent_location, parent_location_level):
        query_key = "uuid"
        for i in range(len(LocationConfig.location_types) - parent_location_level - 1):
            query_key = "parent__" + query_key
        query_key = "location__" + query_key
        return Q(**{query_key: parent_location})


class Mutation(graphene.ObjectType):
    create_individual = CreateIndividualMutation.Field()
    update_individual = UpdateIndividualMutation.Field()
    delete_individual = DeleteIndividualMutation.Field()
    undo_delete_individual = UndoDeleteIndividualMutation.Field()

    create_group = CreateGroupMutation.Field()
    update_group = UpdateGroupMutation.Field()
    delete_group = DeleteGroupMutation.Field()

    add_individual_to_group = CreateGroupIndividualMutation.Field()
    edit_individual_in_group = UpdateGroupIndividualMutation.Field()
    remove_individual_from_group = DeleteGroupIndividualMutation.Field()

    create_group_individuals = CreateGroupIndividualsMutation.Field()
    create_group_and_move_individual = CreateGroupAndMoveIndividualMutation.Field()
