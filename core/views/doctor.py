from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import (
    Consultation, ConsultationSlot, LabOrder, PharmacyQueueEntry,
    Prescription, Token,
)
from core.permissions import IsDoctor
from core.services.analytics import get_next_eligible_token
from core.services.workflow import complete_consultation as workflow_complete_consultation, _normalize_lab_test_names
from accounts.models import User
from core.utils import (
    format_local_time, get_doctor_for_user, patient_id_for_token,
    patient_is_new, patient_priority_category, serialize_token,
)


def _active_consultation_for_doctor(doctor_id, date=None):
    """Return the doctor's in-progress consultation token for the day, if any."""
    date = date or timezone.localdate()
    return (
        Token.objects.filter(
            slot__doctor_id=doctor_id,
            slot__date=date,
            status='consulting',
        )
        .select_related('slot', 'patient')
        .order_by('consultation_started_at')
        .first()
    )


def _queue_for_doctor(doctor_id, slot_type=None, statuses=('checked_in',)):
    from core.services.analytics import get_ordered_queue_tokens

    tokens = get_ordered_queue_tokens(doctor_id)
    if slot_type:
        tokens = [t for t in tokens if t.slot.slot_type == slot_type]

    queue_list = []
    for token in tokens:
        if token.status not in statuses:
            continue
        entry = getattr(token, 'queue_entry', None)
        queue_list.append({
            'position': len(queue_list) + 1,
            'queue_position': entry.queue_position if entry else len(queue_list) + 1,
            'token_id': token.id,
            'token_number': token.token_number,
            'patient_id': patient_id_for_token(token),
            'patient_name': token.patient_name,
            'patient_age': token.patient_age,
            'patient_phone': token.patient_phone,
            'is_elderly': token.is_elderly,
            'is_disabled': token.is_disabled,
            'is_followup': token.is_followup,
            'fee_exempted': token.fee_exempted,
            'is_new_patient': patient_is_new(token.patient),
            'priority': 'HIGH' if (token.is_elderly or token.is_disabled) else 'NORMAL',
            'category': patient_priority_category(token),
            'status': token.status,
            'start_time': token.slot.start_time,
            'end_time': token.slot.end_time,
            'slot_type': token.slot.slot_type,
        })
    return queue_list


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def doctor_schedule(request):
    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)
    today = timezone.localdate()
    slots = ConsultationSlot.objects.filter(doctor=profile, date=today)
    return Response({
        'success': True,
        'doctor': {'id': profile.id, 'name': str(profile), 'specialization': profile.specialization},
        'slots': [{
            'slot_id': s.id,
            'slot_type': s.slot_type,
            'tokens_booked': s.tokens_booked,
            'max_tokens': s.max_tokens,
        } for s in slots],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def doctor_queue(request, doctor_id=None):
    from core.services.workflow import expire_all_ended_slots

    expire_all_ended_slots()

    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)
    if doctor_id and int(doctor_id) != profile.id:
        return Response({'success': False, 'error': 'Access denied'}, status=403)

    queue_list = _queue_for_doctor(profile.id)
    active = _active_consultation_for_doctor(profile.id)
    return Response({
        'success': True,
        'doctor_id': profile.id,
        'queue_length': len(queue_list),
        'next_patient': queue_list[0] if queue_list else None,
        'queue': queue_list,
        'active_consultation': {
            'token_id': active.id,
            'token_number': active.token_number,
            'patient_name': active.patient_name,
            'patient_id': patient_id_for_token(active),
        } if active else None,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def next_patient(request, doctor_id=None):
    profile = get_doctor_for_user(request.user)
    queue_list = _queue_for_doctor(profile.id)
    if not queue_list:
        return Response({'success': True, 'has_next': False})
    return Response({'success': True, 'has_next': True, 'next_patient': queue_list[0]})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsDoctor])
def start_consultation(request, token_id):
    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)

    try:
        token = Token.objects.get(id=token_id, slot__doctor=profile)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)

    active = _active_consultation_for_doctor(profile.id)
    if active and active.id != token.id:
        return Response({
            'success': False,
            'error': (
                f'Finish consultation for token {active.token_number} '
                f'({active.patient_name}) before calling the next patient.'
            ),
            'active_consultation': {
                'token_id': active.id,
                'token_number': active.token_number,
                'patient_name': active.patient_name,
            },
        }, status=400)

    next_token = get_next_eligible_token(profile.id)
    if next_token and next_token.id != token.id:
        return Response({
            'success': False,
            'error': (
                f'Queue discipline enforced: call token {next_token.token_number} '
                f'({next_token.patient_name}) first.'
            ),
            'next_patient': {
                'token_id': next_token.id,
                'token_number': next_token.token_number,
                'patient_name': next_token.patient_name,
            },
        }, status=400)

    try:
        token.start_consultation()
    except ValidationError as exc:
        msg = '; '.join(getattr(exc, 'messages', []) or [str(exc)])
        return Response({'success': False, 'error': msg}, status=400)
    return Response({
        'success': True,
        'message': f'Consultation started for {token.patient_name}',
        'token': serialize_token(token),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsDoctor])
