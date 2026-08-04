"""eSewa redirect payment — booking and patient-portal lab fees."""
from decimal import Decimal
from urllib.parse import urlencode

from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from core.models import ConsultationSlot, GatewayPaymentSession, LabOrder, Token
from core.permissions import IsPatient
from core.services.booking_service import (
    BookingError,
    booking_success_payload,
    create_consultation_booking,
    resolve_booking_patient,
)
from core.services.esewa_gateway import (
    build_payment_form_fields,
    check_transaction_status,
    decode_callback_payload,
    esewa_form_url,
    esewa_failure_url,
    new_transaction_uuid,
    site_base_url,
    verify_callback_payload,
)
from core.models import ConsultationSlot
from core.services.followup import book_followup_via_slot, is_fee_exempt
from core.services.lab_payments import LabPaymentError, PENDING_LAB_STATUSES, pay_lab_order, pay_lab_orders_for_token
from core.utils import consultation_fee_with_charge


def _gateway_response(session):
    fields = build_payment_form_fields(
        session.transaction_uuid,
        session.amount,
        session.tax_amount,
        session.service_charge,
        session.delivery_charge,
        session.total_amount,
    )
    return Response({
        'success': True,
        'gateway': 'esewa',
        'transaction_uuid': session.transaction_uuid,
        'form_url': esewa_form_url(),
        'form_fields': fields,
        'total_amount': float(session.total_amount),
    })


def _patient_token_belongs_to_user(user, token_id):
    from django.db.models import Q
    q = Q(patient_phone=user.phone)
    if user.id:
        q |= Q(patient_id=user.id)
    return Token.objects.filter(q, id=token_id).exists()


def create_esewa_booking_session(request):
    """Create pending gateway session for consultation booking."""
    _, patient_user, booking_fields = resolve_booking_patient(request.data, request.user)
    base, service, total = consultation_fee_with_charge()
    transaction_uuid = new_transaction_uuid('BOOK')

    session_user = None
    if request.user.is_authenticated and getattr(request.user, 'role', None) == 'patient':
        session_user = request.user
    elif patient_user:
        session_user = patient_user

    return GatewayPaymentSession.objects.create(
        transaction_uuid=transaction_uuid,
        purpose='consultation_booking',
        amount=base,
        tax_amount=Decimal('0'),
        service_charge=service,
        delivery_charge=Decimal('0'),
        total_amount=total,
        patient_user=session_user,
        metadata=booking_fields,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def esewa_initiate_booking(request):
    """Start eSewa payment for consultation token booking (redirect flow)."""
    try:
        session = create_esewa_booking_session(request)
    except BookingError as exc:
        payload = {'success': False, 'error': exc.message}
        if exc.extra:
            payload.update(exc.extra)
        return Response(payload, status=exc.status_code)
    return _gateway_response(session)


def _patient_token_belongs_to_user_filter(user):
    from django.db.models import Q
    q = Q(patient_phone=user.phone)
    if user.id:
        q |= Q(patient_id=user.id)
    return q


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def esewa_initiate_followup(request):
    """Start eSewa payment for follow-up token booking (redirect flow)."""
    original_token_id = request.data.get('original_token_id')
    slot_id = request.data.get('slot_id')
    if not original_token_id or not slot_id:
        return Response({
            'success': False,
            'error': 'original_token_id and slot_id are required',
        }, status=400)

    try:
        original = Token.objects.select_related(
            'slot__doctor', 'consultation', 'patient',
        ).filter(_patient_token_belongs_to_user_filter(request.user)).get(id=original_token_id)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)

    try:
        slot = ConsultationSlot.objects.select_related('doctor__user').get(id=slot_id)
    except ConsultationSlot.DoesNotExist:
        return Response({'success': False, 'error': 'Slot not found'}, status=404)

    if is_fee_exempt(original, slot.date):
        return Response({
            'success': False,
            'error': 'This follow-up is fee-exempt. Use Confirm without payment.',
            'fee_exempt': True,
        }, status=400)

    from core.services.followup import (
        exemption_end_date,
        followup_already_booked,
        get_booked_followup,
        patient_has_active_slot_booking,
        duplicate_slot_booking_error,
    )
    from core.services.slot_config import is_slot_bookable

    if followup_already_booked(original.id):
        existing = get_booked_followup(original.id)
        return Response({
            'success': False,
            'error': f'Follow-up already booked (token {existing.token_number})',
        }, status=400)

    consult = getattr(original, 'consultation', None)
    if not consult or not consult.followup_date:
        return Response({'success': False, 'error': 'No follow-up was scheduled for this visit'}, status=400)

    today = timezone.localdate()
    if today > exemption_end_date(original):
        return Response({
            'success': False,
            'error': 'Follow-up fee exemption period has ended. Please book a regular appointment.',
        }, status=400)

    if slot.doctor_id != original.slot.doctor_id:
        return Response({'success': False, 'error': 'Follow-up must be booked with the same doctor'}, status=400)

    if not is_slot_bookable(slot):
        return Response({'success': False, 'error': 'Selected slot is not available'}, status=400)

    if patient_has_active_slot_booking(
        slot,
        patient_user=original.patient,
        patient_phone=original.patient_phone,
    ):
        return Response({'success': False, 'error': duplicate_slot_booking_error(slot)}, status=400)

    base, service, total = consultation_fee_with_charge()
    transaction_uuid = new_transaction_uuid('FUP')

    session = GatewayPaymentSession.objects.create(
        transaction_uuid=transaction_uuid,
        purpose='followup_booking',
        amount=base,
        tax_amount=Decimal('0'),
        service_charge=service,
        delivery_charge=Decimal('0'),
        total_amount=total,
        patient_user=request.user,
        metadata={
            'original_token_id': int(original_token_id),
            'slot_id': int(slot_id),
        },
    )
    return _gateway_response(session)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def esewa_initiate_lab_order(request, order_id):
    try:
        order = LabOrder.objects.select_related('token').get(id=order_id)
    except LabOrder.DoesNotExist:
        return Response({'success': False, 'error': 'Lab order not found'}, status=404)

    if not _patient_token_belongs_to_user(request.user, order.token_id):
        return Response({'success': False, 'error': 'Lab order not found'}, status=404)

    if order.status not in PENDING_LAB_STATUSES:
        return Response({'success': False, 'error': 'Lab fee already processed'}, status=400)

    total = Decimal(str(order.fee))
    transaction_uuid = new_transaction_uuid('LAB')

    session = GatewayPaymentSession.objects.create(
        transaction_uuid=transaction_uuid,
        purpose='lab_order',
        amount=total,
        tax_amount=Decimal('0'),
        service_charge=Decimal('0'),
        delivery_charge=Decimal('0'),
        total_amount=total,
        patient_user=request.user,
        metadata={'order_id': order.id, 'token_id': order.token_id},
    )
    return _gateway_response(session)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def esewa_initiate_lab_token(request, token_id):
    if not _patient_token_belongs_to_user(request.user, token_id):
        return Response({'success': False, 'error': 'Appointment not found'}, status=404)

    orders = list(
        LabOrder.objects.filter(token_id=token_id, status__in=PENDING_LAB_STATUSES).order_by('ordered_at')
    )
    if not orders:
        return Response({'success': False, 'error': 'No pending lab fees for this patient'}, status=400)

    total = sum(Decimal(str(o.fee)) for o in orders)
    transaction_uuid = new_transaction_uuid('LABT')

    session = GatewayPaymentSession.objects.create(
        transaction_uuid=transaction_uuid,
        purpose='lab_token',
        amount=total,
        tax_amount=Decimal('0'),
        service_charge=Decimal('0'),
        delivery_charge=Decimal('0'),
        total_amount=total,
        patient_user=request.user,
        metadata={
            'token_id': token_id,
            'order_ids': [o.id for o in orders],
        },
    )
    return _gateway_response(session)


