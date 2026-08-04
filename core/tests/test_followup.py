from datetime import timedelta

from django.db.models import Q

from core import constants as C
from core.models import Consultation, ConsultationSlot, FollowupRule, Token
from core.services.followup import (
    book_followup,
    book_followup_via_slot,
    is_fee_exempt,
    list_patient_followup_opportunities,
)
from core.tests.base import OPDTestCase


class PatientFollowupBookingTests(OPDTestCase):
    def setUp(self):
        super().setUp()
        self.patient = self.create_patient(phone='9800000088', username='patient_fu')
        self.doctor_user, self.doctor = self.create_doctor('doctor_fu', 'Follow Doctor')
        self.slot = self.create_slot(self.doctor, 'morning')
        self.token = self.create_token(
            self.slot,
            patient=self.patient,
            status=C.COMPLETED,
            phone='9800000088',
            patient_name='Follow Patient',
            token_number='M1',
        )
        self.ensure_followup_rule(7)
        Consultation.objects.create(
            token=self.token,
            diagnosis='Hypertension review',
            followup_date=self.today,
            requires_followup=True,
        )

    def test_fee_exempt_followup_within_window(self):
        self.assertTrue(is_fee_exempt(self.token, self.today))
        followup, fee_exempt = book_followup(self.token, self.today, self.patient)
        self.assertTrue(fee_exempt)
        self.assertTrue(followup.is_followup)
        self.assertTrue(followup.fee_exempted)
        self.assertEqual(followup.original_token_id, self.token.id)

    def test_book_followup_via_slot(self):
        tomorrow = self.today + timedelta(days=1)
        slot2 = ConsultationSlot.objects.create(
            doctor=self.doctor,
            date=tomorrow,
            slot_type='afternoon',
        )
        followup, fee_exempt = book_followup_via_slot(self.token, slot2, self.patient)
        self.assertTrue(fee_exempt)
        self.assertEqual(followup.slot_id, slot2.id)
        self.assertTrue(followup.is_followup)

    def test_no_reminder_after_exemption_period(self):
        self.slot.date = self.today - timedelta(days=10)
        self.slot.save(update_fields=['date'])
        q = Q(token__patient_id=self.patient.id) | Q(token__patient_phone=self.patient.phone)
        self.assertEqual(list_patient_followup_opportunities(q), [])

    def test_followup_exempt_within_seven_days_not_three(self):
        rule = FollowupRule.get_active()
        self.assertEqual(rule.exempt_within_days, 7)
        day_six = self.token.slot.date + timedelta(days=6)
        self.assertTrue(is_fee_exempt(self.token, day_six))
        day_eight = self.token.slot.date + timedelta(days=8)
        self.assertFalse(is_fee_exempt(self.token, day_eight))
