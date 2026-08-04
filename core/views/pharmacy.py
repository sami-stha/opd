from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core import constants as C
from core.utils import patient_id_for_token
from core.models import Payment, PharmacyQueueEntry, Prescription, Token
from core.permissions import IsPharmacist
from core.services.workflow import pharmacy_mark_ready


def _serialize_pharmacy_entry(entry):
    token = entry.token
    consult = getattr(token, 'consultation', None)
    prescriptions = Prescription.objects.filter(token=token)
    return {
        'entry_id': entry.id,
        'appointment_id': token.id,
        'token_id': token.id,
        'token_number': token.token_number,
        'patient_id': patient_id_for_token(token),
        'patient_name': token.patient_name,
        'patient_age': token.patient_age,
        'doctor_name': str(token.slot.doctor),
        'date': token.slot.date.isoformat(),
        'status': entry.status,
        'display_status': C.PHARMACY_DISPLAY.get(entry.status, entry.status),
        'appointment_status': token.status,
        'total_bill': float(entry.total_bill),
        'payment_collected': entry.payment_collected,
        'diagnosis': consult.diagnosis if consult else '',
        'symptoms': consult.symptoms if consult else '',
        'notes': consult.notes if consult else '',
        'prescriptions': [{
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
@permission_classes([IsAuthenticated, IsPharmacist])
def pharmacy_queue(request):
    today = timezone.localdate()
    entries = PharmacyQueueEntry.objects.filter(
        token__slot__date=today,
    ).select_related('token__slot__doctor', 'token__consultation', 'token__patient').order_by('entered_at')

    waiting = []
    processing = []
    ready = []
    dispensed = []
    for entry in entries:
        item = _serialize_pharmacy_entry(entry)
        if entry.status == C.PHARMACY_DONE:
            dispensed.append(item)
        elif entry.status == C.PHARMACY_READY:
            ready.append(item)
        elif entry.status == C.PHARMACY_DISPENSING:
            processing.append(item)
        else:
            waiting.append(item)

    active = waiting + processing + ready
    return Response({
        'success': True,
        'waiting': waiting,
        'processing': processing,
        'ready': ready,
        'dispensed': dispensed,
        'queue': active,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPharmacist])
def pharmacy_start_dispense(request, entry_id):
    try:
        entry = PharmacyQueueEntry.objects.get(id=entry_id)
    except PharmacyQueueEntry.DoesNotExist:
        return Response({'success': False, 'error': 'Entry not found'}, status=404)
    entry.start_dispensing(request.user)
    return Response({'success': True, 'entry': _serialize_pharmacy_entry(entry)})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPharmacist])
def pharmacy_mark_ready_view(request, entry_id):
    try:
        entry = PharmacyQueueEntry.objects.get(id=entry_id)
    except PharmacyQueueEntry.DoesNotExist:
        return Response({'success': False, 'error': 'Entry not found'}, status=404)
    try:
        pharmacy_mark_ready(entry)
    except ValidationError as exc:
        return Response({'success': False, 'error': str(exc)}, status=400)
    entry.refresh_from_db()
    return Response({
        'success': True,
        'message': 'Ready for pickup',
        'entry': _serialize_pharmacy_entry(entry),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPharmacist])
def pharmacy_complete_dispense(request, entry_id):
    try:
        entry = PharmacyQueueEntry.objects.get(id=entry_id)
    except PharmacyQueueEntry.DoesNotExist:
        return Response({'success': False, 'error': 'Entry not found'}, status=404)

    amount = request.data.get('amount', entry.total_bill)
    entry.total_bill = Decimal(str(amount))
    entry.payment_collected = True
    entry.save()

    Payment.objects.create(
        token=entry.token,
        payment_type='pharmacy_fee',
        amount=entry.total_bill,
        status='paid',
        collected_by=request.user,
        paid_at=timezone.now(),
        reference_number=request.data.get('reference_number', f'pharm-{entry.id}'),
    )
    entry.complete()
    return Response({
        'success': True,
        'message': 'Prescription dispensed',
        'entry': _serialize_pharmacy_entry(entry),
    })
