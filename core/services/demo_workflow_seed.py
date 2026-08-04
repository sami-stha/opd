"""Seed realistic demo patient journeys into the live database for UI/analytics testing."""
import random
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

from accounts.models import User
from core import constants as C
from core.models import (
    ConsultationSlot,
    DoctorProfile,
    FollowupRule,
    LabReport,
    Payment,
    PharmacyQueueEntry,
    SlotTypeConfig,
    Token,
)
from core.services.analytics import compute_daily_analytics, compute_kpis
from core.services.followup import book_followup_via_slot
from core.services.lab_payments import pay_lab_order
from core.services.slot_config import ensure_slot_type_configs, get_slot_type_config
from core.services.workflow import (
    after_lab_report_uploaded,
    complete_consultation,
    pharmacy_mark_ready,
    resolve_or_create_patient_user,
)
from core.utils import consultation_fee_with_charge, ensure_today_tomorrow_slots, _consolidate_slot_for_day


DEMO_PHONE_PREFIX = '9800100'
DEMO_HISTORY_PHONE_PREFIX = '9800110'
DEFAULT_MEDICINES = [
    {'name': 'Paracetamol', 'dosage': '500mg', 'frequency': 'TID', 'duration_days': 5},
    {'name': 'Amoxicillin', 'dosage': '500mg', 'frequency': 'TID', 'duration_days': 7},
]
LAB_CBC = 'Complete Blood Count (CBC)'

# Matches seed_opd_data: doctor1 / doctor2 / doctor3 per slot type
SLOT_DOCTOR_USERNAME = {
    'morning': 'doctor1',
    'afternoon': 'doctor2',
    'evening': 'doctor3',
}
SLOT_TYPES = ('morning', 'afternoon', 'evening')


def make_slot_datetime(slot, minutes_after_start=30):
    start_h, start_m = map(int, slot.start_time.split(':'))
    base = datetime.combine(
        slot.date,
        datetime.strptime(f'{start_h}:{start_m:02d}', '%H:%M').time(),
    )
    fake = base + timedelta(minutes=minutes_after_start)
    return timezone.make_aware(fake)


@contextmanager
def at_slot_time(slot, minutes_after_start=30):
    fake = make_slot_datetime(slot, minutes_after_start)
    with patch('django.utils.timezone.localtime', return_value=fake), patch(
        'django.utils.timezone.now', return_value=fake,
    ):
        yield fake