def complete_consultation(request, token_id):
    try:
        token = Token.objects.get(id=token_id, slot__doctor__user=request.user)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)

    if token.status != 'consulting':
        return Response({'success': False, 'error': f'Cannot complete. Status: {token.status}'}, status=400)

    try:
        result = workflow_complete_consultation(
            token,
            symptoms=request.data.get('symptoms', ''),
            diagnosis=request.data.get('diagnosis', ''),
            notes=request.data.get('notes', ''),
            medicines=request.data.get('medicines', []),
            lab_tests=_normalize_lab_test_names(request.data.get('lab_tests', [])),
            followup_date=request.data.get('followup_date'),
        )
    except ValidationError as exc:
        return Response({'success': False, 'error': str(exc)}, status=400)

    token.refresh_from_db()
    duration = None
    if token.consultation_started_at and token.consultation_ended_at:
        duration = round((token.consultation_ended_at - token.consultation_started_at).total_seconds() / 60, 1)

    return Response({
        'success': True,
        'message': f'Consultation completed for {token.patient_name}',
        'token': serialize_token(token, include_workflow=True),
        'consultation_duration_minutes': duration,
        **result,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def doctor_completed_today(request):
    """Tokens whose consultation finished today for the logged-in doctor."""
    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)

    today = timezone.localdate()
    tokens = Token.objects.filter(
        slot__doctor=profile,
        slot__date=today,
        status__in=['completed', 'pending_lab', 'pending_pharmacy'],
    ).exclude(
        consultation_ended_at__isnull=True,
    ).select_related(
        'consultation', 'slot', 'patient', 'pharmacy_queue_entry',
    ).order_by('-consultation_ended_at')

    completed = []
    for token in tokens:
        consult = getattr(token, 'consultation', None)
        duration = None
        if token.consultation_started_at and token.consultation_ended_at:
            duration = round(
                (token.consultation_ended_at - token.consultation_started_at).total_seconds() / 60,
                1,
            )
        completed.append({
            'token_id': token.id,
            'token_number': token.token_number,
            'patient_id': patient_id_for_token(token),
            'patient_name': token.patient_name,
            'patient_age': token.patient_age,
            'status': token.status,
            'diagnosis': consult.diagnosis if consult else '',
            'symptoms': consult.symptoms if consult else '',
            'completed_at': format_local_time(token.consultation_ended_at, '%I:%M %p'),
            'duration_minutes': duration,
            'requires_lab': consult.requires_lab if consult else False,
            'requires_pharmacy': (
                getattr(token, 'pharmacy_queue_entry', None) is not None
                or token.status == 'pending_pharmacy'
            ),
        })
    return Response({'success': True, 'completed': completed})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def doctor_consultation_detail(request, token_id):
    """Active consultation context for the consultation page."""
    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)

    try:
        token = Token.objects.select_related('slot__doctor', 'patient').get(
            id=token_id,
            slot__doctor=profile,
            status='consulting',
        )
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'No active consultation for this token'}, status=404)

    category = patient_priority_category(token)
    return Response({
        'success': True,
        'token': {
            'token_id': token.id,
            'token_number': token.token_number,
            'patient_id': patient_id_for_token(token),
            'patient_name': token.patient_name,
            'patient_age': token.patient_age,
            'is_followup': token.is_followup,
            'is_elderly': token.is_elderly,
            'is_disabled': token.is_disabled,
            'category': category,
            'started_at': token.consultation_started_at.isoformat() if token.consultation_started_at else None,
        },
        'doctor_name': str(profile),
    })


def _patient_history_filter(token):
    q = Q(patient_phone=token.patient_phone)
    if token.patient_id:
        q |= Q(patient_id=token.patient_id)
    return q


