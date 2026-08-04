from decimal import Decimal
from datetime import timedelta

from django.utils import timezone

from core import constants as C
from core.services.workflow import display_status

from .models import DoctorProfile, ConsultationSlot, Token

CONSULTATION_BASE_FEE = Decimal('100.00')
SERVICE_CHARGE_RATE = Decimal('0')
SLOT_TYPES = ['morning', 'afternoon', 'evening']
ELDERLY_AGE_THRESHOLD = 70
DOCTOR_SPECIALIZATION_LABEL = 'General Physician'


def doctor_display_name(doctor):
    return f"Dr. {doctor.user.get_full_name()} - {DOCTOR_SPECIALIZATION_LABEL}"


def doctor_name_short(doctor):
    return f"Dr. {doctor.user.get_full_name()}"


def doctor_specialty(doctor):
    return doctor.specialization or DOCTOR_SPECIALIZATION_LABEL


def is_elderly_by_age(age):
    try:
        return int(age) >= ELDERLY_AGE_THRESHOLD
    except (TypeError, ValueError):
        return False


def patient_priority_category(token):
    """Human-readable priority label for consultation / queue display."""
    if getattr(token, 'is_elderly', False):
        return 'ELDERLY'
    if getattr(token, 'is_disabled', False):
        return 'DISABLED'
    return 'GENERAL'


def patient_gender_for_token(token):
    """Gender from linked patient account, if recorded."""
    patient = getattr(token, 'patient', None)
    if patient is not None:
        raw = getattr(patient, 'gender', None) or ''
        if raw:
            return raw
    return ''


def format_patient_gender(gender):
    if not gender:
        return ''
    labels = {'male': 'Male', 'female': 'Female', 'other': 'Other'}
    return labels.get(str(gender).lower(), str(gender))


def patient_id_for_token(token):
    """Canonical PAT code for a token's linked patient account, if any."""
    patient = getattr(token, 'patient', None)
    if patient is not None:
        return getattr(patient, 'patient_code', None) or getattr(patient, 'patient_id', None)
    return None


def resolve_disabled_flag(explicit_value, *, patient_user=None, token=None):
    """Check-in disabled flag: explicit toggle > patient profile > token default."""
    if explicit_value is not None:
        return bool(explicit_value)
    if patient_user is not None and getattr(patient_user, 'is_disabled', False):
        return True
    if token is not None:
        return bool(getattr(token, 'is_disabled', False))
    return False


def normalize_phone(phone):
    return ''.join(c for c in str(phone or '') if c.isdigit())


def patient_has_active_slot_booking(slot, *, patient_user=None, patient_phone=None):
    """True if this patient already holds a non-cancelled booking for this slot."""
    if not slot:
        return False

    qs = Token.objects.filter(slot=slot, status__in=C.DUPLICATE_BOOKING_BLOCK_STATUSES)
    if patient_user and qs.filter(patient=patient_user).exists():
        return True

    phone_norm = normalize_phone(patient_phone)
    if patient_phone and qs.filter(patient_phone=patient_phone).exists():
        return True
    if phone_norm:
        for existing_phone in qs.values_list('patient_phone', flat=True):
            if normalize_phone(existing_phone) == phone_norm:
                return True
    return False


def duplicate_slot_booking_error(slot):
    slot_label = slot_type_display(slot.slot_type).title() if slot else 'this'
    date_label = slot.date.strftime('%d %b %Y') if slot and slot.date else 'this day'
    return (
        f'You already have an appointment for the {slot_label} slot on {date_label}. '
        'Only one booking per slot per day is allowed.'
    )


def patient_has_online_account(user):
    """True when the patient has activated and used the patient portal."""
    return patient_has_portal_login(user)


def patient_has_portal_login(user):
    """True when the patient has logged into the patient portal at least once."""
    return bool(
        user
        and getattr(user, 'role', None) == 'patient'
        and getattr(user, 'last_login', None)
    )


def patient_is_new(user):
    """
    New patient = registered (or walk-in) but has never logged into the
    patient portal. Unlinked walk-ins with no User row are also new.
    """
    if user is None:
        return True
    if getattr(user, 'role', None) != 'patient':
        return False
    return not patient_has_portal_login(user)


OLD_PATIENT_LOGIN_MSG = (
    'This patient already has an online account. Please use Old Patient login.'
)
OLD_PATIENT_BOOKING_MSG = (
    'This phone number belongs to a registered patient. '
    'Please select Old Patient and verify your Patient ID.'
)


def slot_type_display(slot_type):
    return slot_type.upper() if slot_type else ''


def slot_time_range(slot_type):
    from core.services.slot_config import get_slot_type_config
    return get_slot_type_config(slot_type).time_range if slot_type else ''


def format_local_time(dt, fmt='%I:%M %p'):
    """Format a timezone-aware datetime in the project's local timezone."""
    if not dt:
        return None
    return timezone.localtime(dt).strftime(fmt)


def serialize_slot(slot):
    from core.services.slot_config import is_slot_bookable, is_slot_ended

    tokens_left = slot.max_tokens - slot.tokens_booked
    doctor = slot.doctor
    base, service, total = consultation_fee_with_charge()
    fee = float(base)
    ended = is_slot_ended(slot)
    return {
        'slot_id': slot.id,
        'doctor_id': doctor.id,
        'doctor_name': f"Dr. {doctor.user.get_full_name()}",
        'specialization': DOCTOR_SPECIALIZATION_LABEL,
        'qualification': doctor.qualification or '',
        'date': slot.date.isoformat(),
        'slot_type': slot.slot_type,
        'slot_type_display': slot_type_display(slot.slot_type),
        'start_time': slot.start_time,
        'end_time': slot.end_time,
        'time_range': slot_time_range(slot.slot_type),
        'avg_consultation_minutes': slot.avg_consultation_minutes,
        'tokens_available': max(tokens_left, 0),
        'tokens_booked': slot.tokens_booked,
        'max_tokens': slot.max_tokens,
        'fee': fee,
        'service_charge': float(service),
        'total_fee': float(total),
        'is_full': slot.is_full,
        'is_ended': ended,
        'is_bookable': is_slot_bookable(slot),
        'is_throttled': False,
    }


