"""Follow-up booking and fee exemption helpers."""
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import Consultation, ConsultationSlot, FollowupRule, Payment, Token
from core.utils import (
    consultation_fee_with_charge,
    duplicate_slot_booking_error,
    patient_has_active_slot_booking,
    serialize_slot,
)


class FollowupBookingError(Exception):
    def __init__(self, message, status_code=400):
        self.message = message
        self.status_code = status_code


def get_exempt_within_days():
    rule = FollowupRule.get_active()
    return rule.exempt_within_days if rule else 7


def exemption_end_date(original_token):
    return original_token.slot.date + timedelta(days=get_exempt_within_days())


def followup_already_booked(original_token_id):
    return Token.objects.filter(
        original_token_id=original_token_id,
        is_followup=True,
    ).exclude(status__in=('cancelled', 'expired')).exists()


def get_booked_followup(original_token_id):
    return (
        Token.objects.filter(
            original_token_id=original_token_id,
            is_followup=True,
        )
        .exclude(status__in=('cancelled', 'expired'))
        .select_related('slot')
        .order_by('-created_at')
        .first()
    )


def is_fee_exempt(original_token, visit_date):
    if not original_token:
        return False
    days_diff = (visit_date - original_token.slot.date).days
    return 0 <= days_diff <= get_exempt_within_days()


def _parse_visit_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise FollowupBookingError('Invalid visit date', 400)


def resolve_followup_visit_date(original_token, consult, requested_date=None):
    today = timezone.localdate()
    scheduled = consult.followup_date if consult else None

    if requested_date is not None:
        visit = _parse_visit_date(requested_date)
    elif scheduled and scheduled >= today:
        visit = scheduled
    elif scheduled and scheduled < today and is_fee_exempt(original_token, today):
        visit = today
    else:
        visit = today

    if visit < today:
        raise FollowupBookingError('Follow-up cannot be booked for a past date', 400)

    return visit


def get_followup_slot(doctor, visit_date):
    return ConsultationSlot.objects.filter(doctor=doctor, date=visit_date).first()


def _record_followup_payment(followup_token, collected_by, fee_exempt):
    base, service, total = consultation_fee_with_charge()
    if fee_exempt:
        Payment.objects.create(
            token=followup_token,
            payment_type='consultation_fee',
            amount=total,
            status='waived',
            waiver_reason='Follow-up within fee exemption period',
            collected_by=collected_by,
            paid_at=timezone.now(),
            reference_number=f'followup-waived-{followup_token.id}',
        )
    else:
        Payment.objects.create(
            token=followup_token,
            payment_type='consultation_fee',
            amount=total,
            status='pending',
            reference_number=f'followup-pending-{followup_token.id}',
        )


def book_followup(original_token, visit_date, booked_by=None):
    """Create a follow-up token for the same doctor on visit_date."""
    if followup_already_booked(original_token.id):
        existing = get_booked_followup(original_token.id)
        raise FollowupBookingError(
            f'Follow-up already booked (token {existing.token_number})',
            400,
        )

    consult = getattr(original_token, 'consultation', None)
    if not consult or not consult.followup_date:
        raise FollowupBookingError('No follow-up was scheduled for this visit', 400)

    visit = _parse_visit_date(visit_date)
    today = timezone.localdate()
    if visit < today:
        raise FollowupBookingError('Follow-up cannot be booked for a past date', 400)

    slot = get_followup_slot(original_token.slot.doctor, visit)
    if not slot:
        raise FollowupBookingError(
            f'No consultation slot for Dr. {original_token.slot.doctor} on {visit.isoformat()}',
            400,
        )

    fee_exempt = is_fee_exempt(original_token, visit)
    try:
        followup = Token.objects.create(
            slot=slot,
            patient=original_token.patient,
            patient_name=original_token.patient_name,
            patient_age=original_token.patient_age,
            patient_phone=original_token.patient_phone,
            patient_address=original_token.patient_address,
            is_followup=True,
            fee_exempted=fee_exempt,
            original_token=original_token,
        )
    except ValidationError as exc:
        raise FollowupBookingError(str(exc), 400)

    _record_followup_payment(followup, booked_by, fee_exempt)
    return followup, fee_exempt


def book_followup_via_slot(
    original_token,
    slot,
    booked_by,
    payment_method=None,
    reference_number=None,
):
    """Book follow-up through normal slot selection (same workflow as online booking)."""
    if followup_already_booked(original_token.id):
        existing = get_booked_followup(original_token.id)
        raise FollowupBookingError(
            f'Follow-up already booked (token {existing.token_number})',
            400,
        )

    consult = getattr(original_token, 'consultation', None)
    if not consult or not consult.followup_date:
        raise FollowupBookingError('No follow-up was scheduled for this visit', 400)

    today = timezone.localdate()
    if today > exemption_end_date(original_token):
        raise FollowupBookingError(
            'Follow-up fee exemption period has ended. Please book a regular appointment.',
            400,
        )

    if slot.doctor_id != original_token.slot.doctor_id:
        raise FollowupBookingError('Follow-up must be booked with the same doctor', 400)

    from core.services.slot_config import is_slot_bookable
    if not is_slot_bookable(slot):
        raise FollowupBookingError('Selected slot is not available', 400)

    if patient_has_active_slot_booking(
        slot,
        patient_user=original_token.patient,
        patient_phone=original_token.patient_phone,
    ):
        raise FollowupBookingError(duplicate_slot_booking_error(slot), 400)

    fee_exempt = is_fee_exempt(original_token, slot.date)
    try:
        followup = Token.objects.create(
            slot=slot,
            patient=original_token.patient,
            patient_name=original_token.patient_name,
            patient_age=original_token.patient_age,
            patient_phone=original_token.patient_phone,
            patient_address=original_token.patient_address,
            is_followup=True,
            fee_exempted=fee_exempt,
            original_token=original_token,
        )
    except ValidationError as exc:
        raise FollowupBookingError(str(exc), 400)

    if fee_exempt:
        _record_followup_payment(followup, booked_by, True)
    else:
        method = (payment_method or 'esewa').lower().strip()
        if method != 'esewa':
            raise FollowupBookingError('Follow-up fees must be paid via eSewa', 400)
        _, _, total = consultation_fee_with_charge()
        Payment.objects.create(
            token=followup,
            payment_type='consultation_fee',
            amount=total,
            status='paid',
            collected_by=booked_by,
            paid_at=timezone.now(),
            reference_number=reference_number or f'esewa-{followup.id}',
        )

    from core.services.sms import sms_token_booking
    from core.utils import format_local_time
    sms_token_booking(
        followup.token_number,
        format_local_time(followup.estimated_time) or '',
        followup.patient_phone,
        followup.slot.start_time,
    )

    return followup, fee_exempt


