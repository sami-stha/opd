from datetime import timedelta

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from core.models import Token
from core.permissions import IsPatient
from core.services.booking_service import BookingError, resolve_booking_patient
from core.views.esewa_payments import create_esewa_booking_session, _gateway_response
from core.utils import ensure_today_tomorrow_slots


@api_view(['GET'])
@permission_classes([AllowAny])
def available_slots(request):
    from core.services.workflow import expire_all_ended_slots

    expire_all_ended_slots()
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    date_filter = request.query_params.get('date')

    dates = [today, tomorrow]
    if date_filter == 'today':
        dates = [today]
    elif date_filter == 'tomorrow':
        dates = [tomorrow]

    from core.utils import get_daily_slots_for_dates, serialize_slot

    slots = get_daily_slots_for_dates(dates)

    available = []
    grouped = {'today': [], 'tomorrow': []}
    for slot in slots:
        serialized = serialize_slot(slot)
        available.append(serialized)
        key = 'today' if slot.date == today else 'tomorrow'
        grouped[key].append(serialized)

    return Response({
        'success': True,
        'today': today.isoformat(),
        'tomorrow': tomorrow.isoformat(),
        'count': len(available),
        'slots': available,
        'grouped': grouped,
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def book_token(request):
    """Token booking — online payment via eSewa redirect only."""
    from core.services.workflow import expire_all_ended_slots

    expire_all_ended_slots()
    ensure_today_tomorrow_slots()

    try:
        session = create_esewa_booking_session(request)
    except BookingError as exc:
        payload = {'success': False, 'error': exc.message}
        if exc.extra:
            payload.update(exc.extra)
        return Response(payload, status=exc.status_code)
    return _gateway_response(session)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsPatient])
def cancel_token(request, token_id):
    try:
        token = Token.objects.get(id=token_id, patient_phone=request.user.phone)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)
    try:
        token.cancel()
    except Exception as exc:
        return Response({'success': False, 'error': str(exc)}, status=400)
    return Response({'success': True, 'message': f'Token {token.token_number} cancelled'})


@api_view(['POST'])
@permission_classes([AllowAny])
def cancel_token_public(request, token_id):
    try:
        token = Token.objects.get(id=token_id)
    except Token.DoesNotExist:
        return Response({'success': False, 'error': 'Token not found'}, status=404)
    if token.status != 'booked':
        return Response({'success': False, 'error': f'Cannot cancel. Status: {token.status}'}, status=400)
    token.cancel()
    return Response({'success': True, 'message': f'Token {token.token_number} cancelled'})
