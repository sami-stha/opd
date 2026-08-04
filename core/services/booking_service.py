"""Shared token booking logic for direct book and eSewa gateway fulfillment."""
from decimal import Decimal

from django.utils import timezone

from accounts.models import User
from core.models import ConsultationSlot, Payment, Token
from core.services.sms import sms_token_booking
from core.utils import (
    consultation_fee_with_charge,
    duplicate_slot_booking_error,
    format_local_time,
    OLD_PATIENT_BOOKING_MSG,
    patient_has_active_slot_booking,
    serialize_token,
)


class BookingError(Exception):
    def __init__(self, message, status_code=400, extra=None):
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


def resolve_booking_patient(data, request_user=None):
    """Validate booking fields and return (slot, patient_user, booking_fields dict)."""
    slot_id = data.get('slot_id')
    patient_name = data.get('patient_name')
    patient_age = data.get('patient_age')
    patient_phone = data.get('patient_phone')
    patient_address = data.get('patient_address', '')
    is_disabled_raw = data.get('is_disabled')
    patient_id = data.get('patient_id')

    if not all([slot_id, patient_name, patient_age]) and not patient_id:
        raise BookingError('Missing required fields')

    if patient_id and not patient_phone:
        raise BookingError('Patient phone required for old patients')

    if not patient_id and patient_phone:
        existing_patient = User.objects.filter(phone=patient_phone, role='patient').first()
        if existing_patient and existing_patient.patient_code:
            raise BookingError(
                OLD_PATIENT_BOOKING_MSG,
                extra={'requires_old_patient': True},
            )

    patient_user = None
    if patient_id:
        patient_user = User.resolve_patient_id(patient_id)
        if not patient_user:
            raise BookingError('Patient ID not found', 404)
        if patient_user.phone != patient_phone:
            raise BookingError('Phone number does not match patient record')
        patient_name = patient_name or (patient_user.get_full_name() or patient_user.username)
        patient_age = patient_age or patient_user.age or 30
        patient_address = patient_address or patient_user.address or ''

    try:
        patient_age = int(patient_age)
    except (TypeError, ValueError):
        raise BookingError('Invalid age')

    try:
        slot = ConsultationSlot.objects.select_related('doctor').get(id=slot_id)
    except ConsultationSlot.DoesNotExist:
        raise BookingError('Slot not found', 404)

    from core.services.slot_config import is_slot_bookable
    if not is_slot_bookable(slot):
        if slot.is_full:
            raise BookingError(
                f'Slot is full! Maximum {slot.max_tokens} tokens allowed.',
            )
        raise BookingError('This slot has already passed. Please choose another slot or date.')

    if not patient_user:
        if request_user and getattr(request_user, 'is_authenticated', False) and request_user.role == 'patient':
            patient_user = request_user
        elif patient_phone:
            patient_user = User.objects.filter(phone=patient_phone, role='patient').first()

    if patient_has_active_slot_booking(slot, patient_user=patient_user, patient_phone=patient_phone):
        raise BookingError(duplicate_slot_booking_error(slot))

    if is_disabled_raw is not None:
        disabled_flag = bool(is_disabled_raw)
    elif patient_user:
        disabled_flag = bool(getattr(patient_user, 'is_disabled', False))
    else:
        disabled_flag = False

    return slot, patient_user, {
        'slot_id': slot.id,
        'patient_name': patient_name,
        'patient_age': patient_age,
        'patient_phone': patient_phone,
        'patient_address': patient_address or '',
        'patient_id': patient_id or (patient_user.patient_id if patient_user else None),
        'is_disabled': disabled_flag,
        'patient_user_id': patient_user.id if patient_user else None,
    }


def create_consultation_booking(booking_fields, payment_method='esewa', reference_number='', paid_amount=None):
    """Create token + consultation payment after gateway confirms."""
    slot = ConsultationSlot.objects.select_related('doctor').get(id=booking_fields['slot_id'])
    patient_user = None
    if booking_fields.get('patient_user_id'):
        patient_user = User.objects.filter(id=booking_fields['patient_user_id']).first()

    token = Token.objects.create(
        slot=slot,
        patient=patient_user,
        patient_name=booking_fields['patient_name'],
        patient_age=booking_fields['patient_age'],
        patient_phone=booking_fields['patient_phone'],
        patient_address=booking_fields.get('patient_address', ''),
        is_disabled=booking_fields.get('is_disabled', False),
    )

    base, service, total = consultation_fee_with_charge()
    amount = Decimal(str(paid_amount)) if paid_amount is not None else total
    payment = Payment.objects.create(
        token=token,
        payment_type='consultation_fee',
        amount=amount,
        status='paid',
        reference_number=reference_number or f'{payment_method}-{token.id}',
        paid_at=timezone.now(),
    )

    estimated_str = format_local_time(token.estimated_time) or ''
    sms_result = sms_token_booking(
        token.token_number,
        estimated_str,
        booking_fields['patient_phone'],
        token.slot.start_time,
    )

    return token, payment, sms_result


def booking_success_payload(token, payment, sms_result):
    payload = {
        'success': True,
        'message': 'Token booked successfully!',
        'token': serialize_token(token),
        'payment_id': payment.id,
        'sms_sent': sms_result.success,
    }
    if not sms_result.success:
        payload['sms_warning'] = sms_result.error
    return payload
