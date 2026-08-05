from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from core.models import (
    ConsultationSlot, DailyAnalytics, DoctorProfile, FollowupRule,
    ThrottleLog, Token,
)
from core.permissions import STAFF_ROLES, IsAdmin
from core.services.analytics import (
    compute_kpis,
    ensure_slot_recommendations,
    get_recommendations,
    parse_monthly_date_range,
)
from core.utils import ensure_today_tomorrow_slots, serialize_slot
from core.services.slot_config import (
    ensure_slot_type_configs,
    get_all_slot_configs_serialized,
    get_slot_type_config,
    parse_time_value,
    refresh_consultation_slot_capacities,
    SLOT_TYPES,
)

STAFF_ROLE_LABELS = {
    'admin': 'Administrator',
    'receptionist': 'Receptionist',
    'doctor': 'Doctor',
    'lab_tech': 'Lab Technician',
    'pharmacist': 'Pharmacist',
}


def _doctor_profile_for(user):
    return DoctorProfile.objects.filter(user=user).first()


def _serialize_staff(user):
    data = {
        'id': user.id,
        'username': user.username,
        'full_name': user.get_full_name() or user.first_name or user.username,
        'first_name': user.first_name or '',
        'last_name': user.last_name or '',
        'role': user.role,
        'role_label': STAFF_ROLE_LABELS.get(user.role, user.role),
        'phone': user.phone or '',
        'email': user.email or '',
        'is_active': user.is_active,
        'last_login': user.last_login.strftime('%Y-%m-%d %H:%M') if user.last_login else None,
        'date_joined': user.date_joined.strftime('%Y-%m-%d %H:%M') if user.date_joined else None,
    }
    if user.role == 'doctor':
        profile = _doctor_profile_for(user)
        if profile:
            data['doctor_id'] = profile.id
            data['specialization'] = profile.specialization
            data['qualification'] = profile.qualification or ''
            data['avg_consultation_time'] = profile.avg_consultation_time
            data['is_available'] = profile.is_available
            data['max_queue_size'] = profile.max_queue_size
    return data


def _split_name(full_name):
    parts = (full_name or '').replace('Dr.', '').strip().split()
    if not parts:
        return '', ''
    return parts[0], ' '.join(parts[1:]) if len(parts) > 1 else ''


