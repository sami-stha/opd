"""End-to-end OPD workflow tests (online patient path + walk-in path)."""
from datetime import timedelta

from django.utils import timezone

from accounts.models import User
from core import constants as C
from core.models import LabOrder, PharmacyQueueEntry, Token
from core.services.analytics import compute_daily_analytics, compute_kpis
from core.tests.base import OPDTestCase
from core.views.admin_api import analytics as analytics_view
from core.views.lab import lab_complete_test, lab_queue, lab_start_test
from core.views.patient_portal import (
    patient_book_followup,
    patient_followups,
    patient_pay_lab_fee,
)
from core.views.pharmacy import (
    pharmacy_complete_dispense,
    pharmacy_mark_ready_view,
    pharmacy_queue,
    pharmacy_start_dispense,
)
from core.views.reception import pay_lab_fee, reception_lab_payments


class FullOnlinePatientWorkflowTests(OPDTestCase):
    """Path A: book online → check-in → consult → lab self-pay → lab → pharmacy → follow-up."""

    def setUp(self):
        super().setUp()
        self.reception, self.lab_tech, self.pharmacist, self.admin = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor('doctor_afternoon', 'Naresh Kharbuja')
        self.slot = self.create_slot(self.doctor, 'afternoon')
        self.tomorrow_slot = self.create_slot(
            self.doctor, 'afternoon', date=self.today + timedelta(days=1),
        )
        self.patient = self.create_patient(phone='9800555666', username='patient_e2e')
        self.ensure_followup_rule(7)

    def test_full_online_patient_journey(self):
        phone = '9800555666'
        token, _ = self.book_via_api(
            self.slot, 'E2E Patient', 32, phone, patient_user=self.patient,
        )
        token.patient = self.patient
        token.save(update_fields=['patient'])

        # Reception: no pending lab payments yet
        lab_queue_res = self.api_get(reception_lab_payments, '/api/core/reception/lab-payments/', self.reception)
        self.assertTrue(lab_queue_res.data['success'])
        self.assertEqual(len(lab_queue_res.data.get('lab_payments', [])), 0)

        self.check_in_token(token, self.reception)
        self.assertEqual(token.status, C.CHECKED_IN)

        self.start_consultation_api(token, self.doctor_user)
        self.assertEqual(token.status, C.CONSULTING)

        followup_date = (self.today + timedelta(days=3)).isoformat()
        token, _ = self.complete_consultation_api(
            token,
            self.doctor_user,
            followup_date=followup_date,
        )
        self.assertEqual(token.status, C.PENDING_LAB)
        self.assertTrue(PharmacyQueueEntry.objects.filter(token=token).exists())

        order = LabOrder.objects.get(token=token)
        self.assertEqual(order.status, 'fee_pending')

        # Patient self-pay removes from reception lab queue
        pay_res = self.api_post(
            patient_pay_lab_fee,
            f'/api/core/patient/lab-pay/{order.id}/',
            {},
            user=self.patient,
            order_id=order.id,
        )
        self.assertTrue(pay_res.data['success'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'in_queue')

        reception_lab = self.api_get(reception_lab_payments, '/api/core/reception/lab-payments/', self.reception)
        token_ids = [row['token_id'] for row in reception_lab.data.get('lab_payments', [])]
        self.assertNotIn(token.id, token_ids)

        # Lab: start → complete
        start_res = self.api_post(
            lab_start_test,
            f'/api/core/lab/start/{order.id}/',
            {},
            user=self.lab_tech,
            order_id=order.id,
        )
        self.assertTrue(start_res.data['success'])

        complete_res = self.api_post(
            lab_complete_test,
            f'/api/core/lab/complete/{order.id}/',
            {'findings': 'Normal CBC'},
            user=self.lab_tech,
            order_id=order.id,
        )
        self.assertTrue(complete_res.data['success'])
        token.refresh_from_db()
        self.assertEqual(token.status, C.PENDING_PHARMACY)

        # Pharmacy flow
        entry = PharmacyQueueEntry.objects.get(token=token)
        start_ph = self.api_post(
            pharmacy_start_dispense,
            f'/api/core/pharmacy/{entry.id}/start/',
            {},
            user=self.pharmacist,
            entry_id=entry.id,
        )
        self.assertTrue(start_ph.data['success'])

        ready_ph = self.api_post(
            pharmacy_mark_ready_view,
            f'/api/core/pharmacy/{entry.id}/ready/',
            {},
            user=self.pharmacist,
            entry_id=entry.id,
        )
        self.assertTrue(ready_ph.data['success'])

        done_ph = self.api_post(
            pharmacy_complete_dispense,
            f'/api/core/pharmacy/{entry.id}/complete/',
            {'amount': float(entry.total_bill)},
            user=self.pharmacist,
            entry_id=entry.id,
        )
        self.assertTrue(done_ph.data['success'])
        token.refresh_from_db()
        self.assertEqual(token.status, C.COMPLETED)

        # Follow-up reminder on patient portal
        fu_res = self.api_get(patient_followups, '/api/core/patient/followups/', self.patient)
        self.assertTrue(fu_res.data['success'])
        opportunities = fu_res.data.get('followups', [])
        self.assertTrue(any(o['original_token_id'] == token.id and o['can_book'] for o in opportunities))

        # Book fee-exempt follow-up via portal API
        book_fu = self.api_post(
            patient_book_followup,
            '/api/core/patient/followup-book/',
            {
                'original_token_id': token.id,
                'slot_id': self.tomorrow_slot.id,
            },
            user=self.patient,
        )
        self.assertTrue(book_fu.data['success'])
        self.assertTrue(book_fu.data['fee_exempted'])
        followup = Token.objects.get(is_followup=True, original_token_id=token.id)
        self.assertTrue(followup.is_followup)
        self.assertTrue(followup.fee_exempted)

        # Analytics reflect today's activity
        kpis = compute_kpis(self.today)
        self.assertGreaterEqual(kpis['completed'], 1)
        self.assertGreaterEqual(kpis['system_throughput'], 1)

        analytics_res = self.api_get(analytics_view, '/api/core/analytics/', self.admin)
        self.assertTrue(analytics_res.data['success'])
        self.assertGreaterEqual(analytics_res.data['completed'], 1)
        self.assertIn('charts', analytics_res.data)

        compute_daily_analytics(self.today)
        slot_kpis = next(
            (d for d in kpis['doctor_queues'] if d['doctor_id'] == self.doctor.id),
            None,
        )
        self.assertIsNotNone(slot_kpis)
        self.assertGreaterEqual(slot_kpis['completed'], 1)


class WalkInReceptionWorkflowTests(OPDTestCase):
    """Path B: reception register + book + check-in → consult (minimal slot test)."""

    def setUp(self):
        super().setUp()
        self.reception, _, _, _ = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor('doctor_evening', 'Sita Thapa')
        self.slot = self.create_slot(self.doctor, 'evening')

    def test_walk_in_register_book_checkin_consult_complete(self):
        from core.views.reception import register_walkin_patient

        reg = self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {
                'full_name': 'Walk-in Patient',
                'phone': '9800777888',
                'age': 45,
                'address': 'Kathmandu',
            },
            user=self.reception,
        )
        self.assertTrue(reg.data['success'])
        patient_id = reg.data['patient']['patient_id']

        token, _ = self.book_via_api(
            self.slot,
            'Walk-in Patient',
            45,
            '9800777888',
            patient_user=User.objects.get(phone='9800777888', role='patient'),
        )
        self.assertEqual(token.patient_phone, '9800777888')

        self.check_in_token(token, self.reception)
        self.start_consultation_api(token, self.doctor_user)
        token, _ = self.complete_consultation_api(
            token,
            self.doctor_user,
            lab_tests=[],
            medicines=[{'name': 'Ibuprofen', 'dosage': '400mg', 'frequency': 'BID'}],
        )
        self.assertEqual(token.status, C.PENDING_PHARMACY)


