import graphene
from django.contrib.auth.models import AnonymousUser
from django.db.models import Prefetch
from graphene_django import DjangoObjectType
import graphene_django_optimizer as gql_optimizer

from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from individual.apps import IndividualConfig
from individual.data_source import SUPPORTED_KYC_PAYMENT_FIELDS
from individual.models import Individual, IndividualDataSource, Group, GroupIndividual, \
    IndividualDataSourceUpload, IndividualDataUploadRecords, GroupDataSource
from individual.primary_recipients import authoritative_primary_worker_memberships


def _filter_by_accessible_uploads(queryset, info, upload_field):
    """Scope raw imports to uploads whose every programme is accessible.

    Positively identified general-registry uploads remain visible. An upload is
    general only when it has an active Individual upload record and has never
    had a Benefit Plan upload record; missing or inconsistent ownership data
    therefore stays hidden. With the feature disabled, or without Social
    Protection installed, the legacy queryset is unchanged.
    """
    from django.apps import apps

    if not apps.is_installed("social_protection"):
        return queryset

    from social_protection.apps import SocialProtectionConfig
    from social_protection.benefit_plan_access import (
        get_accessible_benefit_plan_ids,
    )

    if not SocialProtectionConfig.enable_benefit_plan_access_control:
        return queryset

    if not _have_permissions(
        info.context.user,
        IndividualConfig.gql_individual_search_perms,
    ):
        return queryset.none()

    accessible_ids = get_accessible_benefit_plan_ids(info.context.user)
    if accessible_ids is None:
        return queryset

    from social_protection.models import BenefitPlanDataUploadRecords

    # Include soft-deleted ownership rows. Removing an association must never
    # reclassify programme data as a general-registry upload.
    programme_records = BenefitPlanDataUploadRecords.objects.all()
    inaccessible_upload_ids = programme_records.exclude(
        benefit_plan_id__in=accessible_ids,
    ).values("data_upload_id")
    accessible_programme_upload_ids = programme_records.filter(
        benefit_plan_id__in=accessible_ids,
    ).exclude(
        data_upload_id__in=inaccessible_upload_ids,
    ).values("data_upload_id")

    # IndividualDataUploadRecords is the explicit marker produced by the
    # general Individual/Household registry import service. Merely lacking a
    # programme record is not sufficient because an interrupted programme
    # import can leave an unassociated upload behind.
    general_registry_upload_ids = IndividualDataUploadRecords.objects.filter(
        is_deleted=False,
    ).exclude(
        data_upload_id__in=programme_records.values("data_upload_id"),
    ).values("data_upload_id")

    from django.db.models import Q

    return queryset.filter(
        Q(**{f"{upload_field}__in": accessible_programme_upload_ids})
        | Q(**{f"{upload_field}__in": general_registry_upload_ids})
    )


def _have_permissions(user, permission):
    if isinstance(user, AnonymousUser):
        return False
    if not user.id:
        return False
    return user.has_perms(permission)


def has_kyc_payment_details_access(user):
    return _have_permissions(user, IndividualConfig.gql_kyc_payment_details_perms)


def _json_ext_without_payment_details(json_ext):
    if not isinstance(json_ext, dict):
        return json_ext
    return {
        key: value
        for key, value in json_ext.items()
        if key not in SUPPORTED_KYC_PAYMENT_FIELDS
    }


def _payment_details_for_user(json_ext, user):
    if not has_kyc_payment_details_access(user) or not isinstance(json_ext, dict):
        return None
    return {
        field: json_ext[field]
        for field in SUPPORTED_KYC_PAYMENT_FIELDS
        if field in json_ext
    }


class JsonExtMixin:
    def resolve_json_ext(self, info):
        if _have_permissions(info.context.user, IndividualConfig.gql_individual_search_perms):
            return _json_ext_without_payment_details(self.json_ext)
        return None


class KycPaymentDetailsGQLType(graphene.ObjectType):
    account_number = graphene.String()
    account_name = graphene.String()
    bank = graphene.String()
    payment_provider_code = graphene.String()


class IndividualGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    payment_details = graphene.Field(KycPaymentDetailsGQLType)

    def resolve_json_ext(self, info):
        return _json_ext_without_payment_details(self.json_ext)

    def resolve_payment_details(self, info):
        return _payment_details_for_user(self.json_ext, info.context.user)

    class Meta:
        model = Individual
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "first_name": ["iexact", "istartswith", "icontains"],
            "last_name": ["iexact", "istartswith", "icontains"],
            "dob": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "location": ["isnull"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return Individual.get_queryset(queryset, info.context.user)


class IndividualHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)

    def resolve_json_ext(self, info):
        return _json_ext_without_payment_details(self.json_ext)

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = Individual.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "first_name": ["iexact", "istartswith", "icontains"],
            "last_name": ["iexact", "istartswith", "icontains"],
            "dob": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_individual_query = Individual.get_queryset(None, info.context.user)
        accessible_individuals = gql_optimizer.query(accessible_individual_query, info)
        accessible_uuids = set(accessible_individuals.values_list('uuid', flat=True))
        return queryset.filter(id__in=accessible_uuids)


class IndividualDataSourceUploadGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataSourceUpload
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "status": ["iexact", "istartswith", "icontains"],
            "source_type": ["iexact", "istartswith", "icontains"],
            "source_name": ["iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return _filter_by_accessible_uploads(queryset, info, "id")


class IndividualDataSourceGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')
    payment_details = graphene.Field(KycPaymentDetailsGQLType)

    def resolve_payment_details(self, info):
        return _payment_details_for_user(self.json_ext, info.context.user)

    class Meta:
        model = IndividualDataSource
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return _filter_by_accessible_uploads(queryset, info, "upload_id")


class GroupGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    head = graphene.Field(IndividualGQLType)
    primary_recipient = graphene.Field(IndividualGQLType)
    # Populated only for the Phase-scoped KYC export. These scalar fields are
    # explicitly declared so the generic, allow-listed Group exporter can use
    # the annotations added by ``resolve_group``.
    kyc_phase = graphene.String()
    kyc_catchment = graphene.String()
    primary_recipient_full_name = graphene.String()
    primary_recipient_dob = graphene.String()
    primary_recipient_gender = graphene.String()
    primary_recipient_national_id = graphene.String()
    primary_recipient_account_number = graphene.String()
    primary_recipient_account_name = graphene.String()
    primary_recipient_bank = graphene.String()

    def resolve_json_ext(self, info):
        return _json_ext_without_payment_details(self.json_ext)

    def resolve_primary_recipient_account_number(self, info):
        if not has_kyc_payment_details_access(info.context.user):
            return None
        return getattr(self, "primary_recipient_account_number", None)

    def resolve_primary_recipient_account_name(self, info):
        if not has_kyc_payment_details_access(info.context.user):
            return None
        return getattr(self, "primary_recipient_account_name", None)

    def resolve_primary_recipient_bank(self, info):
        if not has_kyc_payment_details_access(info.context.user):
            return None
        return getattr(self, "primary_recipient_bank", None)

    def resolve_head(self, info):
        return Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        ).first()

    def resolve_primary_recipient(self, info):
        primary_recipients = getattr(self, "primary_worker_memberships", None)
        if primary_recipients is not None:
            return primary_recipients[0].individual if primary_recipients else None
        primary = authoritative_primary_worker_memberships().filter(
            group_id=self.id,
        ).select_related("individual").first()
        return primary.individual if primary else None

    class Meta:
        model = Group
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "code": ["iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "location": ["isnull"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        primary_recipients = authoritative_primary_worker_memberships().select_related("individual")
        return Group.get_queryset(queryset, info.context.user).prefetch_related(
            Prefetch(
                "groupindividuals",
                queryset=primary_recipients,
                to_attr="primary_worker_memberships",
            ),
        )


class GroupHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)
    head = graphene.Field(IndividualGQLType)

    def resolve_json_ext(self, info):
        return _json_ext_without_payment_details(self.json_ext)

    def resolve_head(self, info):
        return Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        ).first()

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = Group.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_group_query = Group.get_queryset(None, info.context.user)
        accessible_groups = gql_optimizer.query(accessible_group_query, info)
        accessible_uuids = set(accessible_groups.values_list('uuid', flat=True))
        return queryset.filter(id__in=accessible_uuids)


class GroupIndividualGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = GroupIndividual
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "role": ["exact", "iexact", "istartswith", "icontains"],
            "recipient_type": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return GroupIndividual.get_queryset(queryset, info.context.user)


class GroupIndividualHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = GroupIndividual.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "role": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_group_query = Group.get_queryset(None, info.context.user)
        accessible_groups = gql_optimizer.query(accessible_group_query, info)
        accessible_uuids = set(accessible_groups.values_list('uuid', flat=True))
        return queryset.filter(group__id__in=accessible_uuids)


class IndividualDataUploadQGLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataUploadRecords
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "workflow": ["exact", "iexact", "startswith", "istartswith", "contains", "icontains"],
            **prefix_filterset("data_upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return _filter_by_accessible_uploads(
            queryset, info, "data_upload_id"
        )


class GroupDataSourceGQLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = GroupDataSource
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
            **prefix_filterset("upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return _filter_by_accessible_uploads(queryset, info, "upload_id")


class GlobalSchemaType(graphene.ObjectType):
    schema = graphene.JSONString()
