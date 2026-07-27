
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import LabOrder, Payment, Prescription, Token
from core.permissions import IsPatient
from core.services.followup import (
    FollowupBookingError,
    book_followup,
    list_patient_followup_opportunities,
    resolve_followup_visit_date,
)
from core.services.lab_payments import LabPaymentError, PENDING_LAB_STATUSES, pay_lab_order, pay_lab_orders_for_token
from core.utils import format_local_time, doctor_name_short, doctor_specialty, serialize_token


def _patient_token_filter(user, prefix=''):
    """Match tokens by linked account or phone number."""
    q = Q(**{f'{prefix}patient_phone': user.phone})
    if user.id:
        q |= Q(**{f'{prefix}patient_id': user.id})
    return q


def _serialize_pending_lab_order(order):
    return {
        'order_id': order.id,
        'token_id': order.token_id,
        'token_number': order.token.token_number,
        'test_name': order.test_name,
        'amount': float(order.fee),
        'date': order.token.slot.date.isoformat(),
        'date_display': format_local_time(order.ordered_at, '%d %b %Y'),
        'doctor_name': str(order.token.slot.doctor),
        'payment_status': 'pending',
        'order_status': order.status,
    }


def _patient_pending_lab_orders(user):
    repair_corrupt_lab_orders()
    normalize_pending_lab_order_names()
    return LabOrder.objects.filter(
        _patient_token_filter(user, 'token__'),
        status__in=PENDING_LAB_STATUSES,
    ).select_related('token', 'token__slot__doctor').order_by('-ordered_at')


