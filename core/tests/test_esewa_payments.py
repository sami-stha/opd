"""eSewa redirect payment flow tests."""
import base64
import json

from core.models import GatewayPaymentSession, Token
from core.services.esewa_gateway import build_payment_form_fields, new_transaction_uuid
from core.tests.base import OPDTestCase
from core.views.esewa_payments import esewa_initiate_booking, esewa_payment_success


class EsewaPaymentTests(OPDTestCase):
    def setUp(self):
        super().setUp()
        self.doctor_user, self.doctor = self.create_doctor()
        self.slot = self.create_slot(self.doctor, 'afternoon')
        self.patient = self.create_patient('9800999888', 'esewa_patient')

    def test_initiate_booking_returns_esewa_form(self):
        with self.at_slot_time(self.slot):
            res = self.api_post(
                esewa_initiate_booking,
                '/api/core/payments/esewa/booking/',
                {
                    'slot_id': self.slot.id,
                    'patient_name': 'Esewa Patient',
                    'patient_age': 32,
                    'patient_phone': '9800888777',
                },
            )
        self.assertTrue(res.data['success'])
        self.assertIn('rc-epay.esewa.com.np', res.data['form_url'])
        self.assertIn('signature', res.data['form_fields'])
        self.assertTrue(
            GatewayPaymentSession.objects.filter(
                transaction_uuid=res.data['transaction_uuid'],
                purpose='consultation_booking',
                status='pending',
            ).exists()
        )

    def test_success_callback_fulfills_booking(self):
        uuid = new_transaction_uuid('BOOK')
        session = GatewayPaymentSession.objects.create(
            transaction_uuid=uuid,
            purpose='consultation_booking',
            amount=500,
            tax_amount=0,
            service_charge=50,
            delivery_charge=0,
            total_amount=550,
            metadata={
                'slot_id': self.slot.id,
                'patient_name': 'Callback Patient',
                'patient_age': 40,
                'patient_phone': '9800777666',
                'patient_address': '',
                'is_disabled': False,
            },
        )
        payload = {
            'transaction_code': '000TEST',
            'status': 'COMPLETE',
            'total_amount': '550',
            'transaction_uuid': uuid,
            'product_code': 'EPAYTEST',
            'signed_field_names': 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names',
            'signature': 'test',
        }
        encoded = base64.b64encode(json.dumps(payload).encode('utf-8')).decode('utf-8')

        with self.at_slot_time(self.slot):
            factory = self.factory
            request = factory.get(f'/api/core/payments/esewa/success/?data={encoded}')
            response = esewa_payment_success(request)

        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.status, 'completed')
        self.assertTrue(Token.objects.filter(patient_phone='9800777666').exists())

    def test_official_uat_signature_example(self):
        from core.services.esewa_gateway import generate_signature, ESEWA_UAT_SECRET_KEY

        data = {
            'total_amount': '110',
            'transaction_uuid': '241028',
            'product_code': 'EPAYTEST',
        }
        names = ['total_amount', 'transaction_uuid', 'product_code']
        digest = __import__('hmac').new(
            ESEWA_UAT_SECRET_KEY.encode('utf-8'),
            'total_amount=110,transaction_uuid=241028,product_code=EPAYTEST'.encode('utf-8'),
            __import__('hashlib').sha256,
        ).digest()
        expected = __import__('base64').b64encode(digest).decode('utf-8')
        self.assertEqual(generate_signature(names, data), expected)
        self.assertEqual(expected, 'i94zsd3oXF6ZsSr/kGqT4sSzYQzjj1W/waxjWyRwaME=')

    def test_signature_round_trip(self):
        fields = build_payment_form_fields(
            'OPD-test-uuid',
            amount=500,
            tax_amount=0,
            service_charge=50,
            delivery_charge=0,
            total_amount=550,
        )
        names = [n.strip() for n in fields['signed_field_names'].split(',')]
        from core.services.esewa_gateway import verify_signature
        self.assertTrue(verify_signature(names, fields, fields['signature']))

    def test_build_payment_form_has_required_fields(self):
        fields = build_payment_form_fields(
            'OPD-test-uuid',
            amount=500,
            tax_amount=0,
            service_charge=50,
            delivery_charge=0,
            total_amount=550,
        )
        self.assertEqual(fields['total_amount'], '550')
        self.assertEqual(fields['transaction_uuid'], 'OPD-test-uuid')
        self.assertEqual(fields['amount'], '500')
        self.assertEqual(fields['product_service_charge'], '50')
        self.assertTrue(fields['signature'])
