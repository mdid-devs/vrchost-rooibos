# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def remove_equivalency(apps, schema_editor):
    Field = apps.get_model("data", "Field")
    try:
        Field.objects.get(name='subject', standard__prefix='dc') \
            .equivalent.filter(name='styleperiod', standard__prefix='vra') \
            .delete()
    except Field.DoesNotExist:
        pass
    try:
        Field.objects.get(name='styleperiod', standard__prefix='vra') \
            .equivalent.filter(name='subject', standard__prefix='dc') \
            .delete()
    except Field.DoesNotExist:
        pass


class Migration(migrations.Migration):

    dependencies = [
        ('data', '0008_system_standard_and_alt_text_field'),
    ]

    operations = [
        migrations.RunPython(remove_equivalency, migrations.RunPython.noop),
    ]
