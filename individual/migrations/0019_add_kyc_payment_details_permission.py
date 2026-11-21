from django.db import migrations


PAYMENT_DETAILS_RIGHT = 159006
IMIS_ADMINISTRATOR_SYSTEM = 64


def add_permission_to_administrators(apps, schema_editor):
    Role = apps.get_model("core", "Role")
    RoleRight = apps.get_model("core", "RoleRight")

    for role in Role.objects.filter(
        is_system=IMIS_ADMINISTRATOR_SYSTEM,
        validity_to__isnull=True,
    ):
        if not RoleRight.objects.filter(
            role=role,
            right_id=PAYMENT_DETAILS_RIGHT,
            validity_to__isnull=True,
        ).exists():
            RoleRight.objects.create(
                role=role,
                right_id=PAYMENT_DETAILS_RIGHT,
                audit_user_id=1,
            )


def remove_permission_from_administrators(apps, schema_editor):
    RoleRight = apps.get_model("core", "RoleRight")
    Role = apps.get_model("core", "Role")
    RoleRight.objects.filter(
        role__in=Role.objects.filter(is_system=IMIS_ADMINISTRATOR_SYSTEM),
        right_id=PAYMENT_DETAILS_RIGHT,
        validity_to__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("individual", "0018_alter_groupindividual_role_and_more"),
    ]

    operations = [
        migrations.RunPython(
            add_permission_to_administrators,
            remove_permission_from_administrators,
        ),
    ]