class ReceptionLabPaymentTests(OPDTestCase):
    """Reception lab payment path (alternate to patient self-pay)."""

    def setUp(self):
        super().setUp()
        self.reception, self.lab_tech, _, _ = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor()
        self.slot = self.create_slot(self.doctor, 'morning')
        self.patient = self.create_patient(phone='9800999888')

    def test_reception_lab_pay_then_lab_queue(self):
        token = self.create_token(
            self.slot,
            patient=self.patient,
            status=C.CONSULTING,
            phone='9800999888',
            patient_name='Lab Reception Patient',
            token_number='M9',
        )
        from core.services.workflow import complete_consultation

        complete_consultation(
            token,
            symptoms='fever',
            diagnosis='screen',
            lab_tests=['Complete Blood Count (CBC)'],
        )
        order = LabOrder.objects.get(token=token)

        pay_res = self.api_post(
            pay_lab_fee,
            f'/api/core/reception/lab-pay/{order.id}/',
            {'amount': float(order.fee)},
            user=self.reception,
            order_id=order.id,
        )
        self.assertTrue(pay_res.data['success'])
        order.refresh_from_db()
        self.assertEqual(order.status, 'in_queue')

        queue_res = self.api_get(lab_queue, '/api/core/lab/queue/', self.lab_tech)
        pending_ids = [item['order_id'] for item in queue_res.data.get('pending', [])]
        self.assertIn(order.id, pending_ids)