class DemoWorkflowSeeder:
    """Build mixed queue states + completed visits for portal/analytics demos."""

    def __init__(self, stdout=None, style=None):
        self.stdout = stdout
        self.style = style
        self.today = timezone.localdate()
        self.log_lines = []
        self._staff = {}

    def write(self, msg, success=False, warning=False):
        line = str(msg)
        self.log_lines.append(line)
        if self.stdout:
            if success and self.style:
                self.stdout.write(self.style.SUCCESS(line))
            elif warning and self.style:
                self.stdout.write(self.style.WARNING(line))
            else:
                self.stdout.write(line)

    def _ensure_staff(self):
        self._staff['reception'] = User.objects.filter(role='receptionist').first()
        self._staff['lab_tech'] = User.objects.filter(role='lab_tech').first()
        self._staff['pharmacist'] = User.objects.filter(role='pharmacist').first()
        self._staff['admin'] = User.objects.filter(role='admin').first()
        if not self._staff['reception']:
            raise RuntimeError('No receptionist user. Run: python manage.py seed_opd_data')

    def _ensure_followup_rule(self):
        FollowupRule.objects.filter(is_active=True).update(is_active=False)
        FollowupRule.objects.create(exempt_within_days=7, is_active=True)

    def _ensure_system_doctor_assignments(self):
        """One doctor per slot type, matching seed_opd_data and admin slot config."""
        ensure_slot_type_configs()
        for slot_type, username in SLOT_DOCTOR_USERNAME.items():
            doctor = DoctorProfile.objects.filter(
                user__username=username,
                is_available=True,
            ).select_related('user').first()
            if not doctor:
                continue
            cfg = SlotTypeConfig.objects.get(slot_type=slot_type)
            cfg.assigned_doctor = doctor
            cfg.save(update_fields=['assigned_doctor'])
        ensure_today_tomorrow_slots()

    def _get_slot(self, slot_type):
        ensure_slot_type_configs()
        ensure_today_tomorrow_slots()
        slot = ConsultationSlot.objects.filter(
            date=self.today,
            slot_type=slot_type,
        ).select_related('doctor__user').first()
        if not slot:
            raise RuntimeError(
                f'No {slot_type} slot for today. Run: python manage.py seed_opd_data',
            )
        return slot

    def _get_slots_for_run(self, slot_type):
        self._ensure_system_doctor_assignments()
        if slot_type == 'all':
            return {st: self._get_slot(st) for st in SLOT_TYPES}
        slot = self._get_slot(slot_type)
        return {slot_type: slot}

    def _get_tomorrow_slot(self, doctor, slot_type):
        tomorrow = self.today + timedelta(days=1)
        slot = ConsultationSlot.objects.filter(
            doctor=doctor,
            date=tomorrow,
            slot_type=slot_type,
        ).first()
        if slot:
            return slot
        return ConsultationSlot.objects.create(
            doctor=doctor,
            date=tomorrow,
            slot_type=slot_type,
        )

    def clear_demo_patients(self):
        phones = []
        for prefix in (DEMO_PHONE_PREFIX, DEMO_HISTORY_PHONE_PREFIX):
            phones.extend(
                User.objects.filter(phone__startswith=prefix, role='patient').values_list(
                    'phone', flat=True,
                )
            )
        phones = list(dict.fromkeys(phones))
        if not phones:
            self.write('No prior demo patients to clear.')
            return
        Token.objects.filter(patient_phone__in=phones).delete()
        User.objects.filter(phone__in=phones).delete()
        from accounts.models import PatientSerial

        PatientSerial.sync_from_database()
        self.write(f'Removed demo patients ({len(phones)} phones) and their tokens.', warning=True)

    def _demo_phone(self, n):
        return f'{DEMO_PHONE_PREFIX}{n:03d}'

    def _ensure_patient(self, n, full_name, age=32):
        phone = self._demo_phone(n)
        user = resolve_or_create_patient_user(phone, full_name, age, 'Demo Address Kathmandu')
        user.set_password('demo1234')
        user.save(update_fields=['password'])
        return user, phone

    def _book_token(self, slot, patient, full_name, age):
        with at_slot_time(slot):
            token = Token.objects.create(
                slot=slot,
                patient=patient,
                patient_name=full_name,
                patient_age=age,
                patient_phone=patient.phone,
                patient_address=patient.address or 'Demo Address Kathmandu',
            )
            _, _, total = consultation_fee_with_charge()
            Payment.objects.create(
                token=token,
                payment_type='consultation_fee',
                amount=total,
                status='paid',
                reference_number=f'demo-esewa-{token.id}',
                paid_at=timezone.now(),
            )
        token.refresh_from_db()
        return token

    def _slot_duration_minutes(self, slot):
        return get_slot_type_config(slot.slot_type).duration_minutes

    def _clamp_slot_minute(self, slot, minute):
        return min(max(1, minute), self._slot_duration_minutes(slot) - 1)

    def _check_in(self, token, receptionist, minutes_after_start=25):
        with at_slot_time(token.slot, self._clamp_slot_minute(token.slot, minutes_after_start)):
            token.refresh_from_db()
            if token.status == C.EXPIRED:
                token.status = C.BOOKED
                token.save(update_fields=['status'])
            token.check_in(receptionist=receptionist)

    def _start_consult(self, token, minutes_after_start=30):
        with at_slot_time(token.slot, self._clamp_slot_minute(token.slot, minutes_after_start)):
            token.refresh_from_db()
            token.start_consultation()

    def _complete_consult(
        self,
        token,
        minutes_after_start=40,
        medicines=None,
        lab_tests=None,
        followup_date=None,
    ):
        with at_slot_time(token.slot, self._clamp_slot_minute(token.slot, minutes_after_start)):
            return complete_consultation(
                token,
                symptoms='Fever, cough, fatigue',
                diagnosis='Upper respiratory infection',
                notes='Demo seeded consultation',
                medicines=medicines if medicines is not None else list(DEFAULT_MEDICINES),
                lab_tests=lab_tests if lab_tests is not None else [],
                followup_date=followup_date,
            )

    def _run_consult_pipeline(
        self,
        token,
        receptionist,
        checkin_min,
        start_min,
        consult_minutes=10,
        medicines=None,
        lab_tests=None,
        followup_date=None,
        stop_after='complete',
    ):
        self._check_in(token, receptionist, checkin_min)
        if stop_after == 'checkin':
            return
        self._start_consult(token, start_min)
        if stop_after == 'start':
            return
        self._complete_consult(
            token,
            start_min + consult_minutes,
            medicines=medicines,
            lab_tests=lab_tests,
            followup_date=followup_date,
        )

    def _pay_lab(self, token, collected_by, ref_suffix='reception', minutes_after_start=50):
        order = token.lab_orders.filter(status__in=('fee_pending', 'ordered')).first()
        if not order:
            raise RuntimeError(f'No pending lab order on token {token.token_number}')
        with at_slot_time(token.slot, self._clamp_slot_minute(token.slot, minutes_after_start)):
            pay_lab_order(order.id, order.fee, collected_by, f'demo-{ref_suffix}-{order.id}')
        order.refresh_from_db()
        return order

    def _complete_lab(self, order, lab_tech, minutes_after_start=55):
        entry = order.queue_entry
        with at_slot_time(order.token.slot, self._clamp_slot_minute(order.token.slot, minutes_after_start)):
            if entry.status == 'waiting':
                entry.start(lab_tech)
            entry.complete()
            LabReport.objects.update_or_create(
                lab_order=order,
                defaults={
                    'uploaded_by': lab_tech,
                    'findings': 'Demo report: within normal limits.',
                },
            )
            after_lab_report_uploaded(order)

    def _complete_pharmacy(self, token, pharmacist, minutes_after_start=45):
        entry = PharmacyQueueEntry.objects.get(token=token)
        with at_slot_time(token.slot, self._clamp_slot_minute(token.slot, minutes_after_start)):
            if entry.status == C.PHARMACY_WAITING:
                entry.start_dispensing(pharmacist)
            if entry.status == C.PHARMACY_DISPENSING:
                pharmacy_mark_ready(entry)
            entry.refresh_from_db()
            if entry.status == C.PHARMACY_READY:
                entry.payment_collected = True
                entry.save(update_fields=['payment_collected'])
                Payment.objects.create(
                    token=token,
                    payment_type='pharmacy_fee',
                    amount=entry.total_bill,
                    status='paid',
                    collected_by=pharmacist,
                    paid_at=timezone.now(),
                    reference_number=f'demo-pharm-{entry.id}',
                )
                entry.complete()

    def _mark_noshow(self, token):
        token.status = C.EXPIRED
        token.save(update_fields=['status'])

    def _mark_late_checkin(self, token):
        token.checkin_status = 'missed'
        token.save(update_fields=['checkin_status'])

    def _no_show_count_for_slot(self, day, slot_type, max_count=2):
        """One or two no-shows per slot-day (deterministic for stable re-seeds)."""
        rng = random.Random(day.toordinal() * 31 + hash(slot_type))
        return rng.randint(1, min(2, max_count)) if max_count >= 1 else 0

    def _book_slot_only(self, slot, patient_seq, booking_count, label_prefix, scenarios, use_history=False):
        phase = self._slot_timeline_phase(slot)
        for i in range(booking_count):
            age = 25 + (patient_seq % 35)
            if use_history:
                patient, phone = self._ensure_history_patient(
                    patient_seq, f'{label_prefix} #{i + 1}', age,
                )
            else:
                patient, phone = self._ensure_patient(
                    patient_seq, f'Demo {label_prefix} #{i + 1}', age,
                )
            patient_seq += 1
            token = self._book_token(slot, patient, f'Demo {label_prefix} {i + 1}', age)
            scenarios.append((
                f'{label_prefix} — booked',
                token,
                phone,
                f'not checked in yet [{phase}]',
            ))
        return patient_seq

    def _simulate_slot_day(
        self,
        slot,
        reception,
        lab_tech,
        pharmacist,
        booking_count,
        patient_seq,
        scenarios,
        label_prefix,
        use_history=False,
        freeze_at_minute=None,
        followup_date=None,
        book_followup_for_last=False,
        full_book=False,
        no_show_count=0,
        ignore_time_limit=False,
    ):
        """
        Book patients then advance the slot minute-by-minute.
        Only one patient is consulting at a time (sequential doctor time).
        """
        phase = self._slot_timeline_phase(slot)
        consult_minutes = 8 if ignore_time_limit else 10
        duration = self._slot_duration_minutes(slot)
        max_consults = max(1, duration // consult_minutes)
        if full_book:
            booking_count = slot.max_tokens
        else:
            booking_count = min(booking_count, max_consults)

        no_show_count = min(no_show_count, max(0, booking_count - 1))
        rng = random.Random(slot.date.toordinal() * 17 + hash(slot.slot_type))
        no_show_indices = set()
        if no_show_count:
            picks = list(range(booking_count))
            rng.shuffle(picks)
            no_show_indices = set(picks[:no_show_count])

        scenario_cycle = ('full', 'rx_only', 'full', 'lab_only', 'rx_only', 'full', 'rx_only', 'lab_only')
        booked = []

        for i in range(booking_count):
            age = 25 + (patient_seq % 35)
            if use_history:
                patient, phone = self._ensure_history_patient(
                    patient_seq, f'{label_prefix} d{i + 1}', age,
                )
            else:
                patient, phone = self._ensure_patient(
                    patient_seq, f'Demo {label_prefix} {i + 1}', age,
                )
            patient_seq += 1
            token = self._book_token(slot, patient, f'Demo {label_prefix} {i + 1}', age)
            scenario = 'noshow' if i in no_show_indices else scenario_cycle[i % len(scenario_cycle)]
            booked.append((token, patient, phone, scenario))

        minute = 8
        for idx, (token, patient, phone, scenario) in enumerate(booked):
            if scenario == 'noshow':
                if ignore_time_limit or freeze_at_minute is None:
                    self._mark_noshow(token)
                continue

            if not ignore_time_limit and freeze_at_minute is not None and minute > freeze_at_minute:
                break
            if not ignore_time_limit and minute >= duration - consult_minutes:
                break

            self._check_in(token, reception, minute)
            if idx % 5 == 3 and not ignore_time_limit:
                self._mark_late_checkin(token)
            minute += 2

            if not ignore_time_limit and freeze_at_minute is not None and minute > freeze_at_minute:
                scenarios.append((
                    f'{label_prefix} — in queue',
                    token,
                    phone,
                    f'checked in, waiting [{phase}]',
                ))
                break

            self._start_consult(token, minute)
            if not ignore_time_limit and freeze_at_minute is not None and minute + consult_minutes > freeze_at_minute:
                scenarios.append((
                    f'{label_prefix} — with doctor',
                    token,
                    phone,
                    f'only active consult [{phase}]',
                ))
                break

            minute += consult_minutes
            medicines = [] if scenario == 'lab_only' else (
                [DEFAULT_MEDICINES[0]] if scenario == 'rx_only' else list(DEFAULT_MEDICINES)
            )
            lab_tests = [LAB_CBC] if scenario in ('full', 'lab_only') else []
            fd = followup_date if (book_followup_for_last and idx == len(booked) - 1) else None
            self._complete_consult(
                token,
                minute,
                medicines=medicines,
                lab_tests=lab_tests,
                followup_date=fd,
            )
            minute += 1

            if lab_tests:
                if not ignore_time_limit and freeze_at_minute is not None and minute > freeze_at_minute:
                    token.refresh_from_db()
                    scenarios.append((
                        f'{label_prefix} — lab unpaid',
                        token,
                        phone,
                        f'pending lab payment [{phase}]',
                    ))
                    minute += consult_minutes
                    continue
                order = self._pay_lab(token, reception, 'demo', minute)
                minute += 2
                if scenario in ('full', 'lab_only'):
                    if not ignore_time_limit and freeze_at_minute is not None and minute > freeze_at_minute:
                        token.refresh_from_db()
                        scenarios.append((
                            f'{label_prefix} — lab queue',
                            token,
                            phone,
                            f'lab paid, in queue [{phase}]',
                        ))
                        minute += consult_minutes
                        continue
                    self._complete_lab(order, lab_tech, minute)
                    minute += 3

            if medicines:
                if not ignore_time_limit and freeze_at_minute is not None and minute > freeze_at_minute:
                    token.refresh_from_db()
                    scenarios.append((
                        f'{label_prefix} — pharmacy',
                        token,
                        phone,
                        f'pharmacy queue [{phase}]',
                    ))
                    minute += consult_minutes
                    continue
                self._complete_pharmacy(token, pharmacist, minute)
                minute += 2

            if book_followup_for_last and idx == len(booked) - 1:
                tomorrow = self._get_tomorrow_slot(slot.doctor, slot.slot_type)
                with at_slot_time(tomorrow, 15):
                    book_followup_via_slot(token, tomorrow, patient)

            token.refresh_from_db()
            scenarios.append((
                f'{label_prefix} — {token.status}',
                token,
                phone,
                f'{token.get_status_display()} [{phase}]',
            ))
            minute += 1

        return patient_seq

    def _simulate_history_slot_day(
        self,
        slot,
        reception,
        pharmacist,
        patient_seq,
        label_prefix,
        no_show_count,
    ):
        """Fast path for past days: full slot, 1–2 no-shows, rx-only completed visits."""
        booking_count = slot.max_tokens
        no_show_count = min(no_show_count, max(0, booking_count - 1))
        rng = random.Random(slot.date.toordinal() * 17 + hash(slot.slot_type))
        no_show_indices = set()
        if no_show_count:
            picks = list(range(booking_count))
            rng.shuffle(picks)
            no_show_indices = set(picks[:no_show_count])

        to_complete = []
        with at_slot_time(slot, 5):
            for i in range(booking_count):
                age = 25 + (patient_seq % 35)
                patient, phone = self._ensure_history_patient(
                    patient_seq, f'{label_prefix} d{i + 1}', age,
                )
                patient_seq += 1
                token = Token.objects.create(
                    slot=slot,
                    patient=patient,
                    patient_name=f'Demo {label_prefix} {i + 1}',
                    patient_age=age,
                    patient_phone=patient.phone,
                    patient_address=patient.address or 'Demo Address Kathmandu',
                )
                _, _, total = consultation_fee_with_charge()
                Payment.objects.create(
                    token=token,
                    payment_type='consultation_fee',
                    amount=total,
                    status='paid',
                    reference_number=f'demo-hist-{token.id}',
                    paid_at=timezone.now(),
                )
                if i in no_show_indices:
                    token.status = C.EXPIRED
                    token.save(update_fields=['status'])
                else:
                    to_complete.append(token)

        for idx, token in enumerate(to_complete):
            self._fast_complete_history_visit(token, reception, pharmacist, idx)

        return patient_seq

    def _fast_complete_history_visit(self, token, reception, pharmacist, idx):
        """One time-block per visit — much faster than multi-step lab/pharmacy simulation."""
        minute = self._clamp_slot_minute(token.slot, 12 + (idx * 2) % 100)
        with at_slot_time(token.slot, minute):
            token.refresh_from_db()
            if token.status != C.BOOKED:
                return
            token.check_in(receptionist=reception)
            token.start_consultation()
            complete_consultation(
                token,
                symptoms='Demo history visit',
                diagnosis='Routine OPD visit',
                notes='Seeded monthly history',
                medicines=[DEFAULT_MEDICINES[0]],
                lab_tests=[],
            )
            token.refresh_from_db()
            if token.status != C.PENDING_PHARMACY:
                return
            entry = PharmacyQueueEntry.objects.get(token=token)
            if entry.status == C.PHARMACY_WAITING:
                entry.start_dispensing(pharmacist)
            if entry.status == C.PHARMACY_DISPENSING:
                pharmacy_mark_ready(entry)
            entry.refresh_from_db()
            if entry.status == C.PHARMACY_READY:
                entry.payment_collected = True
                entry.save(update_fields=['payment_collected'])
                Payment.objects.create(
                    token=token,
                    payment_type='pharmacy_fee',
                    amount=entry.total_bill,
                    status='paid',
                    collected_by=pharmacist,
                    paid_at=timezone.now(),
                    reference_number=f'demo-hist-pharm-{entry.id}',
                )
                entry.complete()

    def _seed_today_slots(
        self,
        slot_morning,
        slot_afternoon,
        slot_evening,
        reception,
        lab_tech,
        pharmacist,
        scenarios,
        followup_date,
        with_followup_book,
    ):
        patient_seq = 1
        slot_plan = [
            ('Morning', slot_morning),
            ('Afternoon', slot_afternoon),
            ('Evening', slot_evening),
        ]
        for label_prefix, slot in slot_plan:
            phase = self._slot_timeline_phase(slot)
            booking_count = slot.max_tokens

            if phase == 'future':
                patient_seq = self._book_slot_only(
                    slot, patient_seq, booking_count, label_prefix, scenarios,
                )
                continue

            freeze = self._elapsed_minutes_in_slot(slot) if phase == 'active' else None
            is_evening_followup = with_followup_book and slot.slot_type == 'evening'
            no_shows = (
                self._no_show_count_for_slot(self.today, slot.slot_type, max_count=2)
                if phase == 'past'
                else 0
            )
            patient_seq = self._simulate_slot_day(
                slot,
                reception,
                lab_tech,
                pharmacist,
                booking_count,
                patient_seq,
                scenarios,
                label_prefix,
                freeze_at_minute=freeze,
                followup_date=followup_date if is_evening_followup else None,
                book_followup_for_last=is_evening_followup,
                full_book=True,
                no_show_count=no_shows,
                ignore_time_limit=(phase == 'past'),
            )
        return patient_seq

    def _slot_timeline_phase(self, slot):
        """Whether today's slot is future, active, or past relative to the real clock."""
        now = timezone.localtime()
        if slot.date != self.today:
            return 'past' if slot.date < self.today else 'future'
        cfg = get_slot_type_config(slot.slot_type)
        current = now.time()
        if current < cfg.start_time:
            return 'future'
        if current >= cfg.end_time:
            return 'past'
        return 'active'

    def _elapsed_minutes_in_slot(self, slot):
        now = timezone.localtime()
        cfg = get_slot_type_config(slot.slot_type)
        start = cfg.start_time
        return max(0, (now.hour * 60 + now.minute) - (start.hour * 60 + start.minute))

    def _step_allowed(self, slot, minute):
        phase = self._slot_timeline_phase(slot)
        if phase == 'future':
            return False
        if phase == 'past':
            return True
        return minute <= self._elapsed_minutes_in_slot(slot)

    def _run_journey_steps(
        self,
        token,
        steps,
        slot,
        reception,
        lab_tech,
        pharmacist,
        patient_user=None,
    ):
        """Apply workflow steps only when the real clock has reached that point in the slot."""
        order = None
        for step in steps:
            minute = step['minute']
            if not self._step_allowed(slot, minute):
                break
            action = step['action']
            if action == 'checkin':
                self._check_in(token, reception, minute)
            elif action == 'start':
                self._start_consult(token, minute)
            elif action == 'complete':
                self._complete_consult(
                    token,
                    minute,
                    medicines=step.get('medicines', list(DEFAULT_MEDICINES)),
                    lab_tests=step.get('lab_tests', []),
                    followup_date=step.get('followup_date'),
                )
            elif action == 'pay_lab':
                payer = step.get('collected_by', reception)
                order = self._pay_lab(
                    token,
                    payer,
                    step.get('ref_suffix', 'reception'),
                    minute,
                )
            elif action == 'lab_start':
                if order is None:
                    order = token.lab_orders.filter(
                        status__in=('fee_paid', 'in_queue', 'ordered'),
                    ).first()
                if order:
                    with at_slot_time(slot, minute):
                        order.queue_entry.start(lab_tech)
            elif action == 'complete_lab':
                if order is None:
                    order = token.lab_orders.first()
                if order:
                    self._complete_lab(order, lab_tech, minute)
            elif action == 'pharmacy_ready':
                self._pharmacy_ready_only(token, pharmacist, minute)
            elif action == 'complete_pharmacy':
                self._complete_pharmacy(token, pharmacist, minute)
            elif action == 'book_followup':
                tomorrow_slot = self._get_tomorrow_slot(slot.doctor, slot.slot_type)
                with at_slot_time(tomorrow_slot, minute):
                    book_followup_via_slot(token, tomorrow_slot, patient_user)
        token.refresh_from_db()
        return token

    def _seed_demo_journey(
        self,
        slot,
        patient_n,
        name,
        age,
        steps,
        reception,
        lab_tech,
        pharmacist,
        label,
        where,
        scenarios,
        patient_user=None,
    ):
        patient = patient_user or self._ensure_patient(patient_n, name, age)[0]
        phone = patient.phone
        token = self._book_token(slot, patient, name, age)
        if steps:
            token = self._run_journey_steps(
                token,
                steps,
                slot,
                reception,
                lab_tech,
                pharmacist,
                patient_user=patient,
            )
        phase = self._slot_timeline_phase(slot)
        scenarios.append((label, token, phone, f'{where} [{phase}]'))
        return token, phone

    def _pharmacy_ready_only(self, token, pharmacist, minutes_after_start=48):
        entry = PharmacyQueueEntry.objects.get(token=token)
        with at_slot_time(token.slot, minutes_after_start):
            entry.start_dispensing(pharmacist)
            pharmacy_mark_ready(entry)

    def _history_phone(self, n):
        return f'{DEMO_HISTORY_PHONE_PREFIX}{n:05d}'

    def _ensure_history_patient(self, n, full_name, age):
        phone = self._history_phone(n)
        user = resolve_or_create_patient_user(phone, full_name, age, 'Demo Address Kathmandu')
        user.set_password('demo1234')
        user.save(update_fields=['password'])
        return user, phone

    def _seed_monthly_history(self, slots_by_type, reception, lab_tech, pharmacist, days=29):
        """Past days: full slots, 1–2 no-shows per slot, rest completed for analytics."""
        patient_seq = 1
        total_visits = 0

        self.write(
            f'Seeding {days} days of history (full slots, ~{days * len(SLOT_TYPES)} slot-days) — '
            'progress prints per day; usually 2–5 minutes.',
            warning=True,
        )

        for days_ago in range(1, days + 1):
            day = self.today - timedelta(days=days_ago)
            self.write(f'  History day {days_ago}/{days}: {day.isoformat()}...')
            for slot_type in SLOT_TYPES:
                ref = slots_by_type[slot_type]
                past_slot = _consolidate_slot_for_day(day, slot_type, ref.doctor)
                booking_count = past_slot.max_tokens
                label = f'History {day.strftime("%d %b")} {slot_type.title()}'
                no_shows = self._no_show_count_for_slot(day, slot_type, max_count=2)
                patient_seq = self._simulate_history_slot_day(
                    past_slot,
                    reception,
                    pharmacist,
                    patient_seq,
                    label,
                    no_shows,
                )
                total_visits += booking_count

            compute_daily_analytics(day)

        self.write(
            f'Seeded monthly history: {days} days, {total_visits} full-slot bookings '
            f'(1–2 no-shows per slot, rest completed).',
            success=True,
        )

    def _seed_history_days(self, slots_by_type, reception, lab_tech, pharmacist, days=6):
        """Legacy short history — use _seed_monthly_history for analytics demos."""
        self._seed_monthly_history(slots_by_type, reception, lab_tech, pharmacist, days=days)

    def run(
        self,
        slot_type='all',
        with_followup_book=False,
        clear=False,
        with_history=True,
        history_days=29,
    ):
        self._ensure_staff()
        self._ensure_followup_rule()
        if clear:
            self.clear_demo_patients()

        slots = self._get_slots_for_run(slot_type)
        if slot_type == 'all':
            slot_morning = slots['morning']
            slot_afternoon = slots['afternoon']
            slot_evening = slots['evening']
        else:
            slot_morning = slot_afternoon = slot_evening = slots[slot_type]

        reception = self._staff['reception']
        lab_tech = self._staff['lab_tech'] or self._staff['admin']
        pharmacist = self._staff['pharmacist'] or self._staff['admin']
        if not lab_tech or not pharmacist:
            raise RuntimeError('Need lab_tech and pharmacist (or admin). Run: python manage.py seed_opd_data')

        scenarios = []
        followup_date = self.today + timedelta(days=3)
        now_label = timezone.localtime().strftime('%H:%M')

        self.write(f'Syncing demo journeys to real time ({now_label})...')
        self.write('Today’s slots first, then monthly history (watch for per-day progress).')

        self._seed_today_slots(
            slot_morning,
            slot_afternoon,
            slot_evening,
            reception,
            lab_tech,
            pharmacist,
            scenarios,
            followup_date,
            with_followup_book,
        )

        if with_history:
            self._seed_monthly_history(slots, reception, lab_tech, pharmacist, days=history_days)

        compute_daily_analytics(self.today)
        call_command('compute_analytics', date=self.today.isoformat())
        kpis = compute_kpis(self.today)

        self.write('', success=False)
        self.write('=' * 60, success=True)
        self.write('DEMO WORKFLOW SEEDED (real database)', success=True)
        self.write('=' * 60, success=True)
        if slot_type == 'all':
            self.write(
                f'Morning: {slot_morning.doctor} ({slot_morning.doctor.user.username}) | '
                f'Afternoon: {slot_afternoon.doctor} ({slot_afternoon.doctor.user.username}) | '
                f'Evening: {slot_evening.doctor} ({slot_evening.doctor.user.username})',
            )
        else:
            self.write(
                f'Slot: {slot_type} | Doctor: {slots[slot_type].doctor} '
                f'({slots[slot_type].doctor.user.username})',
            )
        self.write(f'Completed today: {kpis["completed"]} | Throughput: {kpis["system_throughput"]}')
        self.write('')
        self.write('Demo patients (password: demo1234 for portal login):')
        self.write('-' * 60)
        for label, token, phone, where in scenarios:
            token.refresh_from_db()
            self.write(
                f'  {label}: {token.token_number} | {phone} | '
                f'{token.slot.get_slot_type_display()} / {token.slot.doctor} | '
                f'status={token.status} -> {where}',
            )
        self.write('-' * 60)
        self.write('Portals:')
        self.write('  Admin Analytics: /admin/dashboard.html (admin / admin)')
        self.write('  Patient portal: /patient/login.html')
        self.write('  Reception / Doctor / Lab / Pharmacy: /staff/login.html')
        self.write('')
        self.write('Refresh analytics: python manage.py compute_analytics', warning=True)

        return {
            'scenarios': scenarios,
            'kpis': kpis,
            'slots': slots,
        }
