"""Analytics KPI computation and slot optimization feedback loop."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q
from django.utils import timezone

from accounts.models import User
from core.models import (
    ConsultationSlot,
    DailyAnalytics,
    DoctorProfile,
    LabOrder,
    Payment,
    PharmacyQueueEntry,
    QueueEntry,
    SlotOptimizationRecommendation,
    Token,
)

VARIANCE_THRESHOLD_PERCENT = 15


def _queue_entry_sort_key(entry):
    """Match QueueEntry.queue_position: high priority first, then check-in order."""
    return (0 if entry.priority == 'high' else 1, entry.token.created_at)


def _token_sort_key(token):
    """Legacy token-only sort — prefer get_ordered_queue_tokens for live queue."""
    priority = 0 if (token.is_elderly or token.is_disabled) else 1
    num = int(''.join(c for c in token.token_number if c.isdigit()) or 0)
    return (priority, num, token.created_at)


def get_ordered_queue_tokens(doctor_id, date=None):
    """Return checked-in tokens in the same order as reception/patient queue_position."""
    date = date or timezone.localdate()
    entries = list(
        QueueEntry.objects.filter(
            doctor_id=doctor_id,
            queue_date=date,
            queue_status='waiting',
            token__status='checked_in',
        ).select_related('token', 'token__slot', 'token__patient')
    )
    entries.sort(key=_queue_entry_sort_key)
    return [entry.token for entry in entries]


def get_next_eligible_token(doctor_id, date=None):
    queue = get_ordered_queue_tokens(doctor_id, date)
    return queue[0] if queue else None


def compute_kpis(date=None):
    """Compute live KPIs for the analytics dashboard."""
    date = date or timezone.localdate()
    tokens = Token.objects.filter(slot__date=date).select_related('slot__doctor')
    booked = tokens.exclude(status='cancelled')
    total = booked.count()

    completed = tokens.filter(status='completed')
    checked_in = tokens.filter(
        status__in=['checked_in', 'consulting', 'completed', 'pending_lab', 'pending_pharmacy']
    )
    expired = tokens.filter(status='expired')
    no_show_rate = round((expired.count() / total * 100), 1) if total else 0

    wait_times = []
    consult_times = []
    for t in completed:
        wt = t.waiting_time_minutes()
        ct = t.consultation_duration_minutes()
        if wt is not None:
            wait_times.append(wt)
        if ct is not None:
            consult_times.append(ct)

    avg_wait = round(sum(wait_times) / len(wait_times), 1) if wait_times else 0
    avg_consult = round(sum(consult_times) / len(consult_times), 1) if consult_times else 0

    queue_lengths = []
    for doctor in DoctorProfile.objects.all():
        ql = tokens.filter(slot__doctor=doctor, status='checked_in').count()
        queue_lengths.append(ql)
    avg_queue = round(sum(queue_lengths) / len(queue_lengths), 1) if queue_lengths else 0

    throughput = completed.count()

    active_queue = tokens.filter(status__in=['checked_in', 'consulting']).count()
    pharmacy_queue = PharmacyQueueEntry.objects.filter(
        status__in=['waiting', 'dispensing', 'ready'],
        token__slot__date=date,
    ).count()
    pending_lab = LabOrder.objects.filter(
        token__slot__date=date,
        status__in=['fee_pending', 'fee_paid', 'in_queue', 'in_progress'],
    ).count()
    total_lab_tests = LabOrder.objects.filter(ordered_at__date=date).count()
    total_doctors = DoctorProfile.objects.filter(is_available=True).count()
    total_patients_all = User.objects.filter(role='patient').count()

    payments_today = Payment.objects.filter(paid_at__date=date, status='paid')
    daily_revenue = sum(float(p.amount) for p in payments_today)
    month_start = date.replace(day=1)
    monthly_revenue = sum(
        float(p.amount)
        for p in Payment.objects.filter(paid_at__date__gte=month_start, paid_at__date__lte=date, status='paid')
    )
    total_revenue = sum(float(p.amount) for p in Payment.objects.filter(status='paid'))

    present = tokens.filter(checkin_status='present').count()
    checkin_total = tokens.exclude(status__in=['booked', 'cancelled', 'expired']).count()
    checkin_compliance = round((present / checkin_total * 100), 1) if checkin_total else 0

    peak_hours = {}
    for t in checked_in:
        if t.checked_in_at:
            hour = timezone.localtime(t.checked_in_at).hour
            peak_hours[hour] = peak_hours.get(hour, 0) + 1
    peak_hour = max(peak_hours, key=peak_hours.get) if peak_hours else None
    peak_hour_label = f'{peak_hour}:00' if peak_hour is not None else 'N/A'

    lab_orders = LabOrder.objects.filter(
        ordered_at__date=date,
        status='completed',
        completed_at__isnull=False,
    )
    lab_tats = []
    for order in lab_orders:
        tat = (order.completed_at - order.ordered_at).total_seconds() / 60
        lab_tats.append(tat)
    avg_lab_tat = round(sum(lab_tats) / len(lab_tats), 1) if lab_tats else 0

    doctor_idle_minutes = 0
    doctor_queues = []
    for doctor in DoctorProfile.objects.select_related('user'):
        doc_tokens = tokens.filter(slot__doctor=doctor)
        doc_completed = doc_tokens.filter(status='completed')
        doc_consult = [
            t.consultation_duration_minutes()
            for t in doc_completed
            if t.consultation_duration_minutes() is not None
        ]
        slot_minutes = 120 * doc_tokens.values('slot_id').distinct().count()
        busy_minutes = sum(doc_consult)
        idle_minutes = max(slot_minutes - busy_minutes, 0) if slot_minutes else 0
        doctor_idle_minutes += idle_minutes
        utilization_pct = round((busy_minutes / slot_minutes * 100), 1) if slot_minutes else 0
        doctor_queues.append({
            'doctor': str(doctor),
            'doctor_id': doctor.id,
            'queue': doc_tokens.filter(status='checked_in').count(),
            'completed': doc_completed.count(),
            'avg_consult_minutes': round(sum(doc_consult) / len(doc_consult), 1) if doc_consult else None,
            'busy_minutes': round(busy_minutes, 1),
            'slot_minutes': slot_minutes,
            'idle_minutes': round(max(slot_minutes - busy_minutes, 0), 1),
            'utilization_pct': min(utilization_pct, 100),
        })

    avg_utilization = round(
        sum(d['utilization_pct'] for d in doctor_queues) / len(doctor_queues), 1
    ) if doctor_queues else 0

    charts = _build_chart_series(date, tokens, doctor_queues, peak_hours)

    return {
        'date': date.isoformat(),
        'total_patients': total,
        'total_patients_all_time': total_patients_all,
        'todays_patients': total,
        'active_queue': active_queue,
        'completed': completed.count(),
        'completed_appointments': completed.count(),
        'checked_in': checked_in.count(),
        'no_shows': expired.count(),
        'expired_no_shows': expired.count(),
        'no_show_rate': no_show_rate,
        'total_doctors': total_doctors,
        'total_lab_tests': total_lab_tests,
        'pending_lab_tests': pending_lab,
        'pharmacy_queue': pharmacy_queue,
        'revenue': round(total_revenue, 2),
        'daily_revenue': round(daily_revenue, 2),
        'monthly_revenue': round(monthly_revenue, 2),
        'avg_waiting_minutes': avg_wait,
        'avg_queue_length': avg_queue,
        'doctor_idle_minutes': round(doctor_idle_minutes, 1),
        'system_throughput': throughput,
        'checkin_compliance': checkin_compliance,
        'avg_consultation_minutes': avg_consult,
        'peak_hour': peak_hour_label,
        'peak_hour_counts': peak_hours,
        'lab_turnaround_minutes': avg_lab_tat,
        'doctor_queues': doctor_queues,
        'avg_doctor_utilization_pct': avg_utilization,
        'charts': charts,
    }


def _build_chart_series(date, tokens, doctor_queues, peak_hours):
    """Chart-ready datasets for the admin analytics dashboard."""
    status_labels = {
        'booked': 'Booked',
        'checked_in': 'Checked In',
        'consulting': 'Consulting',
        'pending_lab': 'Pending Lab',
        'pending_pharmacy': 'Pending Pharmacy',
        'completed': 'Completed',
        'expired': 'No-show',
        'cancelled': 'Cancelled',
    }
    status_rows = tokens.values('status').annotate(count=Count('id')).order_by('status')
    visit_status = [
        {'status': row['status'], 'label': status_labels.get(row['status'], row['status']), 'count': row['count']}
        for row in status_rows if row['count']
    ]

    slot_labels = {'morning': 'Morning', 'afternoon': 'Afternoon', 'evening': 'Evening'}
    slot_breakdown = []
    for slot_type, label in slot_labels.items():
        slot_tokens = tokens.filter(slot__slot_type=slot_type)
        slot_breakdown.append({
            'slot': slot_type,
            'label': label,
            'booked': slot_tokens.exclude(status='cancelled').count(),
            'completed': slot_tokens.filter(status='completed').count(),
            'no_show': slot_tokens.filter(status='expired').count(),
            'checked_in': slot_tokens.filter(status__in=['checked_in', 'consulting']).count(),
        })

    hourly = []
    for hour in range(6, 22):
        hourly.append({
            'hour': hour,
            'label': f'{hour:02d}:00',
            'count': peak_hours.get(hour, 0),
        })

    trend = _compute_daily_trend(date, days=7)

    wait_buckets = {'0-10': 0, '11-20': 0, '21-30': 0, '31-45': 0, '46+': 0}
    for t in tokens.filter(status='completed'):
        wt = t.waiting_time_minutes()
        if wt is None:
            continue
        if wt <= 10:
            wait_buckets['0-10'] += 1
        elif wt <= 20:
            wait_buckets['11-20'] += 1
        elif wt <= 30:
            wait_buckets['21-30'] += 1
        elif wt <= 45:
            wait_buckets['31-45'] += 1
        else:
            wait_buckets['46+'] += 1

    return {
        'visit_status': visit_status,
        'slot_breakdown': slot_breakdown,
        'hourly_checkins': hourly,
        'daily_trend': trend,
        'wait_time_distribution': [
            {'bucket': k, 'count': v} for k, v in wait_buckets.items()
        ],
        'doctor_utilization': [
            {
                'doctor': d['doctor'],
                'utilization_pct': d['utilization_pct'],
                'busy_minutes': d['busy_minutes'],
                'slot_minutes': d['slot_minutes'],
                'completed': d['completed'],
            }
            for d in doctor_queues
        ],
    }


def _compute_daily_trend(end_date, days=7):
    """Last N days of OPD performance for trend charts."""
    start_date = end_date - timedelta(days=days - 1)
    rows = []
    for offset in range(days):
        d = start_date + timedelta(days=offset)
        day_tokens = Token.objects.filter(slot__date=d).exclude(status='cancelled')
        completed = day_tokens.filter(status='completed')
        wait_times = [
            t.waiting_time_minutes() for t in completed
            if t.waiting_time_minutes() is not None
        ]
        consult_times = [
            t.consultation_duration_minutes() for t in completed
            if t.consultation_duration_minutes() is not None
        ]
        rows.append({
            'date': d.isoformat(),
            'label': d.strftime('%a %d'),
            'patients': day_tokens.count(),
            'completed': completed.count(),
            'no_shows': day_tokens.filter(status='expired').count(),
            'avg_wait_minutes': round(sum(wait_times) / len(wait_times), 1) if wait_times else 0,
            'avg_consult_minutes': round(sum(consult_times) / len(consult_times), 1) if consult_times else 0,
        })
    return rows


def compute_daily_analytics(date=None):
    """Aggregate slot-level analytics into DailyAnalytics records."""
    date = date or timezone.localdate()
    slots = ConsultationSlot.objects.filter(date=date).select_related('doctor')
    results = []
    for slot in slots:
        obj = DailyAnalytics.compute_for_slot(slot)
        tokens = slot.tokens.all()
        peak_q = tokens.filter(status='checked_in').count()
        if obj.peak_queue_length is None or peak_q > obj.peak_queue_length:
            obj.peak_queue_length = peak_q
            obj.save(update_fields=['peak_queue_length'])
        results.append(obj)
    return results


def generate_slot_recommendations(variance_threshold=VARIANCE_THRESHOLD_PERCENT):
    """Compare configured vs actual consultation times; create recommendations."""
    today = timezone.localdate()
    created = []
    for doctor in DoctorProfile.objects.filter(is_available=True):
        tokens = Token.objects.filter(
            slot__doctor=doctor,
            slot__date__gte=today - timedelta(days=7),
            status='completed',
            consultation_started_at__isnull=False,
            consultation_ended_at__isnull=False,
        )
        durations = [t.consultation_duration_minutes() for t in tokens]
        durations = [d for d in durations if d is not None and d > 0]
        if len(durations) < 3:
            continue

        actual_avg = sum(durations) / len(durations)
        configured = doctor.avg_consultation_time
        if configured <= 0:
            continue
        variance = abs(actual_avg - configured) / configured * 100
        if variance < variance_threshold:
            continue

        recommended = max(5, round(actual_avg))
        existing = SlotOptimizationRecommendation.objects.filter(
            doctor=doctor,
            is_acknowledged=False,
            created_at__date=today,
        ).first()
        if existing:
            continue

        message = (
            f"Dr. {doctor.user.get_full_name() or doctor}: configured {configured} min avg consultation, "
            f"but actual average is {actual_avg:.1f} min ({variance:.0f}% variance). "
            f"Recommend setting avg consultation time to {recommended} minutes "
            f"for better slot capacity (max tokens = {120 // recommended})."
        )
        rec = SlotOptimizationRecommendation.objects.create(
            doctor=doctor,
            configured_avg_minutes=configured,
            actual_avg_minutes=Decimal(str(round(actual_avg, 2))),
            variance_percent=Decimal(str(round(variance, 2))),
            recommended_avg_minutes=recommended,
            message=message,
        )
        created.append(rec)
    return created


def get_recommendations(limit=10):
    return SlotOptimizationRecommendation.objects.filter(
        is_acknowledged=False,
    ).select_related('doctor__user').order_by('-created_at')[:limit]
