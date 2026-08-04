from decimal import Decimal

from django.utils import timezone

from core import constants as C
from core.models import LabOrder, LabQueueEntry, PharmacyQueueEntry, Token
from core.services.workflow import after_lab_report_uploaded, complete_consultation, _normalize_lab_test_names
from core.tests.base import OPDTestCase
from core.views.lab import lab_queue
from core.views.reception import pay_lab_fee
from core.views.esewa_payments import esewa_initiate_lab_order


class LabPaymentQueueFlowTests(OPDTestCase):
    def test_normalize_lab_test_names_string_not_chars(self):
        self.assertEqual(
            _normalize_lab_test_names('Complete Blood Count (CBC)'),
            ['Complete Blood Count (CBC)'],
        )

    def setUp(self):
        super().setUp()
        self.reception, self.lab_tech, _, _ = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor('doctor_lab')
        self.slot = self.create_slot(self.doctor, 'morning')
        self.token = self.create_token(
            self.slot,
            status=C.CONSULTING,
            phone='9800000001',
            patient_name='Lab Patient',
        )

    def test_lab_fee_payment_sends_order_to_lab_dashboard(self):
        complete_consultation(
            self.token,
            symptoms='fever',
            diagnosis='malaria screen',
            lab_tests=['Complete Blood Count (CBC)'],
        )
        order = LabOrder.objects.get(token=self.token)
        self.assertEqual(order.status, 'fee_pending')

        pay_response = self.api_post(
            pay_lab_fee,
            f'/api/core/reception/lab-pay/{order.id}/',
            {'amount': float(order.fee)},
            user=self.reception,
            order_id=order.id,
        )
        self.assertTrue(pay_response.data['success'])

        order.refresh_from_db()
        self.assertEqual(order.status, 'in_queue')
        entry = order.queue_entry
        self.assertTrue(entry.lab_fee_paid)

        queue_response = self.api_get(lab_queue, '/api/core/lab/queue/', self.lab_tech)
        self.assertTrue(queue_response.data['success'])
        pending_ids = [item['order_id'] for item in queue_response.data['pending']]
        self.assertIn(order.id, pending_ids)

    def test_paid_lab_queue_visible_regardless_of_appointment_date(self):
        complete_consultation(
            self.token,
            symptoms='fever',
            diagnosis='screen',
            lab_tests=['Complete Blood Count (CBC)'],
        )
        order = LabOrder.objects.get(token=self.token)
        self.api_post(
            pay_lab_fee,
            f'/api/core/reception/lab-pay/{order.id}/',
            {'amount': float(order.fee)},
            user=self.reception,
            order_id=order.id,
        )

        self.slot.date = self.today - timezone.timedelta(days=3)
        self.slot.save(update_fields=['date'])

        queue_response = self.api_get(lab_queue, '/api/core/lab/queue/', self.lab_tech)
        pending_ids = [item['order_id'] for item in queue_response.data['pending']]
        self.assertIn(order.id, pending_ids)

    def test_lab_and_pharmacy_run_in_parallel(self):
        result = complete_consultation(
            self.token,
            symptoms='fever',
            diagnosis='infection',
            medicines=[{'name': 'Amoxicillin', 'dosage': '500mg', 'frequency': 'TID'}],
            lab_tests=['Complete Blood Count (CBC)'],
        )
        self.token.refresh_from_db()

        self.assertTrue(result['requires_lab'])
        self.assertTrue(result['requires_pharmacy'])
        self.assertEqual(self.token.status, C.PENDING_LAB)
        self.assertTrue(
            PharmacyQueueEntry.objects.filter(token=self.token, status=C.PHARMACY_WAITING).exists()
        )

        pharmacy = PharmacyQueueEntry.objects.get(token=self.token)
        pharmacy.complete()
        self.token.refresh_from_db()
        self.assertEqual(pharmacy.status, C.PHARMACY_DONE)
        self.assertEqual(self.token.status, C.PENDING_LAB)

        order = LabOrder.objects.get(token=self.token)
        order.status = 'completed'
        order.save(update_fields=['status'])
        after_lab_report_uploaded(order)
        self.token.refresh_from_db()
        self.assertEqual(self.token.status, C.COMPLETED)


class PatientLabSelfPayTests(OPDTestCase):
    def setUp(self):
        super().setUp()
        self.patient_user = self.create_patient(phone='9800000099', username='patient_lab_pay')
        self.reception, _, _, _ = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor('doctor_lab2')
        self.slot = self.create_slot(self.doctor, 'morning')
        self.token = self.create_token(
            self.slot,
            patient=self.patient_user,
            status=C.CONSULTING,
            phone='9800000099',
            patient_name='Lab Patient',
        )

    def test_patient_self_pay_removes_from_reception_queue(self):
        complete_consultation(
            self.token,
            symptoms='fever',
            diagnosis='malaria screen',
            lab_tests=['Complete Blood Count (CBC)'],
        )
        order = LabOrder.objects.get(token=self.token)
        self.assertEqual(order.status, 'fee_pending')

        self.pay_lab_via_esewa(order, self.patient_user)

        order.refresh_from_db()
        self.assertEqual(order.status, 'in_queue')

        from core.views.reception import reception_lab_payments

        queue_response = self.api_get(reception_lab_payments, '/api/core/reception/lab-payments/', self.reception)
        token_ids = [row['token_id'] for row in queue_response.data['lab_payments']]
        self.assertNotIn(self.token.id, token_ids)

    def test_duplicate_patient_payment_rejected(self):
        complete_consultation(
            self.token,
            symptoms='fever',
            diagnosis='screen',
            lab_tests=['Complete Blood Count (CBC)'],
        )
        order = LabOrder.objects.get(token=self.token)

        self.pay_lab_via_esewa(order, self.patient_user)

        second_response = self.api_post(
            esewa_initiate_lab_order,
            f'/api/core/payments/esewa/lab-order/{order.id}/',
            {},
            user=self.patient_user,
            order_id=order.id,
        )
        self.assertFalse(second_response.data['success'])
        self.assertEqual(second_response.status_code, 400)