def serialize_token(token, include_queue=False, include_workflow=False):
    data = {
        'token_id': token.id,
        'appointment_id': token.id,
        'token_number': token.token_number,
        'patient_name': token.patient_name,
        'patient_age': token.patient_age,
        'patient_gender': format_patient_gender(patient_gender_for_token(token)),
        'patient_phone': token.patient_phone,
        'patient_address': token.patient_address or '',
        'status': token.status,
        'display_status': display_status(token),
        'is_active': token.status in C.ACTIVE_STATUSES,
        'checkin_status': token.checkin_status,
        'estimated_time': format_local_time(token.estimated_time),
        'doctor_name': doctor_display_name(token.slot.doctor),
        'doctor_id': token.slot.doctor_id,
        'date': token.slot.date.isoformat(),
        'slot_type': slot_type_display(token.slot.slot_type),
        'slot_type_raw': token.slot.slot_type,
        'start_time': token.slot.start_time,
        'end_time': token.slot.end_time,
        'avg_consultation_minutes': token.slot.avg_consultation_minutes,
        'is_elderly': token.is_elderly,
        'is_disabled': token.is_disabled,
        'is_followup': token.is_followup,
        'fee_exempted': token.fee_exempted,
        'checked_in_at': format_local_time(token.checked_in_at),
    }
    if token.patient_id:
        data['patient_user_id'] = token.patient_id
    patient_user = getattr(token, 'patient', None)
    if patient_user and getattr(patient_user, 'patient_id', None):
        data['patient_id'] = patient_user.patient_id
    if patient_user is not None:
        data['patient_is_disabled'] = bool(getattr(patient_user, 'is_disabled', False))
    data['is_new_patient'] = patient_is_new(patient_user)
    if include_queue:
        try:
            entry = token.queue_entry
        except Exception:
            entry = None
        if entry:
            data['queue_position'] = entry.queue_position
            data['priority'] = entry.priority

    if include_workflow:
        try:
            pharmacy = token.pharmacy_queue_entry
        except Exception:
            pharmacy = None
        if pharmacy:
            data['pharmacy_status'] = pharmacy.status
            data['pharmacy_display'] = C.PHARMACY_DISPLAY.get(pharmacy.status, pharmacy.status)
        pending_labs = token.lab_orders.exclude(status='completed').count()
        data['pending_lab_count'] = pending_labs
        data['has_prescription'] = token.prescriptions.exists()

    return data


def _consolidate_slot_for_day(day, slot_type, assigned_doctor):
    """Ensure exactly one ConsultationSlot per (date, slot_type)."""
    existing = list(ConsultationSlot.objects.filter(date=day, slot_type=slot_type))
    if existing:
        canonical = max(existing, key=lambda s: s.tokens.count())
        for dup in existing:
            if dup.pk != canonical.pk:
                Token.objects.filter(slot=dup).update(slot=canonical)
                dup.delete()
        if canonical.doctor_id != assigned_doctor.id and canonical.tokens.count() == 0:
            canonical.doctor = assigned_doctor
            canonical.save(update_fields=['doctor'])
        return canonical
    return ConsultationSlot.objects.create(
        doctor=assigned_doctor,
        date=day,
        slot_type=slot_type,
    )


def ensure_today_tomorrow_slots():
    """Create 3 slots per day — one doctor per slot (morning/afternoon/evening)."""
    from core.services.slot_config import (
        ensure_slot_type_configs, get_slot_type_config, refresh_consultation_slot_capacities,
    )
    ensure_slot_type_configs()
    doctors = list(DoctorProfile.objects.filter(is_available=True).order_by('id'))
    if not doctors:
        return

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    for day in (today, tomorrow):
        for idx, slot_type in enumerate(SLOT_TYPES):
            cfg = get_slot_type_config(slot_type)
            configured = getattr(cfg, 'assigned_doctor', None)
            if configured and configured.is_available:
                assigned_doctor = configured
            else:
                assigned_doctor = doctors[idx % len(doctors)]
            _consolidate_slot_for_day(day, slot_type, assigned_doctor)
    refresh_consultation_slot_capacities()


def get_daily_slots_for_dates(dates):
    """Return at most one slot per slot_type for each date (after ensure)."""
    ensure_today_tomorrow_slots()
    result = []
    for day in dates:
        for slot_type in SLOT_TYPES:
            slot = ConsultationSlot.objects.filter(
                date=day,
                slot_type=slot_type,
                doctor__is_available=True,
            ).select_related('doctor__user').first()
            if slot:
                result.append(slot)
    return result


def get_doctor_for_user(user):
    if user.role != 'doctor':
        return None
    return DoctorProfile.objects.filter(user=user).first()


def consultation_fee_with_charge(base_fee=None):
    base = base_fee or CONSULTATION_BASE_FEE
    # Match patient UI (Math.round(fee * 0.015)) so eSewa line items sum to total.
    service = Decimal(str(round(float(base * SERVICE_CHARGE_RATE))))
    return base, service, base + service
