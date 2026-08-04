# Generated manually for GatewayPaymentSession

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_user_gender'),
        ('core', '0016_followuprule_exempt_within_days_7'),
    ]

    operations = [
        migrations.CreateModel(
            name='GatewayPaymentSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transaction_uuid', models.CharField(db_index=True, max_length=64, unique=True)),
                ('gateway', models.CharField(default='esewa', max_length=20)),
                ('purpose', models.CharField(
                    choices=[
                        ('consultation_booking', 'Consultation booking'),
                        ('lab_order', 'Lab order'),
                        ('lab_token', 'Lab token bulk'),
                    ],
                    max_length=30,
                )),
                ('status', models.CharField(
                    choices=[
                        ('pending', 'Pending'),
                        ('completed', 'Completed'),
                        ('failed', 'Failed'),
                        ('cancelled', 'Cancelled'),
                    ],
                    default='pending',
                    max_length=20,
                )),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('tax_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('service_charge', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('delivery_charge', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('total_amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('esewa_transaction_code', models.CharField(blank=True, max_length=50)),
                ('error_message', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(null=True, blank=True)),
                ('patient_user', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='gateway_sessions',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('payment', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='gateway_sessions',
                    to='core.payment',
                )),
                ('token', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='gateway_sessions',
                    to='core.token',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
