"""Shared doctor queue ordering — used by reception, doctor portal, and patient queue status."""
import re

from django.utils import timezone

SLOT_PREFIX_ORDER = {'M': 0, 'A': 1, 'E': 2}


def parse_token_number(token_number):
    """Return (slot_prefix_rank, serial_number, prefix) for M1 / A2 / E10."""
    raw = str(token_number or '').strip()
    match = re.match(r'^([A-Za-z]+)(\d+)$', raw)
    if not match:
        return (9, 0, raw)
    prefix = match[1].upper()
    return (SLOT_PREFIX_ORDER.get(prefix, 9), int(match[2]), prefix)


def _priority_weight(entry):
    return 0 if entry.priority == 'high' else 1


def _lateness_weight(token):
    """On-time (present) patients are seen before late (missed) arrivals."""
    return 1 if token.checkin_status == 'missed' else 0


def queue_entry_sort_key(entry):
    """
    Queue discipline within a slot:
    1. High priority (elderly / disabled) before normal
    2. On-time before late check-ins
    3. Token serial (E2, E3, E6 — not check-in or booking time)
  """
    prefix_rank, serial, _ = parse_token_number(entry.token.token_number)
    return (
        _priority_weight(entry),
        _lateness_weight(entry.token),
        prefix_rank,
        serial,
    )


def sort_waiting_entries(entries):
    return sorted(entries, key=queue_entry_sort_key)


def waiting_entries_queryset(slot=None, doctor_id=None, queue_date=None):
    from core.models import QueueEntry

    qs = QueueEntry.objects.filter(
        queue_status='waiting',
        token__status='checked_in',
    ).select_related('token', 'token__slot', 'token__patient')
    if slot is not None:
        qs = qs.filter(slot=slot)
    if doctor_id is not None:
        qs = qs.filter(doctor_id=doctor_id)
    if queue_date is not None:
        qs = qs.filter(queue_date=queue_date)
    return qs


def ordered_waiting_entries(slot=None, doctor_id=None, queue_date=None):
    return sort_waiting_entries(
        list(waiting_entries_queryset(slot=slot, doctor_id=doctor_id, queue_date=queue_date))
    )


def queue_position_for_entry(entry):
    if entry.queue_status != 'waiting':
        return None
    for idx, waiting in enumerate(ordered_waiting_entries(slot=entry.slot)):
        if waiting.id == entry.id:
            return idx + 1
    return None


def get_ordered_queue_entries(doctor_id, date=None):
    date = date or timezone.localdate()
    return ordered_waiting_entries(doctor_id=doctor_id, queue_date=date)


def get_ordered_queue_tokens(doctor_id, date=None):
    return [entry.token for entry in get_ordered_queue_entries(doctor_id, date)]


def get_next_eligible_token(doctor_id, date=None):
    queue = get_ordered_queue_tokens(doctor_id, date)
    return queue[0] if queue else None