def _redirect_patient(path, params):
    query = urlencode({k: v for k, v in params.items() if v is not None and v != ''})
    url = f'{site_base_url()}/{path}'
    if query:
        url = f'{url}?{query}'
    return HttpResponseRedirect(url)


def _amounts_match(session_total, reported_total):
    try:
        return Decimal(str(reported_total)) == Decimal(str(session_total))
    except Exception:
        return False


@transaction.atomic
def _fulfill_session(session, esewa_code='', status_payload=None):
    if session.status == 'completed':
        return session

    session = GatewayPaymentSession.objects.select_for_update().get(pk=session.pk)
    if session.status == 'completed':
        return session

    if status_payload:
        reported_status = str(status_payload.get('status', '')).upper()
        if reported_status not in ('COMPLETE', 'COMPLETED'):
            session.status = 'failed'
            session.error_message = f'eSewa status: {reported_status}'
            session.save(update_fields=['status', 'error_message'])
            return session

    if esewa_code:
        session.esewa_transaction_code = esewa_code
    session.status = 'completed'
    session.completed_at = timezone.now()

    if session.purpose == 'consultation_booking':
        token, payment, sms_result = create_consultation_booking(
            session.metadata,
            payment_method='esewa',
            reference_number=session.transaction_uuid,
            paid_amount=session.total_amount,
        )
        session.token = token
        session.payment = payment
        session.save()
        session._sms_result = sms_result
        return session

    if session.purpose == 'followup_booking':
        original_id = session.metadata.get('original_token_id')
        slot_id = session.metadata.get('slot_id')
        original = Token.objects.select_related(
            'slot__doctor', 'consultation', 'patient',
        ).get(id=original_id)
        slot = ConsultationSlot.objects.select_related('doctor__user').get(id=slot_id)
        booked_by = session.patient_user
        followup, _ = book_followup_via_slot(
            original,
            slot,
            booked_by,
            payment_method='esewa',
            reference_number=session.transaction_uuid,
        )
        session.token = followup
        from core.models import Payment
        session.payment = Payment.objects.filter(
            token=followup,
            payment_type='consultation_fee',
        ).order_by('-paid_at').first()
        session.save()
        return session

    collected_by = session.patient_user
    ref = session.transaction_uuid

    if session.purpose == 'lab_order':
        order_id = session.metadata.get('order_id')
        order, payment, _ = pay_lab_order(order_id, session.total_amount, collected_by, ref)
        session.payment = payment
        session.token = order.token
        session.save()
        return session

    if session.purpose == 'lab_token':
        token_id = session.metadata.get('token_id')
        orders, payments, _, _ = pay_lab_orders_for_token(token_id, collected_by, ref)
        session.payment = payments[-1] if payments else None
        session.token_id = token_id
        if orders:
            session.token = orders[0].token
        session.save()
        return session

    session.status = 'failed'
    session.error_message = 'Unknown payment purpose'
    session.save(update_fields=['status', 'error_message'])
    return session


