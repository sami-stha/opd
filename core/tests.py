from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from core import constants as C
from core.models import (
    ConsultationSlot,
    DoctorProfile,
    LabOrder,
    LabQueueEntry,
    PharmacyQueueEntry,
    Token,
)
from core.services.workflow import after_lab_report_uploaded, complete_consultation, _normalize_lab_test_names
from core.views.lab import lab_queue
from core.views.reception import pay_lab_fee
from rest_framework.test import APIRequestFactory, force_authenticate


class LabPaymentQueueFlowTests(TestCase):
    def test_normalize_lab_test_names_string_not_chars(self):
        self.assertEqual(
            _normalize_lab_test_names('Complete Blood Count (CBC)'),
            ['Complete Blood Count (CBC)'],
        )

    def setUp(self):
        self.factory = APIRequestFactory()
        self.today = timezone.localdate()

        self.receptionist = User.objects.create_user(
            username='reception_lab',
            password='pass',
            role='receptionist',
        )
        self.lab_tech = User.objects.create_user(
            username='labtech1',
            password='pass',
            role='lab_tech',
        )
        doctor_user = User.objects.create_user(
            username='doctor_lab',
            password='pass',
            role='doctor',
            first_name='Test',
            last_name='Doctor',
        )
        self.doctor = DoctorProfile.objects.create(
            user=doctor_user,
            specialization='General Physician',
        )
        self.slot = ConsultationSlot.objects.create(
            doctor=self.doctor,
            date=self.today,
            slot_type='morning',
            start_time='08:00',
            end_time='11:00',
            max_tokens=20,
        )
        self.token = Token.objects.create(
            slot=self.slot,
            patient_name='Lab Patient',
            patient_age=30,
            patient_phone='9800000001',
            token_number='T1',
            status=C.CONSULTING,
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

        pay_request = self.factory.post(
            f'/api/core/reception/lab-pay/{order.id}/',
            {'amount': float(order.fee)},
            format='json',
        )
        force_authenticate(pay_request, user=self.receptionist)
        pay_response = pay_lab_fee(pay_request, order.id)
        self.assertTrue(pay_response.data['success'])

        order.refresh_from_db()
        self.assertEqual(order.status, 'in_queue')
        entry = LabQueueEntry.objects.get(lab_order=order)
        self.assertTrue(entry.lab_fee_paid)

        queue_request = self.factory.get('/api/core/lab/queue/')
        force_authenticate(queue_request, user=self.lab_tech)
        queue_response = lab_queue(queue_request)
        self.assertTrue(queue_response.data['success'])
        pending_ids = [item['order_id'] for item in queue_response.data['pending']]
        self.assertIn(order.id, pending_ids)

    def test_paid_lab_queue_visible_regardless_of_appointment_date(self):
        """Older appointments stay on the lab queue until completed."""
        complete_consultation(
            self.token,
            symptoms='fever',
            diagnosis='screen',
            lab_tests=['Complete Blood Count (CBC)'],
        )
        order = LabOrder.objects.get(token=self.token)
        pay_request = self.factory.post(
            f'/api/core/reception/lab-pay/{order.id}/',
            {'amount': float(order.fee)},
            format='json',
        )
        force_authenticate(pay_request, user=self.receptionist)
        pay_lab_fee(pay_request, order.id)

        # Simulate an order from a previous visit day
        self.slot.date = self.today - timezone.timedelta(days=3)
        self.slot.save(update_fields=['date'])

        queue_request = self.factory.get('/api/core/lab/queue/')
        force_authenticate(queue_request, user=self.lab_tech)
        queue_response = lab_queue(queue_request)
        pending_ids = [item['order_id'] for item in queue_response.data['pending']]
        self.assertIn(order.id, pending_ids)

    def test_lab_and_pharmacy_run_in_parallel(self):
        """Patients with Rx enter pharmacy immediately even if labs are pending."""
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

        # Completing pharmacy while labs pending keeps visit on lab
        pharmacy = PharmacyQueueEntry.objects.get(token=self.token)
        pharmacy.complete()
        self.token.refresh_from_db()
        self.assertEqual(pharmacy.status, C.PHARMACY_DONE)
        self.assertEqual(self.token.status, C.PENDING_LAB)

        # Finishing labs after pharmacy completes the visit
        order = LabOrder.objects.get(token=self.token)
        order.status = 'completed'
        order.save(update_fields=['status'])
        after_lab_report_uploaded(order)
        self.token.refresh_from_db()
        self.assertEqual(self.token.status, C.COMPLETED)


class PatientFollowupBookingTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.patient = User.objects.create_user(
            username='patient_fu',
            password='pass',
            role='patient',
            phone='9800000088',
        )
        doctor_user = User.objects.create_user(
            username='doctor_fu',
            password='pass',
            role='doctor',
            first_name='Follow',
            last_name='Doctor',
        )
        self.doctor = DoctorProfile.objects.create(
            user=doctor_user,
            specialization='General Physician',
        )
        self.slot = ConsultationSlot.objects.create(
            doctor=self.doctor,
            date=self.today,
            slot_type='morning',
            start_time='08:00',
            end_time='11:00',
            max_tokens=20,
        )
        self.token = Token.objects.create(
            slot=self.slot,
            patient=self.patient,
            patient_name='Follow Patient',
            patient_age=40,
            patient_phone='9800000088',
            token_number='M1',
            status=C.COMPLETED,
        )
        from core.models import Consultation
        Consultation.objects.create(
            token=self.token,
            diagnosis='Hypertension review',
            followup_date=self.today,
            requires_followup=True,
        )

    def test_fee_exempt_followup_within_window(self):
        from core.services.followup import book_followup, is_fee_exempt
        self.assertTrue(is_fee_exempt(self.token, self.today))
        followup, fee_exempt = book_followup(self.token, self.today, self.patient)
        self.assertTrue(fee_exempt)
        self.assertTrue(followup.is_followup)
        self.assertTrue(followup.fee_exempted)
        self.assertEqual(followup.original_token_id, self.token.id)
    def setUp(self):
        self.factory = APIRequestFactory()
        self.today = timezone.localdate()

        self.patient_user = User.objects.create_user(
            username='patient_lab_pay',
            password='pass',
            role='patient',
            phone='9800000099',
        )
        self.receptionist = User.objects.create_user(
            username='reception_lab2',
            password='pass',
            role='receptionist',
        )
        doctor_user = User.objects.create_user(
            username='doctor_lab2',
            password='pass',
            role='doctor',
            first_name='Test',
            last_name='Doctor',
        )
        self.doctor = DoctorProfile.objects.create(
            user=doctor_user,
            specialization='General Physician',
        )
        self.slot = ConsultationSlot.objects.create(
            doctor=self.doctor,
            date=self.today,
            slot_type='morning',
            start_time='08:00',
            end_time='11:00',
            max_tokens=20,
        )
        self.token = Token.objects.create(
            slot=self.slot,
            patient=self.patient_user,
            patient_name='Lab Patient',
            patient_age=30,
            patient_phone='9800000099',
            token_number='T9',
            status=C.CONSULTING,
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

        pay_request = self.factory.post(
            f'/api/core/patient/lab-pay/{order.id}/',
            {},
            format='json',
        )
        force_authenticate(pay_request, user=self.patient_user)
        from core.views.patient_portal import patient_pay_lab_fee
        pay_response = patient_pay_lab_fee(pay_request, order.id)
        self.assertTrue(pay_response.data['success'])

        order.refresh_from_db()
        self.assertEqual(order.status, 'in_queue')

        from core.views.reception import reception_lab_payments
        queue_request = self.factory.get('/api/core/reception/lab-payments/')
        force_authenticate(queue_request, user=self.receptionist)
        queue_response = reception_lab_payments(queue_request)
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
        from core.views.patient_portal import patient_pay_lab_fee

        first = self.factory.post(f'/api/core/patient/lab-pay/{order.id}/', {}, format='json')
        force_authenticate(first, user=self.patient_user)
        first_response = patient_pay_lab_fee(first, order.id)
        self.assertTrue(first_response.data['success'])

        second = self.factory.post(f'/api/core/patient/lab-pay/{order.id}/', {}, format='json')
        force_authenticate(second, user=self.patient_user)
        second_response = patient_pay_lab_fee(second, order.id)
        self.assertFalse(second_response.data['success'])
        self.assertEqual(second_response.status_code, 400)