def _patient_tokens_filter(patient_user):
    q = Q(patient=patient_user)
    if patient_user.phone:
        q |= Q(patient_phone=patient_user.phone)
    return q


_HISTORY_STATUSES = [
    'completed', 'pending_lab', 'pending_pharmacy', 'consulting', 'checked_in',
]


def _serialize_token_history_entry(token):
    consult = getattr(token, 'consultation', None)
    prescriptions = [
        {
            'medicine_name': p.medicine_name,
            'dosage': p.dosage,
            'frequency': p.frequency,
            'duration_days': p.duration_days,
            'instructions': p.instructions,
            'dispensed': p.dispensed,
        }
        for p in token.prescriptions.all()
    ]
    lab_reports = []
    for order in token.lab_orders.filter(status='completed'):
        report = getattr(order, 'report', None)
        lab_reports.append({
            'test_name': order.test_name,
            'findings': report.findings if report else '',
            'report_url': report.report_file.url if report and report.report_file else None,
            'uploaded_at': format_local_time(report.uploaded_at, '%d %b %Y') if report else None,
        })
    return {
        'token_id': token.id,
        'token_number': token.token_number,
        'date': token.slot.date.isoformat(),
        'doctor_name': str(token.slot.doctor),
        'status': token.status,
        'is_followup': token.is_followup,
        'consultation': {
            'symptoms': consult.symptoms if consult else '',
            'diagnosis': consult.diagnosis if consult else '',
            'notes': consult.notes if consult else '',
            'followup_date': consult.followup_date.isoformat() if consult and consult.followup_date else None,
        } if consult else None,
        'prescriptions': prescriptions,
        'lab_reports': lab_reports,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def patient_history(request, token_id):
    """Past visits, prescriptions, and lab reports for a patient."""
    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)

    try:
        current = Token.objects.select_related('slot__doctor', 'patient').get(id=token_id)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)

    past_tokens = (
        Token.objects.filter(_patient_history_filter(current))
        .exclude(id=current.id)
        .filter(status__in=_HISTORY_STATUSES)
        .select_related('slot__doctor', 'consultation')
        .prefetch_related('prescriptions', 'lab_orders__report')
        .order_by('-slot__date', '-created_at')[:15]
    )

    history = [_serialize_token_history_entry(token) for token in past_tokens]

    prior_visits = Token.objects.filter(_patient_history_filter(current)).exclude(id=current.id).count()

    return Response({
        'success': True,
        'patient_name': current.patient_name,
        'patient_age': current.patient_age,
        'patient_phone': current.patient_phone,
        'is_followup': current.is_followup,
        'is_returning': prior_visits > 0,
        'is_new_patient': patient_is_new(current.patient),
        'prior_visit_count': prior_visits,
        'history': history,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDoctor])
def doctor_patient_record(request):
    """Full visit history for a patient looked up by Patient ID (e.g. PAT0001)."""
    profile = get_doctor_for_user(request.user)
    if not profile:
        return Response({'success': False, 'error': 'Doctor profile not found'}, status=404)

    patient_id_raw = (request.GET.get('patient_id') or '').strip()
    if not patient_id_raw:
        return Response({'success': False, 'error': 'Patient ID is required'}, status=400)

    patient_user = User.resolve_patient_id(patient_id_raw)
    if not patient_user:
        return Response({'success': False, 'error': 'Patient not found'}, status=404)

    tokens = (
        Token.objects.filter(_patient_tokens_filter(patient_user))
        .filter(status__in=_HISTORY_STATUSES)
        .select_related('slot__doctor', 'consultation')
        .prefetch_related('prescriptions', 'lab_orders__report')
        .order_by('-slot__date', '-created_at')[:50]
    )

    latest = tokens[0] if tokens else None
    history = [_serialize_token_history_entry(token) for token in tokens]
    visit_count = Token.objects.filter(_patient_tokens_filter(patient_user)).count()

    return Response({
        'success': True,
        'patient_id': patient_user.patient_id,
        'patient_name': (
            latest.patient_name if latest
            else patient_user.get_full_name() or patient_user.username
        ),
        'patient_age': latest.patient_age if latest else patient_user.age,
        'patient_phone': latest.patient_phone if latest else patient_user.phone,
        'patient_address': latest.patient_address if latest else patient_user.address,
        'is_new_patient': patient_is_new(patient_user),
        'visit_count': visit_count,
        'history': history,
    })
