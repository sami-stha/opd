"""Tests for shared doctor queue ordering (serial tokens, priority, late check-in)."""
from core.services.queue_order import (
    get_ordered_queue_tokens,
    queue_entry_sort_key,
    queue_position_for_entry,
)
from core.tests.base import OPDTestCase


class QueueOrderTests(OPDTestCase):
    def setUp(self):
        super().setUp()
        self.reception, _, _, _ = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor('doctor_queue', 'Queue Doctor')
        self.slot = self.create_slot(self.doctor, 'evening')

    def _create_checked_in(self, token_number, age, phone_suffix):
        patient = self.create_patient(
            phone=f'9800{phone_suffix:04d}',
            username=f'pat_{phone_suffix}',
        )
        with self.at_slot_time(self.slot):
            token = self.create_token(
                self.slot,
                patient=patient,
                phone=patient.phone,
                patient_name=f'Demo {token_number}',
                patient_age=age,
                token_number=token_number,
                estimated_time=self.make_slot_datetime(self.slot, 30),
            )
        self.check_in_token(token, self.reception)
        token.refresh_from_db()
        return token

    def test_serial_order_ignores_check_in_order(self):
        """E6 checked in before E3/E2 still queues as E2 → E3 → E6."""
        t6 = self._create_checked_in('E6', 36, 1006)
        t3 = self._create_checked_in('E3', 33, 1003)
        t2 = self._create_checked_in('E2', 32, 1002)

        ordered = [t.token_number for t in get_ordered_queue_tokens(self.doctor.id)]
        self.assertEqual(ordered, ['E2', 'E3', 'E6'])

        positions = {
            t2.token_number: queue_position_for_entry(t2.queue_entry),
            t3.token_number: queue_position_for_entry(t3.queue_entry),
            t6.token_number: queue_position_for_entry(t6.queue_entry),
        }
        self.assertEqual(positions, {'E2': 1, 'E3': 2, 'E6': 3})

    def test_late_check_in_queues_after_on_time_serial(self):
        t2 = self._create_checked_in('E2', 32, 2002)
        t3 = self._create_checked_in('E3', 33, 2003)
        t6 = self._create_checked_in('E6', 36, 2006)

        t6.checkin_status = 'missed'
        t6.save(update_fields=['checkin_status'])

        ordered = [t.token_number for t in get_ordered_queue_tokens(self.doctor.id)]
        self.assertEqual(ordered, ['E2', 'E3', 'E6'])

    def test_late_patient_after_on_time_even_with_lower_token(self):
        t3 = self._create_checked_in('E3', 33, 3003)
        t2 = self._create_checked_in('E2', 32, 3002)

        t2.checkin_status = 'missed'
        t2.save(update_fields=['checkin_status'])

        ordered = [t.token_number for t in get_ordered_queue_tokens(self.doctor.id)]
        self.assertEqual(ordered, ['E3', 'E2'])

    def test_elderly_priority_before_normal_serial(self):
        t2 = self._create_checked_in('E2', 32, 4002)
        elderly = self._create_checked_in('E3', 75, 4003)

        elderly.is_elderly = True
        elderly.save(update_fields=['is_elderly'])
        entry = elderly.queue_entry
        entry.priority = 'high'
        entry.save(update_fields=['priority'])

        ordered = [t.token_number for t in get_ordered_queue_tokens(self.doctor.id)]
        self.assertEqual(ordered, ['E3', 'E2'])

    def test_consulting_patient_excluded_from_waiting_order(self):
        t2 = self._create_checked_in('E2', 32, 5002)
        t3 = self._create_checked_in('E3', 33, 5003)
        t6 = self._create_checked_in('E6', 36, 5006)

        self.start_consultation_api(t2, self.doctor_user)
        t2.refresh_from_db()
        self.assertEqual(t2.status, 'consulting')

        ordered = [t.token_number for t in get_ordered_queue_tokens(self.doctor.id)]
        self.assertEqual(ordered, ['E3', 'E6'])
        self.assertEqual(queue_position_for_entry(t3.queue_entry), 1)
        self.assertEqual(queue_position_for_entry(t6.queue_entry), 2)

    def test_doctor_queue_api_matches_serial_positions(self):
        from core.views.doctor import _queue_for_doctor

        self._create_checked_in('E6', 36, 6006)
        self._create_checked_in('E3', 33, 6003)
        self._create_checked_in('E2', 32, 6002)

        queue = _queue_for_doctor(self.doctor.id)
        self.assertEqual(
            [(row['token_number'], row['position']) for row in queue],
            [('E2', 1), ('E3', 2), ('E6', 3)],
        )

    def test_sort_key_documentation_matches_rules(self):
        t2 = self._create_checked_in('E2', 32, 7002)
        t3 = self._create_checked_in('E3', 33, 7003)
        t3.checkin_status = 'missed'
        t3.save(update_fields=['checkin_status'])

        e2 = t2.queue_entry
        e3 = t3.queue_entry
        self.assertLess(queue_entry_sort_key(e2), queue_entry_sort_key(e3))
