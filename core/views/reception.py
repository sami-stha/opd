from django.core.exceptions import ValidationError
from django.db import transaction
from django.db import models
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.db.models.functions import Concat
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import PatientSerial, User
from core import constants as C
from core.models import DoctorProfile, LabOrder, Token
from core.permissions import IsReceptionist, IsReceptionistOrAdmin
from core.services.lab_orders import group_pending_lab_payments, normalize_pending_lab_order_names, repair_corrupt_lab_orders
from core.services.lab_payments import LabPaymentError, pay_lab_order, pay_lab_orders_for_token
from core.services.sms import sms_patient_registration
from core.utils import is_elderly_by_age, patient_id_for_token, resolve_disabled_flag, serialize_token, format_patient_gender


def _normalize_gender(value):
    if not value:
        return ''
    raw = str(value).strip().lower()
    if raw in ('male', 'm'):
        return 'male'
    if raw in ('female', 'f'):
        return 'female'
    if raw in ('other', 'o'):
        return 'other'
    return ''


def _order_tokens_by_slot(queryset):
    slot_order = Case(
        When(slot__slot_type='morning', then=0),
        When(slot__slot_type='afternoon', then=1),
        When(slot__slot_type='evening', then=2),
        default=3,
        output_field=IntegerField(),
    )
    return queryset.annotate(slot_order=slot_order).order_by(
        'slot__date', 'slot_order', 'estimated_time',
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def search_patient(request):
    from datetime import timedelta

    search_term = (request.query_params.get('q') or '').strip()
    if not search_term:
        return Response({'success': False, 'error': 'Search term required'}, status=400)

    today = timezone.localdate()
    scope = (request.query_params.get('scope') or '').strip().lower()

    tokens = Token.objects.filter(
        models.Q(token_number__iexact=search_term) |
        models.Q(token_number__icontains=search_term) |
        models.Q(patient_phone__icontains=search_term) |
        models.Q(patient_name__icontains=search_term)
    ).select_related('slot__doctor__user', 'patient')

    if scope == 'checkin':
        # Check-in desk only deals with today's visits — never surface tomorrow's
        # token when the same number (e.g. M3) is also booked for today.
        tokens = tokens.filter(slot__date=today).annotate(
            exact_token=Case(
                When(token_number__iexact=search_term, then=0),
                default=1,
                output_field=IntegerField(),
            ),
        ).order_by('exact_token', 'estimated_time')[:20]
        results = [serialize_token(t) for t in tokens]
        return Response({'success': True, 'count': len(results), 'patients': results})

    tokens = tokens.annotate(
        day_rank=Case(
            When(slot__date=today, then=0),
            When(slot__date=today + timedelta(days=1), then=1),
            default=2,
            output_field=IntegerField(),
        ),
        exact_token=Case(
            When(token_number__iexact=search_term, then=0),
            default=1,
            output_field=IntegerField(),
        ),
    ).order_by('day_rank', 'exact_token', 'estimated_time')[:20]

    results = [serialize_token(t) for t in tokens]
    linked_user_ids = {t.patient_id for t in tokens if t.patient_id}
    patient_qs = _filter_patients_by_search(User.objects.filter(role='patient'), search_term)
    for user in patient_qs[:10]:
        if user.id in linked_user_ids:
            continue
        results.append(_serialize_registered_patient_search(user))

    return Response({'success': True, 'count': len(results), 'patients': results})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def check_in_patient(request, token_id):
    try:
        token = Token.objects.select_related('slot__doctor').get(id=token_id)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)

    is_disabled = request.data.get('is_disabled')
    token.is_elderly = is_elderly_by_age(token.patient_age)
    token.is_disabled = resolve_disabled_flag(
        is_disabled if 'is_disabled' in request.data else None,
        patient_user=token.patient,
        token=token,
    )
    token.save(update_fields=['is_elderly', 'is_disabled'])

    try:
        token.check_in(receptionist=request.user)
    except ValidationError as exc:
        return Response({'success': False, 'error': str(exc)}, status=400)

    return Response({
        'success': True,
        'message': f'Patient {token.patient_name} checked in successfully',
        'token': serialize_token(token, include_queue=True),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def register_walkin_patient(request):
    """Register a new patient at reception and optionally link to a token."""
    full_name = request.data.get('full_name') or request.data.get('name')
    phone = request.data.get('phone')
    age = request.data.get('age')
    address = request.data.get('address', '')
    token_id = request.data.get('token_id')
    is_disabled = request.data.get('is_disabled')
    gender = _normalize_gender(request.data.get('gender'))

    if not all([full_name, phone, age]):
        return Response({'success': False, 'error': 'Name, phone, and age required'}, status=400)

    PatientSerial.sync_from_database()

    user, created = User.objects.get_or_create(
        phone=phone,
        role='patient',
        defaults={
            'username': f'pat_{phone}',
            'first_name': full_name.split()[0],
            'last_name': ' '.join(full_name.split()[1:]) if ' ' in full_name else '',
            'age': int(age),
            'address': address,
            'is_disabled': bool(is_disabled) if is_disabled is not None else False,
            'gender': gender,
        },
    )
    sms_result = None
    if not created:
        user.first_name = full_name.split()[0]
        user.last_name = ' '.join(full_name.split()[1:]) if ' ' in full_name else ''
        user.age = int(age)
        user.address = address
        if is_disabled is not None:
            user.is_disabled = bool(is_disabled)
        if gender:
            user.gender = gender
        if not user.patient_code:
            user.assign_patient_code()
        user.save()
    else:
        user.set_unusable_password()
        if not user.patient_code:
            user.assign_patient_code()
        user.save(update_fields=['password', 'patient_code'])
        sms_result = sms_patient_registration(user.patient_id, phone)

    if token_id:
        try:
            token = Token.objects.get(id=token_id)
            token.patient = user
            token.patient_name = full_name
            token.patient_age = int(age)
            token.patient_phone = phone
            token.patient_address = address
            token.is_disabled = resolve_disabled_flag(
                bool(is_disabled) if is_disabled is not None else None,
                patient_user=user,
                token=token,
            )
            token.save()
        except Token.DoesNotExist:
            pass

    response_data = {
        'success': True,
        'patient': {
            'id': user.id,
            'patient_id': user.patient_id,
            'name': full_name,
            'phone': phone,
            'age': user.age,
        },
        'created': created,
    }
    if sms_result is not None:
        response_data['sms_sent'] = sms_result.success
        if not sms_result.success:
            response_data['sms_warning'] = sms_result.error
    return Response(response_data)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def reception_appointments(request):
    today = timezone.localdate()
    from datetime import timedelta
    tomorrow = today + timedelta(days=1)
    day_filter = request.query_params.get('day', 'today')
    view_filter = request.query_params.get('view', 'all')

    tokens = _order_tokens_by_slot(Token.objects.select_related(
        'slot__doctor__user', 'patient',
    ))

    if day_filter == 'all':
        tokens = tokens.filter(slot__date__in=[today, tomorrow])
    elif day_filter == 'tomorrow':
        tokens = tokens.filter(slot__date=tomorrow)
    else:
        tokens = tokens.filter(slot__date=today)

    if view_filter == 'active':
        tokens = tokens.filter(status__in=C.ACTIVE_STATUSES)
    elif view_filter == 'completed':
        tokens = tokens.filter(status=C.COMPLETED)
    elif view_filter == 'expired':
        tokens = tokens.filter(status__in=[C.EXPIRED, C.CANCELLED])

    serialized = [serialize_token(t, include_queue=True, include_workflow=True) for t in tokens]
    active = [a for a in serialized if a.get('is_active')]
    completed = [a for a in serialized if a['status'] == C.COMPLETED]
    expired = [a for a in serialized if a['status'] in (C.EXPIRED, C.CANCELLED)]

    return Response({
        'success': True,
        'appointments': serialized,
        'active': active,
        'completed': completed,
        'expired': expired,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def reception_tokens_booked(request):
    """All token bookings from today onward, grouped by appointment date."""
    from datetime import timedelta
    from core.services.workflow import expire_all_ended_slots

    expire_all_ended_slots()

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)

    tokens = _order_tokens_by_slot(Token.objects.filter(
        slot__date__gte=today,
    ).exclude(
        status=C.CANCELLED,
    ).select_related(
        'slot__doctor__user', 'patient',
    ))

    serialized = [serialize_token(t, include_queue=True, include_workflow=True) for t in tokens]

    grouped_map = {}
    for apt in serialized:
        grouped_map.setdefault(apt['date'], []).append(apt)

    def date_label(iso_date):
        if iso_date == today.isoformat():
            return 'Today'
        if iso_date == tomorrow.isoformat():
            return 'Tomorrow'
        from datetime import date as date_cls
        return date_cls.fromisoformat(iso_date).strftime('%A, %d %b %Y')

    groups = [
        {
            'date': iso_date,
            'date_label': date_label(iso_date),
            'count': len(items),
            'appointments': items,
        }
        for iso_date, items in grouped_map.items()
    ]

    return Response({
        'success': True,
        'count': len(serialized),
        'groups': groups,
        'appointments': serialized,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def reception_lab_payments(request):
    repair_corrupt_lab_orders()
    normalize_pending_lab_order_names()
    orders = LabOrder.objects.filter(
        status__in=['ordered', 'fee_pending']
    ).select_related('token', 'token__patient', 'consultation').order_by('ordered_at')
    return Response({
        'success': True,
        'lab_payments': group_pending_lab_payments(orders),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def pay_lab_fees_for_token(request, token_id):
    """Collect lab fees for all pending orders on a token, then send to lab queue."""
    repair_corrupt_lab_orders(token_id)
    try:
        orders, payments, entries, total = pay_lab_orders_for_token(
            token_id,
            request.user,
            request.data.get('reference_number'),
        )
    except LabPaymentError as exc:
        return Response({'success': False, 'error': exc.message}, status=exc.status_code)

    return Response({
        'success': True,
        'message': 'Lab fee paid — patient sent to lab queue',
        'token_id': token_id,
        'orders_paid': len(orders),
        'total_amount': float(total),
        'queue_entry_ids': [e.id for e in entries],
        'payment_ids': [p.id for p in payments],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def pay_lab_fee(request, order_id):
    try:
        order, payment, entry = pay_lab_order(
            order_id,
            request.data.get('amount', None),
            request.user,
            request.data.get('reference_number'),
        )
    except LabPaymentError as exc:
        return Response({'success': False, 'error': exc.message}, status=exc.status_code)

    return Response({
        'success': True,
        'message': 'Lab fee paid — patient sent to lab queue',
        'order_id': order.id,
        'status': order.status,
        'queue_entry_id': entry.id,
        'payment_id': payment.id,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def throttle_status(request):
    from core import constants as C
    doctors = DoctorProfile.objects.select_related('user').all()
    data = []
    for doc in doctors:
        data.append({
            'doctor_id': doc.id,
            'doctor_name': str(doc),
            'is_throttled': False if not C.AUTO_THROTTLE_ENABLED else doc.is_throttled,
            'max_queue_size': doc.max_queue_size,
        })
    return Response({'success': True, 'doctors': data, 'enabled': C.AUTO_THROTTLE_ENABLED})


def _registered_patients_queryset():
    """All patient accounts in the database (includes demo seed patients)."""
    return User.objects.filter(role='patient')


def _order_patients_by_serial(patients):
    patients = list(patients)

    def sort_key(user):
        code = (user.patient_code or '').strip()
        if code:
            return (0, PatientSerial.code_sort_key(code), user.id)
        return (1, user.date_joined or timezone.now(), user.id)

    patients.sort(key=sort_key)
    return patients


def _serialize_patients_list(queryset, limit=None):
    patients = _order_patients_by_serial(queryset)
    total = len(patients)
    if limit:
        patients = patients[:limit]
    return [_serialize_reception_patient(p, include_stats=True) for p in patients], total


def _serialize_reception_patient(user, *, include_stats=False):
    data = {
        'id': user.id,
        'patient_id': user.patient_id,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'full_name': user.get_full_name() or user.first_name,
        'phone': user.phone,
        'age': user.age,
        'address': user.address or '',
        'email': user.email or '',
        'is_disabled': bool(getattr(user, 'is_disabled', False)),
        'gender': format_patient_gender(getattr(user, 'gender', '')),
        'registered_at': user.date_joined.strftime('%Y-%m-%d %H:%M') if user.date_joined else None,
    }
    if include_stats:
        data['visit_count'] = getattr(user, 'visit_count', user.tokens.count())
    return data


def _get_patient_user(user_id):
    try:
        return User.objects.get(id=user_id, role='patient')
    except User.DoesNotExist:
        return None


def _filter_patients_by_search(queryset, search):
    """Match patient ID, phone, or name (case-insensitive, including full name)."""
    term = (search or '').strip()
    if not term:
        return queryset

    phone_digits = ''.join(ch for ch in term if ch.isdigit())
    patient_id_term = term.upper().replace(' ', '')

    filters = (
        Q(first_name__icontains=term)
        | Q(last_name__icontains=term)
        | Q(_full_name__icontains=term)
        | Q(patient_code__icontains=patient_id_term)
        | Q(phone__icontains=term)
    )
    if phone_digits:
        filters |= Q(phone__icontains=phone_digits)

    resolved = User.resolve_patient_id(term)
    if resolved:
        filters |= Q(pk=resolved.pk)

    return queryset.filter(filters).distinct()


def _serialize_registered_patient_search(user):
    return {
        'type': 'registered_patient',
        'token_id': None,
        'token_number': None,
        'patient_id': user.patient_id,
        'patient_name': user.get_full_name() or user.first_name,
        'patient_phone': user.phone,
        'patient_age': user.age,
        'patient_address': user.address or '',
        'status': 'registered',
        'display_status': 'Registered (no appointment)',
        'is_registered_only': True,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def reception_unbooked_patients(request):
    """Registered patients (with Patient ID) who have no token booked for today."""
    today = timezone.localdate()
    patients = _registered_patients_queryset().annotate(
        today_tokens=Count('tokens', filter=Q(tokens__slot__date=today)),
        visit_count=Count('tokens'),
        _full_name=Concat('first_name', Value(' '), 'last_name'),
    ).filter(today_tokens=0)
    serialized, total = _serialize_patients_list(patients, limit=100)
    return Response({
        'success': True,
        'count': len(serialized),
        'total_count': total,
        'patients': serialized,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def reception_patients(request):
    """All registered patients with serial Patient IDs (same registry as dashboard)."""
    search = request.query_params.get('q', '').strip()
    patients = _registered_patients_queryset().annotate(
        visit_count=Count('tokens'),
        _full_name=Concat('first_name', Value(' '), 'last_name'),
    )

    if search:
        patients = _filter_patients_by_search(patients, search)

    serialized, total = _serialize_patients_list(patients)
    return Response({
        'success': True,
        'count': len(serialized),
        'total_count': total,
        'query': search,
        'patients': serialized,
    })


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated, IsReceptionistOrAdmin])
def reception_patient_detail(request, user_id):
    user = _get_patient_user(user_id)
    if not user:
        return Response({'success': False, 'error': 'Patient not found'}, status=404)

    if request.method == 'GET':
        return Response({
            'success': True,
            'patient': _serialize_reception_patient(user, include_stats=True),
        })

    first_name = (request.data.get('first_name') or '').strip()
    last_name = (request.data.get('last_name') or '').strip()
    phone = (request.data.get('phone') or '').strip()
    address = (request.data.get('address') or '').strip()
    age = request.data.get('age')

    if not first_name:
        return Response({'success': False, 'error': 'First name is required'}, status=400)
    if not phone:
        return Response({'success': False, 'error': 'Phone number is required'}, status=400)

    try:
        age = int(age)
    except (TypeError, ValueError):
        return Response({'success': False, 'error': 'Valid age is required'}, status=400)

    if age < 1 or age > 120:
        return Response({'success': False, 'error': 'Age must be between 1 and 120'}, status=400)

    if User.objects.filter(role='patient', phone=phone).exclude(pk=user.pk).exists():
        return Response({'success': False, 'error': 'Phone number already used by another patient'}, status=400)

    user.first_name = first_name
    user.last_name = last_name
    user.phone = phone
    user.age = age
    user.address = address
    if 'email' in request.data:
        user.email = (request.data.get('email') or '').strip()
    if 'is_disabled' in request.data:
        user.is_disabled = bool(request.data.get('is_disabled'))
    if 'gender' in request.data:
        user.gender = _normalize_gender(request.data.get('gender'))
    user.save()

    full_name = user.get_full_name() or first_name
    Token.objects.filter(patient=user).update(
        patient_name=full_name,
        patient_phone=phone,
        patient_age=age,
        patient_address=address,
    )

    return Response({
        'success': True,
        'message': 'Patient details updated',
        'patient': _serialize_reception_patient(user, include_stats=True),
    })
