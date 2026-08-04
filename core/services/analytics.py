"""Analytics KPI computation and slot optimization feedback loop."""
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count
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


def _booked_tokens_for_date(date):
    return Token.objects.filter(slot__date=date).exclude(status='cancelled')


def _booked_tokens_for_range(start_date, end_date):
    return Token.objects.filter(
        slot__date__gte=start_date,
        slot__date__lte=end_date,
    ).exclude(status='cancelled')


def _no_show_tokens(token_qs):
    """True no-shows only — status expired (never checked in before slot ended)."""
    return token_qs.filter(status='expired')


def _completed_tokens(token_qs):
    return token_qs.filter(status='completed')


def _payment_revenue(start_date, end_date=None):
    end_date = end_date or start_date
    return round(
        sum(
            float(p.amount)
            for p in Payment.objects.filter(
                paid_at__date__gte=start_date,
                paid_at__date__lte=end_date,
                status='paid',
            )
        ),
        2,
    )


def _total_revenue_to_date(end_date):
    return round(
        sum(float(p.amount) for p in Payment.objects.filter(status='paid', paid_at__date__lte=end_date)),
        2,
    )


def _wait_and_consult_times(completed_qs):
    wait_times = []
    consult_times = []
    for token in completed_qs:
        wt = token.waiting_time_minutes()
        if wt is not None:
            wait_times.append(wt)
        ct = token.consultation_duration_minutes()
        if ct is not None:
            consult_times.append(max(0, ct))
    return wait_times, consult_times


def _aggregate_day_metrics(date, label_format='%a %d'):
    """Single source of truth for per-day OPD metrics (charts + aggregates)."""
    booked = _booked_tokens_for_date(date)
    completed_qs = _completed_tokens(booked)
    wait_times, consult_times = _wait_and_consult_times(completed_qs)
    booked_count = booked.count()
    return {
        'date': date.isoformat(),
        'label': date.strftime(label_format),
        'patients': booked_count,
        'booked': booked_count,
        'completed': completed_qs.count(),
        'no_shows': _no_show_tokens(booked).count(),
        'avg_wait_minutes': round(sum(wait_times) / len(wait_times), 1) if wait_times else 0,
        'avg_consult_minutes': round(sum(consult_times) / len(consult_times), 1) if consult_times else 0,
        'revenue': _payment_revenue(date),
    }


def _slot_breakdown(token_qs):
    slot_labels = {'morning': 'Morning', 'afternoon': 'Afternoon', 'evening': 'Evening'}
    rows = []
    for slot_type, label in slot_labels.items():
        slot_tokens = token_qs.filter(slot__slot_type=slot_type)
        rows.append({
            'slot': slot_type,
            'label': label,
            'booked': slot_tokens.count(),
            'completed': _completed_tokens(slot_tokens).count(),
            'no_show': _no_show_tokens(slot_tokens).count(),
            'checked_in': slot_tokens.filter(status__in=['checked_in', 'consulting']).count(),
        })
    return rows


def get_ordered_queue_tokens(doctor_id, date=None):
    from core.services.queue_order import get_ordered_queue_tokens as _ordered_tokens

    return _ordered_tokens(doctor_id, date)


def get_next_eligible_token(doctor_id, date=None):
    from core.services.queue_order import get_next_eligible_token as _next_token

    return _next_token(doctor_id, date)