def get_followup_booking_context(original_token):
    """Slots and fee-exemption info for the follow-up booking portal."""
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    consult = getattr(original_token, 'consultation', None)
    doctor = original_token.slot.doctor

    from core.services.workflow import expire_all_ended_slots
    from core.utils import ensure_today_tomorrow_slots

    expire_all_ended_slots()
    ensure_today_tomorrow_slots()

    slots = ConsultationSlot.objects.filter(
        doctor=doctor,
        date__in=[today, tomorrow],
    ).select_related('doctor__user')

    slots_data = [serialize_slot(s) for s in slots]
    grouped = {
        'today': [s for s in slots_data if s['date'] == today.isoformat()],
        'tomorrow': [s for s in slots_data if s['date'] == tomorrow.isoformat()],
    }

    return {
        'original_token_id': original_token.id,
        'token_number': original_token.token_number,
        'doctor_id': doctor.id,
        'doctor_name': str(doctor),
        'followup_date': consult.followup_date.isoformat() if consult and consult.followup_date else None,
        'followup_instructions': (consult.followup_instructions or '') if consult else '',
        'diagnosis': (consult.diagnosis or '') if consult else '',
        'exempt_within_days': get_exempt_within_days(),
        'exemption_end_date': exemption_end_date(original_token).isoformat(),
        'days_until_exemption_ends': max(0, (exemption_end_date(original_token) - today).days),
        'fee_exempt_today': is_fee_exempt(original_token, today),
        'fee_exempt_tomorrow': is_fee_exempt(original_token, tomorrow),
        'already_booked': followup_already_booked(original_token.id),
        'slots': slots_data,
        'grouped': grouped,
        'today': today.isoformat(),
        'tomorrow': tomorrow.isoformat(),
    }


def serialize_followup_opportunity(consult):
    """Build patient-portal payload for one schedulable follow-up."""
    original = consult.token
    today = timezone.localdate()
    exempt_days = get_exempt_within_days()
    original_date = original.slot.date
    exemption_end = exemption_end_date(original)
    scheduled = consult.followup_date
    booked = get_booked_followup(original.id)

    within_fee_window = today <= exemption_end
    days_until_exemption_ends = max(0, (exemption_end - today).days)

    recommended = None
    if scheduled and scheduled >= today:
        recommended = scheduled
    elif within_fee_window:
        recommended = today

    fee_exempt_on_recommended = (
        recommended is not None and is_fee_exempt(original, recommended)
    )

    return {
        'original_token_id': original.id,
        'token_number': original.token_number,
        'visit_date': original_date.isoformat(),
        'doctor_id': original.slot.doctor_id,
        'doctor_name': str(original.slot.doctor),
        'followup_date': scheduled.isoformat() if scheduled else None,
        'followup_date_display': scheduled.strftime('%d %b %Y') if scheduled else None,
        'followup_instructions': consult.followup_instructions or '',
        'diagnosis': consult.diagnosis or '',
        'exempt_within_days': exempt_days,
        'exemption_end_date': exemption_end.isoformat(),
        'exemption_end_display': exemption_end.strftime('%d %b %Y'),
        'days_until_exemption_ends': days_until_exemption_ends,
        'fee_exempt_available': within_fee_window,
        'fee_exempt_on_recommended_date': fee_exempt_on_recommended,
        'already_booked': booked is not None,
        'can_book': (not booked) and within_fee_window and recommended is not None,
        'recommended_booking_date': recommended.isoformat() if recommended else None,
        'booked_followup': {
            'token_id': booked.id,
            'token_number': booked.token_number,
            'date': booked.slot.date.isoformat(),
            'fee_exempted': booked.fee_exempted,
            'status': booked.status,
        } if booked else None,
    }


def list_patient_followup_opportunities(patient_token_filter_q):
    """Return follow-up reminders/opportunities for a patient query filter."""
    today = timezone.localdate()
    exempt_days = get_exempt_within_days()
    cutoff = today - timedelta(days=exempt_days)

    consultations = (
        Consultation.objects.filter(
            patient_token_filter_q,
            followup_date__isnull=False,
            token__slot__date__gte=cutoff,
        )
        .select_related('token', 'token__slot__doctor')
        .order_by('followup_date', '-token__slot__date')
    )

    opportunities = []
    for consult in consultations:
        original = consult.token
        if today > exemption_end_date(original):
            continue
        item = serialize_followup_opportunity(consult)
        if item['already_booked'] or item['can_book']:
            opportunities.append(item)

    return opportunities
