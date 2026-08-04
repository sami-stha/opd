"""Reception registered-patient list and serial Patient ID assignment."""
from django.utils import timezone

from accounts.models import PatientSerial, User
from core.models import ConsultationSlot, Token
from core.tests.base import OPDTestCase
from core.views.reception import reception_patients, reception_unbooked_patients, register_walkin_patient


class ReceptionRegisteredPatientsTests(OPDTestCase):
    def setUp(self):
        super().setUp()
        self.reception, _, _, _ = self.create_staff()
        PatientSerial.objects.all().delete()
        User.objects.filter(role='patient').delete()

    def test_reception_register_assigns_next_serial(self):
        res1 = self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'First Patient', 'phone': '9811000001', 'age': 30},
            user=self.reception,
        )
        self.assertTrue(res1.data['success'])
        self.assertEqual(res1.data['patient']['patient_id'], 'PAT0001')

        res2 = self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Second Patient', 'phone': '9811000002', 'age': 40},
            user=self.reception,
        )
        self.assertTrue(res2.data['success'])
        self.assertEqual(res2.data['patient']['patient_id'], 'PAT0002')

    def test_patients_list_only_registered_with_serial_ids(self):
        doctor_user, doctor = self.create_doctor('doc_list', 'List Doc')
        slot = self.create_slot(doctor, 'morning')
        with self.at_slot_time(slot):
            Token.objects.create(
                slot=slot,
                patient_name='Booked Only',
                patient_age=30,
                patient_phone='9811888888',
                token_number='M1',
                estimated_time=self.make_slot_datetime(slot, 30),
            )

        self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Listed Patient', 'phone': '9811000003', 'age': 28},
            user=self.reception,
        )

        res = self.api_get(reception_patients, '/api/core/reception/patients/', self.reception)
        self.assertTrue(res.data['success'])
        self.assertEqual(res.data['count'], 1)
        self.assertEqual(res.data['patients'][0]['patient_id'], 'PAT0001')
        self.assertEqual(res.data['patients'][0]['full_name'], 'Listed Patient')

    def test_patients_list_ordered_by_serial_not_date(self):
        self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Patient A', 'phone': '9811000010', 'age': 30},
            user=self.reception,
        )
        self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Patient B', 'phone': '9811000011', 'age': 31},
            user=self.reception,
        )
        self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Patient C', 'phone': '9811000012', 'age': 32},
            user=self.reception,
        )

        res = self.api_get(reception_patients, '/api/core/reception/patients/', self.reception)
        ids = [p['patient_id'] for p in res.data['patients']]
        self.assertEqual(ids, ['PAT0001', 'PAT0002', 'PAT0003'])

    def test_unbooked_today_matches_registered_without_token(self):
        doctor_user, doctor = self.create_doctor('doc_unbooked', 'Unbooked Doc')
        slot = self.create_slot(doctor, 'evening')

        reg = self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Walkin Only', 'phone': '9811000020', 'age': 45},
            user=self.reception,
        )
        patient_id = reg.data['patient']['patient_id']

        with self.at_slot_time(slot):
            Token.objects.create(
                slot=slot,
                patient_name='Booked Today',
                patient_age=30,
                patient_phone='9811000021',
                token_number='E1',
                estimated_time=self.make_slot_datetime(slot, 30),
            )

        res = self.api_get(
            reception_unbooked_patients,
            '/api/core/reception/patients/unbooked-today/',
            self.reception,
        )
        self.assertTrue(res.data['success'])
        self.assertEqual(len(res.data['patients']), 1)
        self.assertEqual(res.data['patients'][0]['patient_id'], patient_id)

    def test_patients_list_includes_demo_seed_patients(self):
        User.objects.create(
            username='demo_list',
            phone='9800100555',
            role='patient',
            first_name='Demo',
            last_name='Patient',
            age=35,
        )
        self.api_post(
            register_walkin_patient,
            '/api/core/reception/register/',
            {'full_name': 'Real Patient', 'phone': '9811000099', 'age': 28},
            user=self.reception,
        )

        res = self.api_get(reception_patients, '/api/core/reception/patients/', self.reception)
        self.assertTrue(res.data['success'])
        self.assertEqual(res.data['total_count'], 2)
        names = {p['full_name'] for p in res.data['patients']}
        self.assertIn('Demo Patient', names)
        self.assertIn('Real Patient', names)
