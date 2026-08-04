"""Shared fixtures and helpers for OPD workflow tests."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import User
from core import constants as C
from core.models import ConsultationSlot, DoctorProfile, FollowupRule, Token


class OPDTestCase(TestCase):
    factory = APIRequestFactory()

    def setUp(self):
        self.today = timezone.localdate()

    @staticmethod
    def make_slot_datetime(slot, minutes_after_start=30):
        start_h, start_m = map(int, slot.start_time.split(':'))
        base = datetime.combine(slot.date, datetime.strptime(f'{start_h}:{start_m:02d}', '%H:%M').time())
        fake = base + timedelta(minutes=minutes_after_start)
        return timezone.make_aware(fake)

    @contextmanager
    def at_slot_time(self, slot, minutes_after_start=30):
        fake = self.make_slot_datetime(slot, minutes_after_start)
        with patch('django.utils.timezone.localtime', return_value=fake), patch(
            'django.utils.timezone.now', return_value=fake,
        ):
            yield fake

    def create_user(self, username, role, **extra):
        defaults = {'password': 'pass'}
        defaults.update(extra)
        return User.objects.create_user(username=username, **defaults, role=role)

    def create_doctor(self, username='doctor_test', name='Test Doctor'):
        parts = name.split()
        user = self.create_user(
            username,
            'doctor',
            first_name=parts[0],
            last_name=' '.join(parts[1:]) if len(parts) > 1 else '',
        )
        profile = DoctorProfile.objects.create(
            user=user,
            specialization='General Physician',
            qualification='MBBS',
        )
        return user, profile

    def create_slot(self, doctor, slot_type='afternoon', date=None):
        from core.models import SlotTypeConfig
        from core.services.slot_config import ensure_slot_type_configs

        ensure_slot_type_configs()
        cfg = SlotTypeConfig.objects.get(slot_type=slot_type)
        cfg.assigned_doctor = doctor
        cfg.save(update_fields=['assigned_doctor'])
        return ConsultationSlot.objects.create(
            doctor=doctor,
            date=date or self.today,
            slot_type=slot_type,
        )

    def create_staff(self):
        reception = self.create_user('reception_test', 'receptionist')
        lab_tech = self.create_user('labtech_test', 'lab_tech')
        pharmacist = self.create_user('pharmacist_test', 'pharmacist')
        admin = self.create_user('admin_test', 'admin')
        return reception, lab_tech, pharmacist, admin

    def create_patient(self, phone='9800111222', username='patient_test'):
        return self.create_user(username, 'patient', phone=phone, age=30)

    def ensure_followup_rule(self, days=7):
        FollowupRule.objects.filter(is_active=True).update(is_active=False)
        return FollowupRule.objects.create(exempt_within_days=days, is_active=True)

    def api_get(self, view, path, user=None, **url_kwargs):
        request = self.factory.get(path)
        if user:
            force_authenticate(request, user=user)
        return view(request, **url_kwargs)

    def api_post(self, view, path, data, user=None, **url_kwargs):
        request = self.factory.post(path, data, format='json')
        if user:
            force_authenticate(request, user=user)
        return view(request, **url_kwargs)

    def create_token(self, slot, status=C.BOOKED, patient=None, phone='9800000001', **kwargs):
        data = {
            'patient_name': kwargs.pop('patient_name', 'Test Patient'),
            'patient_age': kwargs.pop('patient_age', 30),
            'patient_phone': phone,
            'status': status,
        }
        data.update(kwargs)
        if patient:
            data['patient'] = patient
        with self.at_slot_time(slot):
            data['estimated_time'] = self.make_slot_datetime(slot, 30)
            return Token.objects.create(slot=slot, **data)

    def book_via_api(
        self,
        slot,
        patient_name='Test Patient',
        patient_age=30,
        phone='9800111222',
        patient_user=None,
    ):
        from core.views.booking import book_token

        patient = patient_user or User.objects.filter(phone=phone, role='patient').first()
        payload = {
            'slot_id': slot.id,
            'patient_name': patient_name,
            'patient_age': patient_age,
            'patient_phone': phone,
            'payment_method': 'esewa',
        }
        if patient and patient.patient_code:
            payload['patient_id'] = patient.patient_code

        with self.at_slot_time(slot):
            response = self.api_post(
                book_token,
                '/api/core/book/',
                payload,
            )
        self.assertTrue(response.data['success'], response.data.get('error'))
        token = Token.objects.get(id=response.data['token']['token_id'])
        return token, response.data

    def check_in_token(self, token, receptionist):
        from core.views.reception import check_in_patient

        with self.at_slot_time(token.slot):
            token.refresh_from_db()
            if token.status == C.EXPIRED:
                token.status = C.BOOKED
                token.save(update_fields=['status'])
            response = self.api_post(
                check_in_patient,
                f'/api/core/check-in/{token.id}/',
                {'is_elderly': False, 'is_disabled': False},
                user=receptionist,
                token_id=token.id,
            )
        self.assertTrue(response.data['success'], response.data.get('error'))
        token.refresh_from_db()
        return token

    def start_consultation_api(self, token, doctor_user):
        from core.views.doctor import start_consultation

        with self.at_slot_time(token.slot):
            response = self.api_post(
                start_consultation,
                f'/api/core/start-consult/{token.id}/',
                {},
                user=doctor_user,
                token_id=token.id,
            )
        self.assertTrue(response.data['success'], response.data.get('error'))
        token.refresh_from_db()
        return token

    def complete_consultation_api(
        self,
        token,
        doctor_user,
        lab_tests=None,
        medicines=None,
        followup_date=None,
    ):
        from core.views.doctor import complete_consultation

        payload = {
            'symptoms': 'fever and cough',
            'diagnosis': 'common cold',
            'notes': 'rest and fluids',
            'medicines': medicines or [{'name': 'Paracetamol', 'dosage': '500mg', 'frequency': 'TID'}],
            'lab_tests': lab_tests if lab_tests is not None else ['Complete Blood Count (CBC)'],
            'followup_date': followup_date,
        }
        response = self.api_post(
            complete_consultation,
            f'/api/core/complete-consult/{token.id}/',
            payload,
            user=doctor_user,
            token_id=token.id,
        )
        self.assertTrue(response.data['success'], response.data.get('error'))
        token.refresh_from_db()
        return token, response.data
