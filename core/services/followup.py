"""Follow-up booking and fee exemption helpers."""
from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

from core.models import Consultation, ConsultationSlot, FollowupRule, Payment, Token
from core.utils import consultation_fee_with_charge


class FollowupBookingError(Exception):
    def __init__(self, message, status_code=400):
        self.message = message
        self.status_code = status_code


def get_exempt_within_days():
    rule = FollowupRule.get_active()
    return rule.exempt_within_days if rule else 3


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
    return FollowupRule.check_exemption(original_token, visit_date)


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

    show_reminder = False
    if booked:
        show_reminder = False
    elif scheduled and scheduled >= today:
        show_reminder = True
    elif within_fee_window:
        show_reminder = True

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
        'can_book': show_reminder and recommended is not None,
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
        item = serialize_followup_opportunity(consult)
        if item['already_booked'] or item['can_book'] or (
            consult.followup_date and consult.followup_date >= today
        ):
            opportunities.append(item)

    return opportunities
