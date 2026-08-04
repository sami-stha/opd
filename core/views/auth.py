from django.contrib.auth import authenticate
from django.contrib.auth.hashers import make_password
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from accounts.services.auth_tokens import issue_api_token, revoke_request_token
from core.permissions import STAFF_ROLES, IsPatient, IsStaff
from core.services.otp import is_otp_verified, verify_otp
from core.services.sms import sms_patient_registration
from core.utils import get_doctor_for_user, patient_has_portal_login


def _user_payload(user):
    data = {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or user.first_name or user.username,
        'role': user.role,
        'phone': user.phone,
    }
    if user.role == 'patient':
        data['patient_id'] = user.patient_id
    if user.role == 'doctor':
        profile = get_doctor_for_user(user)
        if profile:
            data['doctor_id'] = profile.id
            data['specialization'] = profile.specialization
    return data


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_register(request):
    full_name = request.data.get('full_name') or request.data.get('name')
    phone = request.data.get('phone')
    password = request.data.get('password')
    age = request.data.get('age')
    address = request.data.get('address', '')
    otp = request.data.get('otp')
    patient_id = request.data.get('patient_id')
    gender_raw = request.data.get('gender')

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

    gender = _normalize_gender(gender_raw)

    if not all([phone, password]):
        return Response({'success': False, 'error': 'Phone and password required'}, status=400)

    if not is_otp_verified(phone, 'registration') and otp:
        result = verify_otp(phone, otp, 'registration')
        if not result.get('success'):
            return Response({'success': False, 'error': result.get('error', 'OTP verification required')}, status=400)
    elif not is_otp_verified(phone, 'registration'):
        return Response({'success': False, 'error': 'OTP verification required before registration'}, status=400)

    # Activate online account for existing walk-in patient (Patient ID + phone)
    if patient_id:
        existing = User.resolve_patient_id(patient_id)
        if not existing:
            return Response({'success': False, 'error': 'Patient ID not found'}, status=404)
        if existing.phone != phone:
            return Response({'success': False, 'error': 'Phone number does not match patient record'}, status=400)
        if patient_has_portal_login(existing):
            return Response({
                'success': False,
                'error': 'This patient already has an online account. Please use Old Patient login.',
                'already_registered': True,
            }, status=400)
        existing.password = make_password(password)
        if full_name:
            existing.first_name = full_name.split()[0] if ' ' in full_name else full_name
            existing.last_name = ' '.join(full_name.split()[1:]) if ' ' in full_name else ''
        if age:
            existing.age = int(age)
        if address:
            existing.address = address
        if gender:
            existing.gender = gender
        existing.save()
        api_token = issue_api_token(existing)
        return Response({
            'success': True,
            'message': 'Account activated successfully!',
            'token': api_token.key,
            'patient': _user_payload(existing),
        })

    if not full_name:
        return Response({'success': False, 'error': 'Full name required for new registration'}, status=400)

    existing_by_phone = User.objects.filter(phone=phone, role='patient').first()
    if existing_by_phone:
        if patient_has_portal_login(existing_by_phone):
            return Response({
                'success': False,
                'error': 'This phone number already has an online account. Please use Old Patient login.',
                'already_registered': True,
            }, status=400)
        if existing_by_phone.patient_code:
            return Response({
                'success': False,
                'error': 'This phone number belongs to a registered patient. Use Patient ID to activate your account.',
                'already_registered': True,
            }, status=400)

    username = f'pat_{phone}'
    if User.objects.filter(username=username).exists():
        username = f'pat_{phone}_{User.objects.count()}'

    user = User(
        username=username,
        phone=phone,
        password=make_password(password),
        role='patient',
        first_name=full_name.split()[0] if ' ' in full_name else full_name,
        last_name=' '.join(full_name.split()[1:]) if ' ' in full_name else '',
        age=int(age) if age else None,
        address=address,
        gender=gender,
    )
    user.assign_patient_code()
    user.save()
    sms_result = sms_patient_registration(user.patient_id, phone)

    payload = {
        'success': True,
        'message': 'Registration successful! Please login.',
        'patient': {
            'id': user.id,
            'patient_id': user.patient_id,
            'name': full_name,
            'phone': phone,
        },
        'sms_sent': sms_result.success,
    }
    if not sms_result.success:
        payload['sms_warning'] = sms_result.error
    return Response(payload)


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_login(request):
    phone = request.data.get('phone')
    password = request.data.get('password')
    patient_id = request.data.get('patient_id')

    user = None
    if patient_id:
        user = User.resolve_patient_id(patient_id)
    elif phone:
        try:
            user = User.objects.get(phone=phone, role='patient')
        except User.DoesNotExist:
            user = None

    if not user or not password or not user.check_password(password):
        return Response({'success': False, 'error': 'Invalid credentials'}, status=401)

    api_token = issue_api_token(user)
    return Response({
        'success': True,
        'message': 'Welcome back!',
        'token': api_token.key,
        'patient': _user_payload(user),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def patient_logout(request):
    revoke_request_token(request)
    return Response({'success': True, 'message': 'Logged out'})


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsPatient])
def get_current_patient(request):
    return Response({'success': True, 'patient': _user_payload(request.user)})


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_login_otp(request):
    """OTP-based patient login using registered phone number."""
    phone = request.data.get('phone', '').strip()
    otp = request.data.get('otp', '').strip()

    if not phone or not otp:
        return Response({'success': False, 'error': 'Phone and OTP required'}, status=400)

    result = verify_otp(phone, otp, 'login')
    if not result.get('success'):
        return Response({'success': False, 'error': result.get('error', 'Invalid OTP')}, status=401)

    try:
        user = User.objects.get(phone=phone, role='patient')
    except User.DoesNotExist:
        return Response({'success': False, 'error': 'No patient account for this phone number'}, status=404)

    api_token = issue_api_token(user)
    return Response({
        'success': True,
        'message': 'Welcome back!',
        'token': api_token.key,
        'patient': _user_payload(user),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def patient_reset_password(request):
    phone = request.data.get('phone')
    password = request.data.get('password')
    otp = request.data.get('otp')
    if not phone or not password:
        return Response({'success': False, 'error': 'Phone and new password required'}, status=400)

    if not is_otp_verified(phone, 'password_reset') and otp:
        result = verify_otp(phone, otp, 'password_reset')
        if not result.get('success'):
            return Response({'success': False, 'error': result.get('error', 'OTP verification required')}, status=400)
    elif not is_otp_verified(phone, 'password_reset'):
        return Response({'success': False, 'error': 'OTP verification required before password reset'}, status=400)

    try:
        user = User.objects.get(phone=phone, role='patient')
    except User.DoesNotExist:
        return Response({'success': False, 'error': 'Phone number not found'}, status=404)
    user.password = make_password(password)
    user.save(update_fields=['password'])
    return Response({'success': True, 'message': 'Password reset successful'})


@api_view(['POST'])
@permission_classes([AllowAny])
def staff_login(request):
    username = request.data.get('username')
    password = request.data.get('password')
    expected_role = request.data.get('role')

    user = authenticate(request, username=username, password=password)
    if not user:
        return Response({'success': False, 'error': 'Invalid credentials'}, status=401)
    if user.role not in STAFF_ROLES:
        return Response({'success': False, 'error': 'Not a staff account'}, status=403)
    if expected_role and user.role != expected_role:
        return Response({'success': False, 'error': f'Account is not a {expected_role}'}, status=403)

    api_token = issue_api_token(user)
    return Response({'success': True, 'user': _user_payload(user), 'token': api_token.key})


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsStaff])
def staff_logout(request):
    revoke_request_token(request)
    return Response({'success': True, 'message': 'Logged out'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def auth_me(request):
    return Response({'success': True, 'user': _user_payload(request.user)})
