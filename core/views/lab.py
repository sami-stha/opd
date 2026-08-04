from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import LabOrder, LabQueueEntry, LabReport
from core.permissions import IsLabTech, IsReceptionistOrAdmin
from core.services.sms import sms_lab_report_ready
from core.services.lab_orders import normalize_pending_lab_order_names, repair_corrupt_lab_orders
from core.services.workflow import after_lab_report_uploaded
from core.utils import serialize_token, patient_id_for_token, patient_gender_for_token, format_patient_gender


def _serialize_lab_order(order):
    entry = getattr(order, 'queue_entry', None)
    token = order.token
    report = getattr(order, 'report', None)
    report_url = report.report_file.url if report and report.report_file else None
    return {
        'order_id': order.id,
        'token_id': order.token_id,
        'token_number': token.token_number,
        'patient_id': patient_id_for_token(token),
        'patient_name': token.patient_name,
        'patient_age': token.patient_age,
        'patient_gender': format_patient_gender(patient_gender_for_token(token)),
        'test_name': order.test_name,
        'fee': float(order.fee),
        'status': order.status,
        'queue_status': entry.status if entry else None,
        'ordered_at': order.ordered_at.isoformat() if order.ordered_at else None,
        'started_at': entry.started_at.isoformat() if entry and entry.started_at else None,
        'completed_at': (
            order.completed_at.isoformat() if order.completed_at
            else (report.uploaded_at.isoformat() if report and report.uploaded_at else None)
        ),
        'findings': report.findings if report else '',
        'report_url': report_url,
        'lab_fee_paid': entry.lab_fee_paid if entry else False,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsLabTech])
def lab_queue(request):
    """Lab technician queue — only paid tests (after reception payment)."""
    repair_corrupt_lab_orders()
    normalize_pending_lab_order_names()
    today = timezone.localdate()

    entries = (
        LabQueueEntry.objects.filter(lab_fee_paid=True)
        .exclude(lab_order__status='completed')
        .select_related('lab_order', 'lab_order__token', 'lab_order__token__patient', 'token')
        .order_by('entered_at')
    )

    pending = []
    processing = []
    for entry in entries:
        order = entry.lab_order
        item = _serialize_lab_order(order)
        if entry.status == 'in_progress' or order.status == 'in_progress':
            processing.append(item)
        else:
            pending.append(item)

    completed_today = []
    for order in LabOrder.objects.filter(
        status='completed',
        completed_at__date=today,
    ).select_related('token', 'token__patient', 'queue_entry', 'report').order_by('-completed_at'):
        completed_today.append(_serialize_lab_order(order))

    completed_all = []
    for order in LabOrder.objects.filter(
        status='completed',
    ).select_related('token', 'token__patient', 'queue_entry', 'report').order_by('-completed_at'):
        completed_all.append(_serialize_lab_order(order))

    return Response({
        'success': True,
        'pending': pending,
        'processing': processing,
        'completed': completed_today,
        'completed_all': completed_all,
        'all': pending + processing + completed_today,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsLabTech])
def lab_start_test(request, order_id):
    try:
        order = LabOrder.objects.get(id=order_id)
        entry = order.queue_entry
    except (LabOrder.DoesNotExist, LabQueueEntry.DoesNotExist):
        return Response({'success': False, 'error': 'Lab order not in queue'}, status=404)
    entry.start(request.user)
    return Response({'success': True, 'message': 'Test started', 'order': _serialize_lab_order(order)})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsLabTech])
def lab_complete_test(request, order_id):
    try:
        order = LabOrder.objects.get(id=order_id)
        entry = order.queue_entry
    except (LabOrder.DoesNotExist, LabQueueEntry.DoesNotExist):
        return Response({'success': False, 'error': 'Lab order not in queue'}, status=404)

    findings = request.data.get('findings', '')
    report_file = request.FILES.get('report_file')

    entry.complete()
    report, _ = LabReport.objects.update_or_create(
        lab_order=order,
        defaults={
            'uploaded_by': request.user,
            'findings': findings,
        },
    )
    if report_file:
        report.report_file = report_file
        report.save()

    after_lab_report_uploaded(order)

    sms_result = None
    phone = order.token.patient_phone
    if phone:
        sms_result = sms_lab_report_ready(
            order.token.patient_name,
            order.test_name,
            phone,
        )

    payload = {
        'success': True,
        'message': 'Report uploaded',
        'order': _serialize_lab_order(order),
    }
    if sms_result is not None:
        payload['sms_sent'] = sms_result.success
        if not sms_result.success:
            payload['sms_warning'] = sms_result.error
    return Response(payload)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lab_reports_for_token(request, token_id):
    orders = LabOrder.objects.filter(token_id=token_id).select_related('report')
    data = []
    for order in orders:
        report = getattr(order, 'report', None)
        data.append({
            'order_id': order.id,
            'test_name': order.test_name,
            'status': order.status,
            'findings': report.findings if report else '',
            'uploaded_at': report.uploaded_at.isoformat() if report else None,
            'report_url': report.report_file.url if report and report.report_file else None,
        })
    return Response({'success': True, 'reports': data})