def _complete_from_callback(payload):
    transaction_uuid = payload.get('transaction_uuid')
    if not transaction_uuid:
        return None, 'Missing transaction reference'

    try:
        session = GatewayPaymentSession.objects.get(transaction_uuid=transaction_uuid)
    except GatewayPaymentSession.DoesNotExist:
        return None, 'Payment session not found'

    if not _amounts_match(session.total_amount, payload.get('total_amount')):
        session.status = 'failed'
        session.error_message = 'Amount mismatch'
        session.save(update_fields=['status', 'error_message'])
        return session, 'Payment amount mismatch'

    status_data = check_transaction_status(transaction_uuid, session.total_amount)
    status_payload = None
    if status_data and str(status_data.get('status', '')).upper() in ('COMPLETE', 'COMPLETED'):
        status_payload = status_data
    elif str(payload.get('status', '')).upper() in ('COMPLETE', 'COMPLETED'):
        status_payload = {'status': 'COMPLETE'}

    session = _fulfill_session(
        session,
        esewa_code=payload.get('transaction_code', ''),
        status_payload=status_payload,
    )
    return session, None


@api_view(['GET'])
@permission_classes([AllowAny])
def esewa_payment_success(request):
    raw = request.GET.get('data') or request.GET.get('encodedData')
    payload = decode_callback_payload(raw)

    if not payload or not verify_callback_payload(payload):
        return _redirect_patient(
            'patient/payment-failure.html',
            {'reason': 'invalid_callback'},
        )

    session, error = _complete_from_callback(payload)
    if error or not session:
        return _redirect_patient(
            'patient/payment-failure.html',
            {'reason': error or 'failed', 'uuid': payload.get('transaction_uuid', '')},
        )

    if session.status != 'completed':
        return _redirect_patient(
            'patient/payment-failure.html',
            {'reason': session.error_message or 'failed', 'uuid': session.transaction_uuid},
        )

    if session.purpose == 'consultation_booking':
        return _redirect_patient('patient/booking.html', {
            'confirmed': '1',
            'uuid': session.transaction_uuid,
            'token_id': session.token_id,
        })
    if session.purpose == 'followup_booking':
        return _redirect_patient('patient/followup-booking.html', {
            'confirmed': '1',
            'uuid': session.transaction_uuid,
            'token_id': session.token_id,
            'original_token_id': session.metadata.get('original_token_id'),
        })

    params = {
        'type': session.purpose,
        'uuid': session.transaction_uuid,
    }
    if session.token_id:
        params['token_id'] = session.token_id
        params['token_number'] = session.token.token_number
    if session.purpose in ('lab_order', 'lab_token'):
        params['type'] = 'lab'

    return _redirect_patient('patient/payment-success.html', params)


@api_view(['GET'])
@permission_classes([AllowAny])
def esewa_payment_failure(request):
    uuid = request.GET.get('transaction_uuid') or request.GET.get('uuid')
    if uuid:
        GatewayPaymentSession.objects.filter(
            transaction_uuid=uuid, status='pending',
        ).update(status='failed', error_message='Cancelled or failed at eSewa')
    return _redirect_patient(
        'patient/payment-failure.html',
        {'reason': 'cancelled', 'uuid': uuid or ''},
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def esewa_payment_status(request, transaction_uuid):
    try:
        session = GatewayPaymentSession.objects.select_related('token').get(
            transaction_uuid=transaction_uuid,
        )
    except GatewayPaymentSession.DoesNotExist:
        return Response({'success': False, 'error': 'Session not found'}, status=404)

    if session.status == 'pending':
        status_data = check_transaction_status(transaction_uuid, session.total_amount)
        if status_data and str(status_data.get('status', '')).upper() in ('COMPLETE', 'COMPLETED'):
            session = _fulfill_session(session, status_payload=status_data)

    data = {
        'success': True,
        'status': session.status,
        'purpose': session.purpose,
        'transaction_uuid': session.transaction_uuid,
        'total_amount': float(session.total_amount),
        'payment_method': 'esewa',
    }
    if session.token_id:
        token = session.token
        if token is None:
            token = Token.objects.select_related('slot__doctor__user', 'patient').get(pk=session.token_id)
        from core.utils import serialize_token
        data['token_id'] = session.token_id
        data['token_number'] = token.token_number
        data['token'] = serialize_token(token, include_queue=True)
    if session.status == 'failed':
        data['error'] = session.error_message
    return Response(data)
