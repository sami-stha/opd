from django.test import TestCase

from accounts.models import PatientSerial, User


class PatientSerialTests(TestCase):
    def test_next_code_starts_at_pat0001(self):
        code = PatientSerial.next_code()
        self.assertEqual(code, 'PAT0001')

    def test_next_code_continues_sequentially(self):
        PatientSerial.next_code()
        code = PatientSerial.next_code()
        self.assertEqual(code, 'PAT0002')

    def test_demo_patients_do_not_receive_patient_codes(self):
        for i in range(3):
            User.objects.create(
                username=f'demo_{i}',
                phone=f'9800100{i:03d}',
                role='patient',
                first_name='Demo',
            )
        codes = list(User.objects.filter(role='patient').values_list('patient_code', flat=True))
        self.assertEqual(codes, [None, None, None])

    def test_next_code_continues_after_demo_patients_removed(self):
        for i in range(3):
            User.objects.create(
                username=f'demo_{i}',
                phone=f'9800100{i:03d}',
                role='patient',
                first_name='Demo',
            )
        User.objects.filter(phone__startswith='9800100').delete()
        PatientSerial.sync_from_database()

        user = User.objects.create(
            username='manual_1',
            phone='9811111111',
            role='patient',
            first_name='Manual',
        )
        self.assertEqual(user.patient_code, 'PAT0001')

    def test_manual_registration_ignores_demo_patients_for_serial(self):
        for i in range(5):
            User.objects.create(
                username=f'demo_{i}',
                phone=f'9800100{i:03d}',
                role='patient',
                first_name='Demo',
            )
        user = User.objects.create(
            username='manual_1',
            phone='9811111111',
            role='patient',
            first_name='Manual',
        )
        self.assertEqual(user.patient_code, 'PAT0001')

    def test_renumber_removes_gaps(self):
        User.objects.create(username='a', phone='9810000001', role='patient', first_name='A')
        User.objects.create(username='b', phone='9810000002', role='patient', first_name='B')
        User.objects.filter(phone='9810000001').delete()
        PatientSerial.sync_from_database()
        User.objects.create(username='c', phone='9810000003', role='patient', first_name='C')

        count = PatientSerial.renumber_all_patients_serial()
        self.assertEqual(count, 2)
        codes = sorted(User.objects.filter(role='patient').values_list('patient_code', flat=True))
        self.assertEqual(codes, ['PAT0001', 'PAT0002'])

    def test_renumber_clears_demo_patient_codes(self):
        User.objects.create(username='real', phone='9810000099', role='patient', first_name='Real')
        User.objects.create(username='demo', phone='9800100999', role='patient', first_name='Demo')
        User.objects.filter(phone='9810000099').update(patient_code='PAT0099')
        User.objects.filter(phone='9800100999').update(patient_code='PAT1814')

        count = PatientSerial.renumber_all_patients_serial()
        self.assertEqual(count, 1)
        real = User.objects.get(phone='9810000099')
        demo = User.objects.get(phone='9800100999')
        self.assertEqual(real.patient_code, 'PAT0001')
        self.assertEqual(demo.patient_code, '')
