"""Analytics KPI and chart data tests."""
from decimal import Decimal

from datetime import timedelta

from django.core.management import call_command
from django.utils import timezone

from core import constants as C
from core.models import PharmacyQueueEntry
from core.services.analytics import (
    compute_daily_analytics,
    compute_kpis,
    parse_monthly_date_range,
)
from core.tests.base import OPDTestCase
from core.views.admin_api import analytics as analytics_view


class AnalyticsWorkflowTests(OPDTestCase):
    def setUp(self):
        super().setUp()
        self.reception, _, self.pharmacist, self.admin = self.create_staff()
        self.doctor_user, self.doctor = self.create_doctor()
        self.slot = self.create_slot(self.doctor, 'afternoon')

    def test_analytics_increase_after_completed_visit(self):
        kpis_before = compute_kpis(self.today)
        completed_before = kpis_before['completed']

        token, _ = self.book_via_api(self.slot, 'Analytics Patient', 40, '9800333444')
        self.check_in_token(token, self.reception)
        self.start_consultation_api(token, self.doctor_user)
        token, _ = self.complete_consultation_api(
            token,
            self.doctor_user,
            lab_tests=[],
            medicines=[{'name': 'Vitamin C', 'dosage': '500mg', 'frequency': 'OD'}],
        )
        self.assertEqual(token.status, C.PENDING_PHARMACY)

        kpis_after = compute_kpis(self.today)
        self.assertGreater(kpis_after['checked_in'], kpis_before['checked_in'])
        self.assertGreaterEqual(kpis_after['avg_consultation_minutes'], 0)

        # Complete pharmacy to mark visit completed
        from core.models import PharmacyQueueEntry
        from core.views.pharmacy import pharmacy_complete_dispense

        entry = PharmacyQueueEntry.objects.get(token=token)
        entry.start_dispensing(self.admin)
        entry.mark_ready()
        done = self.api_post(
            pharmacy_complete_dispense,
            f'/api/core/pharmacy/{entry.id}/complete/',
            {'amount': float(entry.total_bill)},
            user=self.pharmacist,
            entry_id=entry.id,
        )
        self.assertTrue(done.data['success'])
        token.refresh_from_db()
        self.assertEqual(token.status, C.COMPLETED)

        kpis_final = compute_kpis(self.today)
        self.assertGreater(kpis_final['completed'], completed_before)
        self.assertGreaterEqual(kpis_final['system_throughput'], 1)
        self.assertIn('charts', kpis_final)
        self.assertIn('visit_status', kpis_final['charts'])
        self.assertIn('slot_breakdown', kpis_final['charts'])

    def test_compute_analytics_command_and_api(self):
        token, _ = self.book_via_api(self.slot, 'Cmd Patient', 50, '9800444555')
        self.check_in_token(token, self.reception)
        self.start_consultation_api(token, self.doctor_user)
        token, _ = self.complete_consultation_api(
            token,
            self.doctor_user,
            lab_tests=[],
            medicines=[{'name': 'Paracetamol', 'dosage': '500mg', 'frequency': 'OD'}],
        )

        call_command('compute_analytics', date=self.today.isoformat())
        records = compute_daily_analytics(self.today)
        self.assertTrue(len(records) >= 1)

        api_res = self.api_get(analytics_view, '/api/core/analytics/', self.admin)
        self.assertTrue(api_res.data['success'])
        self.assertIn('updated_at', api_res.data)
        self.assertIn('recommendations', api_res.data)

    def test_monthly_overview_metrics_are_synchronized(self):
        token, _ = self.book_via_api(self.slot, 'Sync Patient', 45, '9800555666')
        self.check_in_token(token, self.reception)
        self.start_consultation_api(token, self.doctor_user)
        token, _ = self.complete_consultation_api(
            token,
            self.doctor_user,
            lab_tests=[],
            medicines=[],
        )
        token.refresh_from_db()
        token.status = C.COMPLETED
        token.save(update_fields=['status'])

        kpis = compute_kpis(self.today)
        summary = kpis['monthly_overview']['summary']
        self.assertEqual(kpis['monthly_revenue'], summary['total_revenue'])
        self.assertEqual(kpis['completed'], kpis['today_metrics']['completed'])
        self.assertEqual(kpis['daily_revenue'], kpis['today_metrics']['revenue'])
        self.assertEqual(summary['completed_visits'], summary['series_total_completed'])
        self.assertEqual(summary['total_patients'], summary['series_total_patients'])
        self.assertEqual(summary['no_shows'], summary['series_total_no_shows'])
        self.assertEqual(summary['total_revenue'], summary['series_total_revenue'])

    def test_analytics_api_generates_slot_recommendations_without_command(self):
        """Recommendations are refreshed when admin loads analytics (no compute_analytics CLI)."""
        from datetime import timedelta

        from core.models import SlotOptimizationRecommendation

        self.doctor.avg_consultation_time = 5
        self.doctor.save(update_fields=['avg_consultation_time'])

        now = timezone.now()
        for i in range(3):
            token = self.create_token(
                self.slot,
                status=C.COMPLETED,
                phone=f'9800666{i:03d}',
                patient_name=f'Rec Patient {i}',
            )
            token.consultation_started_at = now - timedelta(minutes=30)
            token.consultation_ended_at = now - timedelta(minutes=10)
            token.save(update_fields=[
                'consultation_started_at', 'consultation_ended_at', 'status',
            ])

        SlotOptimizationRecommendation.objects.all().delete()

        api_res = self.api_get(analytics_view, '/api/core/analytics/', self.admin)
        self.assertTrue(api_res.data['success'])
        self.assertGreaterEqual(len(api_res.data['recommendations']), 1)
        self.assertEqual(
            SlotOptimizationRecommendation.objects.filter(is_acknowledged=False).count(),
            1,
        )

    def test_only_one_active_recommendation_per_doctor(self):
        from datetime import timedelta

        from core.models import SlotOptimizationRecommendation

        self.doctor.avg_consultation_time = 5
        self.doctor.save(update_fields=['avg_consultation_time'])

        SlotOptimizationRecommendation.objects.create(
            doctor=self.doctor,
            configured_avg_minutes=6,
            actual_avg_minutes=Decimal('9.0'),
            variance_percent=Decimal('50.0'),
            recommended_avg_minutes=9,
            message='Stale recommendation',
            is_acknowledged=False,
        )
        old = SlotOptimizationRecommendation.objects.filter(doctor=self.doctor).first()
        SlotOptimizationRecommendation.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=5),
        )

        now = timezone.now()
        for i in range(3):
            token = self.create_token(
                self.slot,
                status=C.COMPLETED,
                phone=f'9800777{i:03d}',
            )
            token.consultation_started_at = now - timedelta(minutes=30)
            token.consultation_ended_at = now - timedelta(minutes=10)
            token.save(update_fields=[
                'consultation_started_at', 'consultation_ended_at', 'status',
            ])

        api_res = self.api_get(analytics_view, '/api/core/analytics/', self.admin)
        self.assertTrue(api_res.data['success'])
        naresh_recs = [
            r for r in api_res.data['recommendations']
            if 'Test Doctor' in r['doctor'] or self.doctor.user.get_full_name() in r['doctor']
        ]
        self.assertEqual(len(naresh_recs), 1)
        self.assertEqual(
            SlotOptimizationRecommendation.objects.filter(
                doctor=self.doctor, is_acknowledged=False,
            ).count(),
            1,
        )

    def test_parse_monthly_date_range_defaults_to_current_month(self):
        start, end, err = parse_monthly_date_range(None, None, today=self.today)
        self.assertIsNone(err)
        self.assertEqual(start, self.today.replace(day=1))
        self.assertEqual(end, self.today)

    def test_parse_monthly_date_range_rejects_long_ranges(self):
        start = self.today - timedelta(days=40)
        _, _, err = parse_monthly_date_range(
            start.isoformat(),
            self.today.isoformat(),
            today=self.today,
        )
        self.assertEqual(err, 'Date range must not exceed 31 days.')

    def test_monthly_overview_custom_date_range(self):
        past = self.today - timedelta(days=10)
        past_slot = self.create_slot(self.doctor, 'morning', date=past)
        token = self.create_token(past_slot, status=C.COMPLETED, phone='9800888999')
        token.refresh_from_db()

        kpis = compute_kpis(
            monthly_start=past.isoformat(),
            monthly_end=self.today.isoformat(),
        )
        overview = kpis['monthly_overview']
        self.assertEqual(overview['month_start'], past.isoformat())
        self.assertEqual(overview['month_end'], self.today.isoformat())
        self.assertGreaterEqual(overview['summary']['completed_visits'], 1)

    def test_analytics_api_accepts_monthly_range_params(self):
        start = self.today.replace(day=1).isoformat()
        end = self.today.isoformat()
        path = f'/api/core/analytics/?monthly_start={start}&monthly_end={end}'
        api_res = self.api_get(analytics_view, path, self.admin)
        self.assertTrue(api_res.data['success'])
        self.assertEqual(api_res.data['monthly_overview']['month_start'], start)
        self.assertEqual(api_res.data['monthly_overview']['month_end'], end)

    def test_analytics_api_rejects_invalid_monthly_range(self):
        start = (self.today - timedelta(days=40)).isoformat()
        path = f'/api/core/analytics/?monthly_start={start}&monthly_end={self.today.isoformat()}'
        api_res = self.api_get(analytics_view, path, self.admin)
        self.assertFalse(api_res.data['success'])
        self.assertEqual(api_res.status_code, 400)
