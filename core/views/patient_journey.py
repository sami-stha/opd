"""Extended patient portal — unified appointment journey."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core import constants as C
from core.models import LabOrder, Payment, Prescription, Token
from core.permissions import IsPatient
from core.services.followup import list_patient_followup_opportunities
from core.services.lab_orders import normalize_pending_lab_order_names, repair_corrupt_lab_orders
from core.services.lab_payments import PENDING_LAB_STATUSES
from core.utils import format_local_time, serialize_token, doctor_name_short, doctor_specialty


def _patient_token_filter(user, prefix=''):
    q = Q(**{f'{prefix}patient_phone': user.phone})
    if user.id:
        q |= Q(**{f'{prefix}patient_id': user.id})
    return q


def _prescription_slip(token):
    consult = getattr(token, 'consultation', None)
    prescriptions = list(token.prescriptions.all())
    return {
        'appointment_id': token.id,
        'token_number': token.token_number,
        'patient_name': token.patient_name,
        'patient_age': token.patient_age,
        'patient_id': token.patient.patient_id if token.patient_id and hasattr(token.patient, 'patient_id') else None,
        'date': token.slot.date.isoformat(),
        'doctor_name': str(token.slot.doctor),
        'status': token.status,
        'display_status': C.DISPLAY_STATUS.get(token.status, token.status),
        'symptoms': consult.symptoms if consult else '',
        'diagnosis': consult.diagnosis if consult else '',
        'notes': consult.notes if consult else '',
        'medicines': [{
            'medicine_name': p.medicine_name,
            'name': p.medicine_name,
            'dosage': p.dosage,
            'frequency': p.frequency,
            'duration_days': p.duration_days,
            'instructions': p.instructions,
            'dispensed': p.dispensed,
        } for p in prescriptions],
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_journey(request):
    """Single endpoint for patient dashboard — status, rx, lab, pharmacy, payments."""
    from core.services.workflow import expire_all_ended_slots

    expire_all_ended_slots()

    today = timezone.localdate()
    tokens = Token.objects.filter(
        _patient_token_filter(request.user),
    ).select_related(
        'slot__doctor__user', 'patient', 'consultation', 'pharmacy_queue_entry',
    ).prefetch_related('prescriptions', 'lab_orders__report', 'payments').order_by('-created_at')

    active_token = tokens.filter(
        slot__date=today,
        status__in=C.ACTIVE_STATUSES,
    ).first()

    journey = None
    if active_token:
        try:
            pharmacy = active_token.pharmacy_queue_entry
        except Exception:
            pharmacy = None
        queue_position = None
        try:
            entry = active_token.queue_entry
            if entry and entry.queue_status == 'waiting':
                queue_position = entry.queue_position
        except Exception:
            queue_position = None

        journey = {
            **serialize_token(active_token, include_queue=True, include_workflow=True),
            'queue_position': queue_position,
            'pharmacy_status': pharmacy.status if pharmacy else None,
            'pharmacy_display': C.PHARMACY_DISPLAY.get(pharmacy.status) if pharmacy else None,
            'lifecycle': [
                {'stage': 'booked', 'label': 'Booked', 'done': True},
                {'stage': 'checked_in', 'label': 'Checked In', 'done': active_token.status != C.BOOKED},
                {'stage': 'waiting', 'label': 'Waiting', 'done': active_token.status not in (C.BOOKED,)},
                {'stage': 'consulting', 'label': 'With Doctor', 'done': active_token.status not in (C.BOOKED, C.CHECKED_IN)},
                {
                    'stage': 'lab',
                    'label': 'Lab',
                    'done': (
                        not active_token.lab_orders.exclude(status='completed').exists()
                        if active_token.lab_orders.exists()
                        else active_token.status not in (C.BOOKED, C.CHECKED_IN, C.CONSULTING)
                    ),
                },
                {
                    'stage': 'pharmacy',
                    'label': 'Pharmacy',
                    'done': (
                        pharmacy.status == C.PHARMACY_DONE
                        if pharmacy
                        else (
                            not active_token.prescriptions.exists()
                            and active_token.status not in (C.BOOKED, C.CHECKED_IN, C.CONSULTING)
                        )
                    ),
                },
                {'stage': 'completed', 'label': 'Completed', 'done': active_token.status == C.COMPLETED},
            ],
        }

    prescriptions = [_prescription_slip(t) for t in tokens if t.prescriptions.exists()][:20]
    lab_reports = []
    for order in LabOrder.objects.filter(
        _patient_token_filter(request.user, 'token__'),
        status='completed',
    ).select_related('report', 'token').order_by('-ordered_at')[:20]:
        report = getattr(order, 'report', None)
        lab_reports.append({
            'order_id': order.id,
            'appointment_id': order.token_id,
            'token_number': order.token.token_number,
            'test_name': order.test_name,
            'findings': report.findings if report else '',
            'uploaded_at': report.uploaded_at.isoformat() if report else None,
            'report_url': report.report_file.url if report and report.report_file else None,
            'date': order.token.slot.date.isoformat(),
            'doctor_name': str(order.token.slot.doctor),
        })

    payments_qs = Payment.objects.filter(
        _patient_token_filter(request.user, 'token__'),
    ).select_related('token', 'token__slot__doctor').order_by('-paid_at')[:20]
    payments = [{
        'payment_id': p.id,
        'appointment_id': p.token_id,
        'token_number': p.token.token_number,
        'payment_type': p.payment_type,
        'amount': float(p.amount),
        'status': p.status,
        'reference_number': p.reference_number or '',
        'paid_at_display': format_local_time(p.paid_at, '%d %b %Y, %I:%M %p') if p.paid_at else None,
        'doctor_name': doctor_name_short(p.token.slot.doctor),
        'doctor_specialization': doctor_specialty(p.token.slot.doctor),
        'visit_date': p.token.slot.date.isoformat(),
    } for p in payments_qs]

    repair_corrupt_lab_orders()
    normalize_pending_lab_order_names()
    pending_lab_orders = LabOrder.objects.filter(
        _patient_token_filter(request.user, 'token__'),
        status__in=PENDING_LAB_STATUSES,
    ).select_related('token', 'token__slot__doctor').order_by('-ordered_at')
    pending_lab_payments = [{
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
    } for order in pending_lab_orders]

    followups = list_patient_followup_opportunities(
        _patient_token_filter(request.user, 'token__'),
    )

    return Response({
        'success': True,
        'today': today.isoformat(),
        'tomorrow': (today + timedelta(days=1)).isoformat(),
        'has_active': bool(active_token),
        'journey': journey,
        'prescriptions': prescriptions,
        'lab_reports': lab_reports,
        'pending_lab_payments': pending_lab_payments,
        'followups': followups,
        'followup_actionable_count': sum(1 for f in followups if f.get('can_book')),
        'payments': payments,
        'tokens': [
            serialize_token(t, include_workflow=True)
            for t in tokens.order_by('-slot__date', 'slot__slot_type', '-created_at')
        ],
    })
