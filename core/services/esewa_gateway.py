"""eSewa ePay v2 — redirect form, signature, and status verification."""
import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from django.conf import settings

# Official eSewa UAT credentials (developer.esewa.com.np — no trailing "(" in secret)
ESEWA_UAT_SECRET_KEY = '8gBm/:&EnhH.1/q'
ESEWA_UAT_PRODUCT_CODE = 'EPAYTEST'


def _secret_key():
    product = _product_code()
    if getattr(settings, 'ESEWA_UAT', True) and product == ESEWA_UAT_PRODUCT_CODE:
        return ESEWA_UAT_SECRET_KEY
    key = (getattr(settings, 'ESEWA_SECRET_KEY', '') or '').strip()
    return key or ESEWA_UAT_SECRET_KEY


def _product_code():
    if getattr(settings, 'ESEWA_UAT', True):
        return ESEWA_UAT_PRODUCT_CODE
    return (getattr(settings, 'ESEWA_PRODUCT_CODE', '') or '').strip() or ESEWA_UAT_PRODUCT_CODE


def esewa_form_url():
    if getattr(settings, 'ESEWA_UAT', True):
        return 'https://rc-epay.esewa.com.np/api/epay/main/v2/form'
    return 'https://epay.esewa.com.np/api/epay/main/v2/form'


def esewa_status_url():
    if getattr(settings, 'ESEWA_UAT', True):
        return 'https://rc.esewa.com.np/api/epay/transaction/status/'
    return 'https://esewa.com.np/api/epay/transaction/status/'


def site_base_url():
    return getattr(settings, 'SITE_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')


def esewa_success_url():
    return f'{site_base_url()}/api/core/payments/esewa/success/'


def esewa_failure_url():
    return f'{site_base_url()}/api/core/payments/esewa/failure/'


def format_esewa_amount(value):
    """Match eSewa samples: whole NPR amounts without decimals (110 not 110.00)."""
    dec = Decimal(str(value))
    if dec == dec.to_integral_value():
        return str(int(dec))
    return f'{dec:.2f}'


def build_signed_message(field_names, data):
    parts = []
    for name in field_names:
        name = name.strip()
        if not name:
            continue
        parts.append(f'{name}={data.get(name)}')
    return ','.join(parts)


def generate_signature(field_names, data):
    message = build_signed_message(field_names, data)
    digest = hmac.new(
        _secret_key().encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode('utf-8')


def verify_signature(field_names, data, signature):
    if not signature:
        return False
    if getattr(settings, 'ESEWA_SKIP_SIGNATURE_VERIFY', False):
        return True
    expected = generate_signature(field_names, data)
    return hmac.compare_digest(expected, signature)


def new_transaction_uuid(prefix='OPD'):
    return f'{prefix}-{secrets.token_hex(8)}'


def build_payment_form_fields(
    transaction_uuid,
    amount,
    tax_amount,
    service_charge,
    delivery_charge,
    total_amount,
):
    """Build eSewa v2 form fields (amount + tax + service + delivery = total)."""
    signed_field_names = ['total_amount', 'transaction_uuid', 'product_code']
    product_code = _product_code()
    amount_str = format_esewa_amount(amount)
    tax_str = format_esewa_amount(tax_amount)
    service_str = format_esewa_amount(service_charge)
    delivery_str = format_esewa_amount(delivery_charge)
    total_str = format_esewa_amount(total_amount)
    data = {
        'amount': amount_str,
        'tax_amount': tax_str,
        'product_service_charge': service_str,
        'product_delivery_charge': delivery_str,
        'product_code': product_code,
        'total_amount': total_str,
        'transaction_uuid': transaction_uuid,
        'success_url': esewa_success_url(),
        'failure_url': esewa_failure_url(),
        'signed_field_names': ','.join(signed_field_names),
    }
    data['signature'] = generate_signature(signed_field_names, data)
    return data


def decode_callback_payload(raw_data):
    """Decode Base64 JSON from eSewa success redirect."""
    if not raw_data:
        return None
    try:
        decoded = base64.b64decode(raw_data)
        text = decoded.decode('utf-8')
        return json.loads(text)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def verify_callback_payload(payload):
    if not payload or not isinstance(payload, dict):
        return False
    signed_names = [
        n.strip() for n in str(payload.get('signed_field_names', '')).split(',') if n.strip()
    ]
    if not signed_names:
        return False
    return verify_signature(signed_names, payload, payload.get('signature'))


def check_transaction_status(transaction_uuid, total_amount):
    params = urllib.parse.urlencode({
        'product_code': _product_code(),
        'total_amount': format_esewa_amount(total_amount),
        'transaction_uuid': transaction_uuid,
    })
    url = f'{esewa_status_url()}?{params}'
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None
