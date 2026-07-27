"""Shared lab fee collection — used by reception and patient self-pay."""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.models import LabOrder, Payment

PENDING_LAB_STATUSES = ('ordered', 'fee_pending')


class LabPaymentError(Exception):
    def __init__(self, message, status_code=400):
        self.message = message
        self.status_code = status_code


def _parse_amount(value, default):
    if value is None:
        return Decimal(str(default))
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise LabPaymentError('Invalid payment amount', 400)


@transaction.atomic
def pay_lab_order(order_id, amount, collected_by, reference_number=None):
    """Pay a single lab order and place it in the lab technician queue."""
    try:
        order = LabOrder.objects.select_for_update().select_related('token').get(id=order_id)
    except LabOrder.DoesNotExist:
        raise LabPaymentError('Lab order not found', 404)

    if order.status not in PENDING_LAB_STATUSES:
        raise LabPaymentError(f'Lab fee already processed (status: {order.status})', 400)

    pay_amount = _parse_amount(amount, order.fee)
    ref = (reference_number or '').strip() or f'lab-{order.id}'
    payment = Payment.objects.create(
        token=order.token,
        payment_type='lab_fee',
        amount=pay_amount,
        status='paid',
        collected_by=collected_by,
        paid_at=timezone.now(),
        reference_number=ref,
    )
    entry = order.mark_fee_paid()
    order.refresh_from_db()
    return order, payment, entry


@transaction.atomic
def pay_lab_orders_for_token(token_id, collected_by, reference_number=None):
    """Pay all pending lab orders on a token."""
    orders = list(
        LabOrder.objects.select_for_update()
        .filter(token_id=token_id, status__in=PENDING_LAB_STATUSES)
        .select_related('token')
        .order_by('ordered_at')
    )
    if not orders:
        raise LabPaymentError('No pending lab fees for this patient', 400)

    entries = []
    payments = []
    total = Decimal('0')
    base_ref = (reference_number or '').strip()

    for order in orders:
        amount = Decimal(str(order.fee))
        ref = base_ref or f'lab-{order.id}'
        payment = Payment.objects.create(
            token=order.token,
            payment_type='lab_fee',
            amount=amount,
            status='paid',
            collected_by=collected_by,
            paid_at=timezone.now(),
            reference_number=ref,
        )
        entries.append(order.mark_fee_paid())
        payments.append(payment)
        total += amount

    return orders, payments, entries, total