def _patient_token_belongs_to_user(user, token_id):
    return Token.objects.filter(_patient_token_filter(user), id=token_id).exists()


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def get_patient_tokens(request):
    from core.services.workflow import expire_all_ended_slots

    expire_all_ended_slots()

    tokens = Token.objects.filter(
        _patient_token_filter(request.user)
    ).select_related('slot__doctor__user').order_by('-created_at')

    tokens_data = [serialize_token(t, include_queue=True) for t in tokens]
    return Response({'success': True, 'tokens': tokens_data})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_queue_status(request):
    from core.services.workflow import expire_all_ended_slots
    from core import constants as C

    expire_all_ended_slots()

    today = timezone.localdate()
    token = Token.objects.filter(
        _patient_token_filter(request.user),
        slot__date=today,
        status__in=['booked', 'checked_in', 'consulting', 'pending_lab', 'pending_pharmacy'],
    ).select_related('slot__doctor__user', 'patient').order_by('-created_at').first()

    if not token:
        return Response({'success': True, 'has_active': False})

    queue_position = None
    queue_length = 0
    try:
        entry = token.queue_entry
        if entry and entry.queue_status == 'waiting':
            queue_position = entry.queue_position
            from core.models import QueueEntry
            queue_length = QueueEntry.objects.filter(
                slot=token.slot,
                queue_status='waiting',
            ).count()
    except Exception:
        pass

    pharmacy = None
    pharmacy_status = None
    pharmacy_display = None
    try:
        pharmacy = token.pharmacy_queue_entry
        pharmacy_status = pharmacy.status
        pharmacy_display = C.PHARMACY_DISPLAY.get(pharmacy.status, pharmacy.status)
    except Exception:
        pass

    token_data = serialize_token(token, include_queue=True, include_workflow=True)
    if pharmacy_display:
        token_data['pharmacy_display'] = pharmacy_display

    return Response({
        'success': True,
        'has_active': True,
        'token': token_data,
        'queue_position': queue_position,
        'queue_length': queue_length,
        'pharmacy_status': pharmacy_status,
        'pharmacy_display': pharmacy_display,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_prescriptions(request):
    prescriptions = Prescription.objects.filter(
        _patient_token_filter(request.user, 'token__')
    ).select_related('token', 'consultation', 'token__slot__doctor').order_by('-id')

    data = []
    for p in prescriptions:
        consult = getattr(p, 'consultation', None)
        data.append({
            'token_number': p.token.token_number,
            'medicine_name': p.medicine_name,
            'dosage': p.dosage,
            'frequency': p.frequency,
            'duration_days': p.duration_days,
            'instructions': p.instructions,
            'dispensed': p.dispensed,
            'date': p.token.slot.date.isoformat(),
            'doctor_name': str(p.token.slot.doctor),
            'diagnosis': consult.diagnosis if consult else '',
            'symptoms': consult.symptoms if consult else '',
        })
    return Response({'success': True, 'prescriptions': data})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_lab_reports(request):
    orders = LabOrder.objects.filter(
        _patient_token_filter(request.user, 'token__'),
        status='completed',
    ).select_related('report', 'token', 'token__slot__doctor').order_by('-ordered_at')
    data = []
    for order in orders:
        report = getattr(order, 'report', None)
        data.append({
            'order_id': order.id,
            'token_number': order.token.token_number,
            'test_name': order.test_name,
            'findings': report.findings if report else '',
            'uploaded_at': report.uploaded_at.isoformat() if report else None,
            'report_url': report.report_file.url if report and report.report_file else None,
            'date': order.token.slot.date.isoformat(),
            'doctor_name': str(order.token.slot.doctor),
        })
    return Response({'success': True, 'lab_reports': data})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_bills(request):
    payments = Payment.objects.filter(
        _patient_token_filter(request.user, 'token__')
    ).select_related('token', 'token__slot__doctor').order_by('-paid_at')
    data = []
    for pay in payments:
        data.append({
            'payment_id': pay.id,
            'token_number': pay.token.token_number,
            'payment_type': pay.payment_type,
            'amount': float(pay.amount),
            'status': pay.status,
            'reference_number': pay.reference_number or '',
            'paid_at': pay.paid_at.isoformat() if pay.paid_at else None,
            'paid_at_display': format_local_time(pay.paid_at, '%d %b %Y, %I:%M %p') if pay.paid_at else None,
            'doctor_name': doctor_name_short(pay.token.slot.doctor),
            'doctor_specialization': doctor_specialty(pay.token.slot.doctor),
            'visit_date': pay.token.slot.date.isoformat(),
        })
    return Response({'success': True, 'bills': data})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_followups(request):
    """Follow-up reminders and fee-exempt booking opportunities."""
    q = _patient_token_filter(request.user, 'token__')
    opportunities = list_patient_followup_opportunities(q)
    actionable = [o for o in opportunities if o['can_book']]
    return Response({
        'success': True,
        'followups': opportunities,
        'actionable_count': len(actionable),
        'exempt_within_days': opportunities[0]['exempt_within_days'] if opportunities else 3,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def create_followup(request, token_id):
    try:
        original = Token.objects.select_related(
            'slot__doctor', 'consultation', 'patient',
        ).filter(_patient_token_filter(request.user)).get(id=token_id)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)

    consult = getattr(original, 'consultation', None)
    if not consult or not consult.followup_date:
        return Response({'success': False, 'error': 'No follow-up scheduled for this visit'}, status=400)

    try:
        visit_date = resolve_followup_visit_date(
            original,
            consult,
            request.data.get('date'),
        )
        followup, fee_exempt = book_followup(original, visit_date, request.user)
    except FollowupBookingError as exc:
        return Response({'success': False, 'error': exc.message}, status=exc.status_code)

    return Response({
        'success': True,
        'followup_token': followup.token_number,
        'fee_exempted': fee_exempt,
        'visit_date': visit_date.isoformat(),
        'token': serialize_token(followup),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_lab_payments(request):
    """Pending lab test fees the patient can pay online."""
    orders = _patient_pending_lab_orders(request.user)
    items = [_serialize_pending_lab_order(o) for o in orders]
    total = sum(item['amount'] for item in items)
    return Response({
        'success': True,
        'pending_lab_payments': items,
        'count': len(items),
        'total_amount': total,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_pay_lab_fee(request, order_id):
    try:
        order = LabOrder.objects.select_related('token').get(id=order_id)
    except LabOrder.DoesNotExist:
        return Response({'success': False, 'error': 'Lab order not found'}, status=404)

    if not _patient_token_belongs_to_user(request.user, order.token_id):
        return Response({'success': False, 'error': 'Lab order not found'}, status=404)

    try:
        order, payment, entry = pay_lab_order(
            order_id,
            order.fee,
            request.user,
            request.data.get('reference_number', f'patient-lab-{order.id}'),
        )
    except LabPaymentError as exc:
        return Response({'success': False, 'error': exc.message}, status=exc.status_code)

    return Response({
        'success': True,
        'message': 'Lab fee paid successfully',
        'order_id': order.id,
        'payment_id': payment.id,
        'amount': float(payment.amount),
        'status': order.status,
        'payment_status': 'paid',
        'paid_at': payment.paid_at.isoformat(),
        'paid_at_display': format_local_time(payment.paid_at, '%d %b %Y, %I:%M %p'),
        'queue_entry_id': entry.id,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_pay_lab_fees_for_token(request, token_id):
    if not _patient_token_belongs_to_user(request.user, token_id):
        return Response({'success': False, 'error': 'Appointment not found'}, status=404)

    repair_corrupt_lab_orders(token_id)
    try:
        orders, payments, entries, total = pay_lab_orders_for_token(
            token_id,
            request.user,
            request.data.get('reference_number', f'patient-lab-token-{token_id}'),
        )
    except LabPaymentError as exc:
        return Response({'success': False, 'error': exc.message}, status=exc.status_code)

    paid_at = payments[-1].paid_at if payments else None
    return Response({
        'success': True,
        'message': 'Lab fees paid successfully',
        'token_id': token_id,
        'orders_paid': len(orders),
        'total_amount': float(total),
        'payment_status': 'paid',
        'paid_at': paid_at.isoformat() if paid_at else None,
        'paid_at_display': format_local_time(paid_at, '%d %b %Y, %I:%M %p') if paid_at else None,
        'payment_ids': [p.id for p in payments],
        'queue_entry_ids': [e.id for e in entries],
    })
