"""Real SMS gateway adapters (Sparrow SMS for Nepal, Twilio international)."""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger('opd.sms')


@dataclass
class SmsDeliveryResult:
    success: bool
    error: str = ''
    provider: str = ''
    detail: str = ''


def normalize_nepal_phone(phone: str) -> str:
    """Normalize to 10-digit Nepal mobile (e.g. 9801234567)."""
    digits = re.sub(r'\D', '', phone or '')
    if digits.startswith('977') and len(digits) >= 13:
        digits = digits[3:]
    if digits.startswith('0') and len(digits) == 11:
        digits = digits[1:]
    return digits


def validate_nepal_mobile(phone: str) -> bool:
    normalized = normalize_nepal_phone(phone)
    return len(normalized) == 10 and normalized.isdigit()


def _http_post_form(url: str, data: dict, headers: dict | None = None, timeout: int = 30) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', errors='replace')


def _http_post_json(url: str, data: dict, auth: tuple[str, str] | None = None, timeout: int = 30) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    if auth:
        import base64
        token = base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode('ascii')
        req.add_header('Authorization', f'Basic {token}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode('utf-8', errors='replace')


def send_via_sparrow(phone: str, message: str) -> SmsDeliveryResult:
    token = getattr(settings, 'SMS_API_KEY', '')
    sender = getattr(settings, 'SMS_SENDER_ID', '')
    if not token or not sender:
        return SmsDeliveryResult(
            success=False,
            error='Sparrow SMS not configured. Set SMS_API_KEY and SMS_SENDER_ID in .env',
            provider='sparrow',
        )

    to_number = normalize_nepal_phone(phone)
    if not validate_nepal_mobile(to_number):
        return SmsDeliveryResult(
            success=False,
            error=f'Invalid Nepal mobile number: {phone}',
            provider='sparrow',
        )

    url = getattr(settings, 'SPARROW_SMS_URL', 'http://api.sparrowsms.com/v2/sms/')
    status, body = _http_post_form(url, {
        'token': token,
        'from': sender,
        'to': to_number,
        'text': message,
    })

    if status >= 400:
        return SmsDeliveryResult(
            success=False,
            error=f'Sparrow SMS HTTP {status}',
            provider='sparrow',
            detail=body[:500],
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}

  
    response_code = payload.get('response_code', status)
    if str(response_code) in ('200', '201') or payload.get('count', 0):
        return SmsDeliveryResult(success=True, provider='sparrow', detail=body[:500])

    error_msg = payload.get('response') or payload.get('message') or body[:200]
    return SmsDeliveryResult(
        success=False,
        error=f'Sparrow SMS rejected: {error_msg}',
        provider='sparrow',
        detail=body[:500],
    )


def send_via_twilio(phone: str, message: str) -> SmsDeliveryResult:
    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    from_number = getattr(settings, 'TWILIO_FROM_NUMBER', '')
    if not account_sid or not auth_token or not from_number:
        return SmsDeliveryResult(
            success=False,
            error='Twilio not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER',
            provider='twilio',
        )

    normalized = normalize_nepal_phone(phone)
    if normalized.startswith('98') or normalized.startswith('97') or normalized.startswith('96'):
        to_number = f'+977{normalized}'
    elif phone.strip().startswith('+'):
        to_number = phone.strip()
    else:
        to_number = f'+{normalized}' if normalized else phone.strip()

    url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    status, body = _http_post_json(url, {
        'From': from_number,
        'To': to_number,
        'Body': message,
    }, auth=(account_sid, auth_token))

    if status >= 400:
        return SmsDeliveryResult(
            success=False,
            error=f'Twilio HTTP {status}',
            provider='twilio',
            detail=body[:500],
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}

    if payload.get('sid'):
        return SmsDeliveryResult(success=True, provider='twilio', detail=payload.get('sid', ''))

    return SmsDeliveryResult(
        success=False,
        error=payload.get('message', 'Twilio send failed'),
        provider='twilio',
        detail=body[:500],
    )


def deliver_sms(phone: str, message: str) -> SmsDeliveryResult:
    """Send SMS through the configured live provider."""
    provider = getattr(settings, 'SMS_PROVIDER', 'sparrow').lower().strip()
    if provider == 'twilio':
        return send_via_twilio(phone, message)
    if provider == 'sparrow':
        return send_via_sparrow(phone, message)
    return SmsDeliveryResult(
        success=False,
        error=f'Unknown SMS_PROVIDER "{provider}". Use sparrow or twilio.',
        provider=provider,
    )