def _ensure_doctor_profile(user, data=None):
    data = data or {}
    profile, created = DoctorProfile.objects.get_or_create(
        user=user,
        defaults={
            'specialization': data.get('specialization') or 'General',
            'qualification': data.get('qualification') or '',
            'avg_consultation_time': int(data.get('avg_consultation_time') or 10),
            'max_queue_size': int(data.get('max_queue_size') or 5),
            'is_available': True,
        },
    )
    if not created:
        for field in ('specialization', 'qualification', 'avg_consultation_time', 'max_queue_size', 'is_available'):
            if field in data and data[field] is not None and data[field] != '':
                value = data[field]
                if field in ('avg_consultation_time', 'max_queue_size'):
                    value = int(value)
                elif field == 'is_available':
                    value = bool(value) if not isinstance(value, str) else value.lower() in ('1', 'true', 'yes')
                setattr(profile, field, value)
        profile.save()
    return profile


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_staff_list(request):
    """List or create staff accounts (all non-patient roles)."""
    if request.method == 'GET':
        role_filter = (request.query_params.get('role') or '').strip().lower()
        qs = User.objects.filter(role__in=STAFF_ROLES).order_by('role', 'username')
        if role_filter and role_filter in STAFF_ROLES:
            qs = qs.filter(role=role_filter)
        return Response({
            'success': True,
            'staff': [_serialize_staff(u) for u in qs],
            'roles': [
                {'value': key, 'label': label}
                for key, label in STAFF_ROLE_LABELS.items()
            ],
        })

    username = (request.data.get('username') or '').strip()
    password = request.data.get('password') or ''
    role = (request.data.get('role') or '').strip().lower()
    full_name = (request.data.get('full_name') or request.data.get('name') or '').strip()
    phone = (request.data.get('phone') or '').strip()
    email = (request.data.get('email') or '').strip()

    if not username or not password:
        return Response({'success': False, 'error': 'Username and password are required'}, status=400)
    if role not in STAFF_ROLES:
        return Response({'success': False, 'error': 'Invalid staff role'}, status=400)
    if User.objects.filter(username__iexact=username).exists():
        return Response({'success': False, 'error': 'Username already exists'}, status=400)
    if len(password) < 4:
        return Response({'success': False, 'error': 'Password must be at least 4 characters'}, status=400)

    first_name, last_name = _split_name(full_name)
    if request.data.get('first_name'):
        first_name = request.data.get('first_name').strip()
    if request.data.get('last_name') is not None:
        last_name = (request.data.get('last_name') or '').strip()

    with transaction.atomic():
        user = User.objects.create(
            username=username,
            password=make_password(password),
            role=role,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email=email,
            is_active=True,
        )
        if role == 'doctor':
            _ensure_doctor_profile(user, request.data)
            ensure_today_tomorrow_slots()

    return Response({
        'success': True,
        'message': f'{STAFF_ROLE_LABELS.get(role, role)} account created',
        'staff': _serialize_staff(user),
    }, status=201)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_staff_detail(request, user_id):
    """Get, update, or remove a staff account."""
    try:
        user = User.objects.get(id=user_id, role__in=STAFF_ROLES)
    except User.DoesNotExist:
        return Response({'success': False, 'error': 'Staff account not found'}, status=404)

    if request.method == 'DELETE':
        if user.id == request.user.id:
            return Response({
                'success': False,
                'error': 'You cannot remove your own account',
            }, status=400)

        if user.role == 'admin':
            other_admins = User.objects.filter(
                role='admin', is_active=True,
            ).exclude(id=user.id).count()
            if other_admins == 0:
                return Response({
                    'success': False,
                    'error': 'Cannot remove the only active administrator',
                }, status=400)

        if user.role == 'doctor':
            profile = _doctor_profile_for(user)
            if profile and Token.objects.filter(slot__doctor=profile).exists():
                return Response({
                    'success': False,
                    'error': (
                        'This doctor has consultation history. '
                        'Deactivate the account instead of removing it.'
                    ),
                }, status=400)

        name = user.get_full_name() or user.username
        user.delete()
        return Response({
            'success': True,
            'message': f'Staff account "{name}" removed',
        })

    if request.method == 'GET':
        return Response({'success': True, 'staff': _serialize_staff(user)})

    data = request.data
    new_username = (data.get('username') or '').strip()
    new_role = (data.get('role') or '').strip().lower()
    new_password = data.get('password')
    full_name = data.get('full_name') if 'full_name' in data else data.get('name')

    # Protect the currently logged-in admin from locking themselves out
    if user.id == request.user.id:
        if 'is_active' in data and not bool(data.get('is_active')):
            return Response({
                'success': False,
                'error': 'You cannot deactivate your own account',
            }, status=400)
        if new_role and new_role != 'admin':
            return Response({
                'success': False,
                'error': 'You cannot change your own role away from admin',
            }, status=400)

    if new_role and new_role not in STAFF_ROLES:
        return Response({'success': False, 'error': 'Invalid staff role'}, status=400)

    if new_username and new_username.lower() != user.username.lower():
        if User.objects.filter(username__iexact=new_username).exclude(id=user.id).exists():
            return Response({'success': False, 'error': 'Username already exists'}, status=400)
        user.username = new_username

    if new_password not in (None, ''):
        if len(str(new_password)) < 4:
            return Response({'success': False, 'error': 'Password must be at least 4 characters'}, status=400)
        user.password = make_password(new_password)

    if full_name is not None:
        first_name, last_name = _split_name(full_name)
        user.first_name = first_name
        user.last_name = last_name
    if 'first_name' in data:
        user.first_name = (data.get('first_name') or '').strip()
    if 'last_name' in data:
        user.last_name = (data.get('last_name') or '').strip()
    if 'phone' in data:
        user.phone = (data.get('phone') or '').strip()
    if 'email' in data:
        user.email = (data.get('email') or '').strip()
    if 'is_active' in data:
        raw = data.get('is_active')
        user.is_active = bool(raw) if not isinstance(raw, str) else raw.lower() in ('1', 'true', 'yes')

    old_role = user.role
    if new_role:
        user.role = new_role

    with transaction.atomic():
        user.save()
        if user.role == 'doctor':
            _ensure_doctor_profile(user, data)
            ensure_today_tomorrow_slots()
        elif old_role == 'doctor' and user.role != 'doctor':
            # Keep DoctorProfile row for history, but mark unavailable
            profile = _doctor_profile_for(user)
            if profile:
                profile.is_available = False
                profile.save(update_fields=['is_available'])

    return Response({
        'success': True,
        'message': 'Staff account updated',
        'staff': _serialize_staff(user),
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_doctors(request):
    doctors = DoctorProfile.objects.select_related('user').all()
    data = []
    for doc in doctors:
        data.append({
            'id': doc.id,
            'user_id': doc.user_id,
            'name': str(doc),
            'username': doc.user.username,
            'specialization': doc.specialization,
            'qualification': doc.qualification,
            'avg_consultation_time': doc.avg_consultation_time,
            'is_available': doc.is_available,
            'max_queue_size': doc.max_queue_size,
            'is_throttled': doc.is_throttled,
            'is_active': doc.user.is_active,
            'phone': doc.user.phone or '',
        })
    return Response({'success': True, 'doctors': data})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_add_doctor(request):
    full_name = request.data.get('full_name') or request.data.get('name')
    username = request.data.get('username')
    password = request.data.get('password', 'doctor123')
    specialization = request.data.get('specialization', 'General')
    qualification = request.data.get('qualification', '')
    avg_consultation_time = int(request.data.get('avg_consultation_time', 10))

    if not full_name or not username:
        return Response({'success': False, 'error': 'Name and username required'}, status=400)
    if User.objects.filter(username=username).exists():
        return Response({'success': False, 'error': 'Username already exists'}, status=400)

    parts = full_name.replace('Dr.', '').strip().split()
    user = User.objects.create(
        username=username,
        password=make_password(password),
        role='doctor',
        first_name=parts[0] if parts else full_name,
        last_name=' '.join(parts[1:]) if len(parts) > 1 else '',
    )
    profile = DoctorProfile.objects.create(
        user=user,
        specialization=specialization,
        qualification=qualification,
        avg_consultation_time=avg_consultation_time,
    )
    ensure_today_tomorrow_slots()
    return Response({
        'success': True,
        'doctor': {'id': profile.id, 'name': str(profile), 'username': username},
    })


@api_view(['PUT'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_update_doctor(request, doctor_id):
    try:
        profile = DoctorProfile.objects.get(id=doctor_id)
    except DoctorProfile.DoesNotExist:
        return Response({'success': False, 'error': 'Doctor not found'}, status=404)

    for field in ('specialization', 'qualification', 'avg_consultation_time', 'is_available', 'max_queue_size'):
        if field in request.data:
            setattr(profile, field, request.data[field])
    profile.save()
    ensure_today_tomorrow_slots()
    return Response({'success': True, 'doctor_id': profile.id})


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_slot_config(request):
    ensure_slot_type_configs()
    if request.method == 'GET':
        slots = get_all_slot_configs_serialized()
        return Response({
            'success': True,
            'slots': slots,
        })

    slots_payload = request.data.get('slots')
    if not isinstance(slots_payload, dict):
        return Response({'success': False, 'error': 'slots object required'}, status=400)

    for slot_type in SLOT_TYPES:
        payload = slots_payload.get(slot_type)
        if not payload:
            continue
        config = get_slot_type_config(slot_type)
        if 'start_time' in payload:
            config.start_time = parse_time_value(payload['start_time'])
        if 'end_time' in payload:
            config.end_time = parse_time_value(payload['end_time'])
        if 'avg_consultation_minutes' in payload:
            config.avg_consultation_minutes = max(int(payload['avg_consultation_minutes']), 1)
        if 'checkin_opens_minutes_before' in payload:
            config.checkin_opens_minutes_before = max(int(payload['checkin_opens_minutes_before']), 0)
        if 'assigned_doctor_id' in payload:
            doc_id = payload.get('assigned_doctor_id')
            if doc_id:
                try:
                    config.assigned_doctor = DoctorProfile.objects.get(
                        id=int(doc_id), is_available=True,
                    )
                except (DoctorProfile.DoesNotExist, TypeError, ValueError):
                    return Response({
                        'success': False,
                        'error': f'Invalid doctor for {slot_type} slot',
                    }, status=400)
            else:
                config.assigned_doctor = None
        if config.end_time <= config.start_time:
            return Response({
                'success': False,
                'error': f'{slot_type.title()} end time must be after start time',
            }, status=400)
        config.save()

    refresh_consultation_slot_capacities()
    ensure_today_tomorrow_slots()
    return Response({
        'success': True,
        'slots': get_all_slot_configs_serialized(),
    })


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_throttle_config(request):
    from core import constants as C
    if request.method == 'GET':
        doc = DoctorProfile.objects.first()
        return Response({
            'success': True,
            'enabled': C.AUTO_THROTTLE_ENABLED,
            'max_queue_size': doc.max_queue_size if doc else 5,
            'is_throttled': False,
        })

    if not C.AUTO_THROTTLE_ENABLED:
        return Response({
            'success': False,
            'error': 'Auto-throttle is disabled system-wide.',
        }, status=400)

    max_queue = int(request.data.get('max_queue_size', 5))
    DoctorProfile.objects.update(max_queue_size=max_queue)
    for doc in DoctorProfile.objects.all():
        doc.check_throttle()
    return Response({'success': True, 'max_queue_size': max_queue})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def admin_throttle_logs(request):
    logs = ThrottleLog.objects.select_related('slot__doctor').order_by('-triggered_at')[:50]
    return Response({
        'success': True,
        'logs': [{
            'action': log.action,
            'doctor': str(log.slot.doctor),
            'queue_length': log.queue_length_at_trigger,
            'threshold': log.threshold_at_trigger,
            'triggered_at': log.triggered_at.strftime('%Y-%m-%d %H:%M'),
        } for log in logs],
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def analytics(request):
    ensure_slot_recommendations()
    monthly_start, monthly_end, range_error = parse_monthly_date_range(
        request.query_params.get('monthly_start'),
        request.query_params.get('monthly_end'),
    )
    if range_error:
        return Response({'success': False, 'error': range_error}, status=400)
    kpis = compute_kpis(monthly_start=monthly_start, monthly_end=monthly_end)
    recommendations = [
        {
            'id': r.id,
            'doctor': str(r.doctor),
            'configured_avg_minutes': r.configured_avg_minutes,
            'actual_avg_minutes': float(r.actual_avg_minutes),
            'variance_percent': float(r.variance_percent),
            'recommended_avg_minutes': r.recommended_avg_minutes,
            'message': r.message,
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M'),
        }
        for r in get_recommendations()
    ]
    return Response({
        'success': True,
        **kpis,
        'recommendations': recommendations,
        'throttle_events': ThrottleLog.objects.filter(
            triggered_at__date=timezone.localdate(), action='throttled'
        ).count(),
        'updated_at': timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
    })