def compute_kpis(date=None):
    """Compute live KPIs for the analytics dashboard."""
    date = date or timezone.localdate()
    monthly_overview = compute_monthly_overview(date)
    month_summary = monthly_overview['summary']

    tokens = Token.objects.filter(slot__date=date).select_related('slot__doctor')
    booked = tokens.exclude(status='cancelled')
    total = booked.count()

    completed_qs = _completed_tokens(tokens)
    no_show_count = _no_show_tokens(tokens).count()
    no_show_rate = round((no_show_count / total * 100), 1) if total else 0

    checked_in = tokens.filter(
        status__in=['checked_in', 'consulting', 'completed', 'pending_lab', 'pending_pharmacy']
    )

    wait_times, consult_times = _wait_and_consult_times(completed_qs)
    avg_wait = round(sum(wait_times) / len(wait_times), 1) if wait_times else 0
    avg_consult = round(sum(consult_times) / len(consult_times), 1) if consult_times else 0

    queue_lengths = []
    for doctor in DoctorProfile.objects.all():
        ql = tokens.filter(slot__doctor=doctor, status='checked_in').count()
        queue_lengths.append(ql)
    avg_queue = round(sum(queue_lengths) / len(queue_lengths), 1) if queue_lengths else 0

    throughput = completed_qs.count()
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

    daily_revenue = _payment_revenue(date)

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

    doctor_idle_minutes = 0
    doctor_queues = []
    for doctor in DoctorProfile.objects.select_related('user'):
        doc_tokens = tokens.filter(slot__doctor=doctor)
        doc_completed = _completed_tokens(doc_tokens)
        _, doc_consult_vals = _wait_and_consult_times(doc_completed)
        slot_minutes = 120 * doc_tokens.values('slot_id').distinct().count()
        busy_minutes = sum(doc_consult_vals)
        doctor_idle_minutes += max(slot_minutes - busy_minutes, 0) if slot_minutes else 0
        utilization_pct = round((busy_minutes / slot_minutes * 100), 1) if slot_minutes else 0
        doctor_queues.append({
            'doctor': str(doctor),
            'doctor_id': doctor.id,
            'queue': doc_tokens.filter(status='checked_in').count(),
            'completed': doc_completed.count(),
            'avg_consult_minutes': round(sum(doc_consult_vals) / len(doc_consult_vals), 1) if doc_consult_vals else None,
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
        'completed': throughput,
        'completed_appointments': throughput,
        'checked_in': checked_in.count(),
        'no_shows': no_show_count,
        'expired_no_shows': no_show_count,
        'no_show_rate': no_show_rate,
        'total_doctors': total_doctors,
        'total_lab_tests': total_lab_tests,
        'pending_lab_tests': pending_lab,
        'pharmacy_queue': pharmacy_queue,
        'revenue': _total_revenue_to_date(date),
        'daily_revenue': daily_revenue,
        'monthly_revenue': month_summary['total_revenue'],
        'avg_waiting_minutes': avg_wait,
        'avg_queue_length': avg_queue,
        'doctor_idle_minutes': round(doctor_idle_minutes, 1),
        'system_throughput': throughput,
        'checkin_compliance': checkin_compliance,
        'avg_consultation_minutes': avg_consult,
        'peak_hour': peak_hour_label,
        'peak_hour_counts': peak_hours,
        'doctor_queues': doctor_queues,
        'avg_doctor_utilization_pct': avg_utilization,
        'charts': charts,
        'monthly_overview': monthly_overview,
        'today_metrics': _aggregate_day_metrics(date),
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

    slot_breakdown = _slot_breakdown(tokens)

    hourly = []
    for hour in range(6, 22):
        hourly.append({
            'hour': hour,
            'label': f'{hour:02d}:00',
            'count': peak_hours.get(hour, 0),
        })

    trend = _compute_daily_trend(date, days=7)

    wait_buckets = {'0-10': 0, '11-20': 0, '21-30': 0, '31-45': 0, '46+': 0}
    for t in _completed_tokens(tokens):
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
    return [
        _aggregate_day_metrics(start_date + timedelta(days=offset))
        for offset in range(days)
    ]


def compute_monthly_overview(reference_date=None):
    """Calendar-month summary; all monthly charts use the same date range and metrics."""
    reference_date = reference_date or timezone.localdate()
    month_start = reference_date.replace(day=1)

    month_tokens = _booked_tokens_for_range(month_start, reference_date)
    month_completed = _completed_tokens(month_tokens)
    month_no_show_count = _no_show_tokens(month_tokens).count()

    wait_times, consult_times = _wait_and_consult_times(month_completed)
    booked_count = month_tokens.count()
    completed_count = month_completed.count()
    month_revenue = _payment_revenue(month_start, reference_date)

    calendar_series = []
    d = month_start
    while d <= reference_date:
        calendar_series.append(_aggregate_day_metrics(d, label_format='%d %b'))
        d += timedelta(days=1)

    # Charts and hero cards sum the same daily rows as the month summary.
    series_totals = {
        'patients': sum(r['patients'] for r in calendar_series),
        'completed': sum(r['completed'] for r in calendar_series),
        'no_shows': sum(r['no_shows'] for r in calendar_series),
        'revenue': round(sum(r['revenue'] for r in calendar_series), 2),
    }

    weekly_map = {}
    for row in calendar_series:
        day = datetime.strptime(row['date'], '%Y-%m-%d').date()
        week_key = day.isocalendar()[:2]
        label = f'W{week_key[1]}'
        bucket = weekly_map.setdefault(week_key, {
            'label': label,
            'patients': 0,
            'completed': 0,
            'no_shows': 0,
            'revenue': 0.0,
        })
        bucket['patients'] += row['patients']
        bucket['completed'] += row['completed']
        bucket['no_shows'] += row['no_shows']
        bucket['revenue'] = round(bucket['revenue'] + row['revenue'], 2)

    weekly_totals = list(weekly_map.values())

    slot_breakdown = _slot_breakdown(month_tokens)

    month_payments = Payment.objects.filter(
        paid_at__date__gte=month_start,
        paid_at__date__lte=reference_date,
        status='paid',
    )
    revenue_by_type = []
    for payment_type, label in (
        ('consultation_fee', 'Consultation'),
        ('lab_fee', 'Laboratory'),
        ('pharmacy_fee', 'Pharmacy'),
    ):
        amount = sum(float(p.amount) for p in month_payments.filter(payment_type=payment_type))
        if amount > 0:
            revenue_by_type.append({'type': payment_type, 'label': label, 'amount': round(amount, 2)})

    doctor_throughput = []
    for doctor in DoctorProfile.objects.select_related('user'):
        doc_completed = month_completed.filter(slot__doctor=doctor).count()
        if doc_completed:
            doctor_throughput.append({
                'doctor': str(doctor),
                'completed': doc_completed,
            })

    active_days = len([r for r in calendar_series if r['patients'] > 0])
    peak_day = max(calendar_series, key=lambda r: r['completed']) if calendar_series else None

    return {
        'month_label': month_start.strftime('%B %Y'),
        'month_start': month_start.isoformat(),
        'month_end': reference_date.isoformat(),
        'summary': {
            'total_patients': booked_count,
            'completed_visits': completed_count,
            'no_shows': month_no_show_count,
            'completion_rate': round((completed_count / booked_count * 100), 1) if booked_count else 0,
            'no_show_rate': round((month_no_show_count / booked_count * 100), 1) if booked_count else 0,
            'total_revenue': round(month_revenue, 2),
            'lab_tests': LabOrder.objects.filter(
                ordered_at__date__gte=month_start,
                ordered_at__date__lte=reference_date,
            ).count(),
            'avg_wait_minutes': round(sum(wait_times) / len(wait_times), 1) if wait_times else 0,
            'avg_consult_minutes': round(sum(consult_times) / len(consult_times), 1) if consult_times else 0,
            'avg_daily_patients': round(booked_count / active_days, 1) if active_days else 0,
            'active_days': active_days,
            'peak_day_label': peak_day['label'] if peak_day and peak_day['completed'] else '—',
            'peak_day_completed': peak_day['completed'] if peak_day else 0,
            'series_total_patients': series_totals['patients'],
            'series_total_completed': series_totals['completed'],
            'series_total_no_shows': series_totals['no_shows'],
            'series_total_revenue': series_totals['revenue'],
        },
        'daily_series': calendar_series,
        'calendar_month_series': calendar_series,
        'weekly_totals': weekly_totals,
        'slot_breakdown': slot_breakdown,
        'revenue_by_type': revenue_by_type,
        'doctor_throughput': doctor_throughput,
    }


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
        ).order_by('-created_at').first()
        if existing:
            existing.configured_avg_minutes = configured
            existing.actual_avg_minutes = Decimal(str(round(actual_avg, 2)))
            existing.variance_percent = Decimal(str(round(variance, 2)))
            existing.recommended_avg_minutes = recommended
            existing.message = (
                f"Dr. {doctor.user.get_full_name() or doctor}: configured {configured} min avg consultation, "
                f"but actual average is {actual_avg:.1f} min ({variance:.0f}% variance). "
                f"Recommend setting avg consultation time to {recommended} minutes "
                f"for better slot capacity (max tokens = {120 // recommended})."
            )
            existing.save()
            # Supersede any older duplicate rows for this doctor (legacy data).
            SlotOptimizationRecommendation.objects.filter(
                doctor=doctor,
                is_acknowledged=False,
            ).exclude(pk=existing.pk).update(is_acknowledged=True)
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
    """Latest unacknowledged recommendation per doctor."""
    qs = SlotOptimizationRecommendation.objects.filter(
        is_acknowledged=False,
    ).select_related('doctor__user').order_by('-created_at')
    seen_doctors = set()
    results = []
    for rec in qs:
        if rec.doctor_id in seen_doctors:
            continue
        seen_doctors.add(rec.doctor_id)
        results.append(rec)
        if len(results) >= limit:
            break
    return results


_RECOMMENDATIONS_MIN_INTERVAL_SECONDS = 60
_last_slot_recommendations_run = 0.0


def ensure_slot_recommendations(variance_threshold=VARIANCE_THRESHOLD_PERCENT):
    """
    Refresh slot optimization recommendations from live consultation data.
    Safe to call on each analytics request — throttled to once per minute.
    """
    import time

    global _last_slot_recommendations_run
    now = time.monotonic()
    if now - _last_slot_recommendations_run < _RECOMMENDATIONS_MIN_INTERVAL_SECONDS:
        return []
    _last_slot_recommendations_run = now
    return generate_slot_recommendations(variance_threshold)
