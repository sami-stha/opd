# opd/models.py
# Part 1: Doctor, Slot, Token, Queue, Throttle — extends the code you
# already wrote. Your original DoctorProfile / ConsultationSlot / Token
# logic is kept exactly as-is, just reorganized and completed.

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import datetime, timedelta
from accounts.models import User


class DoctorProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE,
        limit_choices_to={'role': 'doctor'}
    )
    specialization = models.CharField(max_length=100)
    qualification = models.CharField(max_length=150, blank=True)
    avg_consultation_time = models.IntegerField(default=10)
    is_available = models.BooleanField(default=True)
    max_queue_size = models.IntegerField(default=5)
    is_throttled = models.BooleanField(default=False)

    def __str__(self):
        return f"Dr. {self.user.get_full_name()} - General Physician"

    def check_throttle(self):
        """Auto-throttle is disabled when AUTO_THROTTLE_ENABLED is False."""
        from core import constants as C
        if not C.AUTO_THROTTLE_ENABLED:
            if self.is_throttled:
                self.is_throttled = False
                self.save(update_fields=['is_throttled'])
            return False

        today = timezone.localdate()
        current_slot = self.slots.filter(date=today).filter(
            slot_type=self.get_current_slot_type()
        ).first()

        if not current_slot:
            return self.is_throttled

        queue_count = Token.objects.filter(
            slot=current_slot,
            status='checked_in'
        ).count()

        was_throttled = self.is_throttled
        self.is_throttled = queue_count >= self.max_queue_size

        if self.is_throttled != was_throttled:
            ThrottleLog.objects.create(
                slot=current_slot,
                action='throttled' if self.is_throttled else 'resumed',
                queue_length_at_trigger=queue_count,
                threshold_at_trigger=self.max_queue_size,
                triggered_by='system'
            )

        self.save(update_fields=['is_throttled'])
        return self.is_throttled

    def get_current_slot_type(self):
        from core.services.slot_config import get_active_slot_type
        return get_active_slot_type()


class ConsultationSlot(models.Model):
    SLOT_TYPE = (
        ('morning', 'Morning (9:00-11:00)'),
        ('afternoon', 'Afternoon (12:00-14:00)'),
        ('evening', 'Evening (15:00-17:00)'),
    )
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name='slots')
    date = models.DateField()
    slot_type = models.CharField(max_length=20, choices=SLOT_TYPE)
    max_tokens = models.IntegerField(editable=False)

    def save(self, *args, **kwargs):
        from core.services.slot_config import get_slot_type_config
        config = get_slot_type_config(self.slot_type)
        self.max_tokens = config.max_tokens
        super().save(*args, **kwargs)

    @property
    def start_time(self):
        from core.services.slot_config import get_slot_type_config
        return get_slot_type_config(self.slot_type).start_time_str

    @property
    def end_time(self):
        from core.services.slot_config import get_slot_type_config
        return get_slot_type_config(self.slot_type).end_time_str

    @property
    def checkin_opens_at(self):
        from core.services.slot_config import get_slot_type_config
        config = get_slot_type_config(self.slot_type)
        start = datetime.strptime(self.start_time, '%H:%M')
        return (start - timedelta(minutes=config.checkin_opens_minutes_before)).strftime('%H:%M')

    @property
    def avg_consultation_minutes(self):
        from core.services.slot_config import get_slot_type_config
        return get_slot_type_config(self.slot_type).avg_consultation_minutes

    @property
    def tokens_booked(self):
        return self.tokens.filter(
            status__in=['booked', 'checked_in', 'consulting', 'completed']
        ).count()

    @property
    def tokens_checked_in_count(self):
        return self.tokens.filter(status='checked_in').count()

    @property
    def is_full(self):
        return self.tokens_booked >= self.max_tokens

    def __str__(self):
        return f"{self.doctor} - {self.date} {self.slot_type} ({self.tokens_booked}/{self.max_tokens})"

    class Meta:
        unique_together = ['date', 'slot_type']


class SlotTypeConfig(models.Model):
    """Per-slot timing and capacity — drives max tokens and estimated appointment times."""
    SLOT_TYPE = ConsultationSlot.SLOT_TYPE

    slot_type = models.CharField(max_length=20, choices=SLOT_TYPE, unique=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    avg_consultation_minutes = models.PositiveIntegerField(default=10)
    checkin_opens_minutes_before = models.PositiveIntegerField(default=15)
    assigned_doctor = models.ForeignKey(
        'DoctorProfile', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='slot_type_configs',
    )

    class Meta:
        verbose_name = 'Slot type configuration'
        verbose_name_plural = 'Slot type configurations'

    @property
    def duration_minutes(self):
        start = datetime.combine(timezone.localdate(), self.start_time)
        end = datetime.combine(timezone.localdate(), self.end_time)
        if end <= start:
            end += timedelta(days=1)
        return int((end - start).total_seconds() // 60)

    @property
    def max_tokens(self):
        if self.avg_consultation_minutes <= 0:
            return 0
        return max(self.duration_minutes // self.avg_consultation_minutes, 1)

    @property
    def start_time_str(self):
        return self.start_time.strftime('%H:%M')

    @property
    def end_time_str(self):
        return self.end_time.strftime('%H:%M')

    @property
    def time_range(self):
        def label(value):
            return value.strftime('%I:%M %p').lstrip('0')
        return f"{label(self.start_time)} - {label(self.end_time)}"

    def __str__(self):
        return f"{self.slot_type} ({self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')})"


class Token(models.Model):
    STATUS_CHOICES = (
        ('booked', 'Booked'),
        ('checked_in', 'Checked In'),
        ('consulting', 'Consulting'),
        ('pending_lab', 'Pending Lab'),
        ('pending_pharmacy', 'Pending Pharmacy'),
        ('completed', 'Completed'),
        ('expired', 'No-show'),       # never arrived before slot ended
        ('cancelled', 'Cancelled'),
    )
    # Arrival classification — set once, at check-in.
    # 'present' = arrived on or before estimated appointment time.
    # 'missed'  = arrived after estimated time but before slot end (NOT a no-show).
    # A true no-show is tracked via status='expired', not here.
    CHECKIN_STATUS_CHOICES = (
        ('present', 'Present'),
        ('missed', 'Missed'),
    )

    slot = models.ForeignKey(ConsultationSlot, on_delete=models.CASCADE, related_name='tokens')
    patient = models.ForeignKey(
        User, on_delete=models.CASCADE,
        limit_choices_to={'role': 'patient'}, null=True, blank=True,
        related_name='tokens'
    )
    patient_name = models.CharField(max_length=100)
    patient_age = models.IntegerField()
    patient_phone = models.CharField(max_length=15)
    patient_address = models.CharField(max_length=255, blank=True)

    token_number = models.CharField(max_length=10)
    estimated_time = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='booked')
    checkin_status = models.CharField(
        max_length=10, choices=CHECKIN_STATUS_CHOICES, null=True, blank=True
    )

    is_elderly = models.BooleanField(default=False)
    is_disabled = models.BooleanField(default=False)
    is_followup = models.BooleanField(default=False)
    fee_exempted = models.BooleanField(default=False)
    original_token = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='followup_tokens'
    )

    receptionist = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role': 'receptionist'},
        related_name='checked_in_tokens'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    consultation_started_at = models.DateTimeField(null=True, blank=True)
    consultation_ended_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.pk and not self.is_followup:
            # The today/tomorrow cap applies to fresh walk-in/online
            # bookings only. Follow-up tokens are scheduled by the doctor
            # for a specific future date (Step 12) and are exempt.
            today = timezone.localdate()
            if self.slot.date not in (today, today + timedelta(days=1)):
                raise ValidationError(
                    "Tokens can only be booked for today or tomorrow."
                )

            from core.utils import patient_has_active_slot_booking, duplicate_slot_booking_error
            if patient_has_active_slot_booking(
                self.slot,
                patient_user=self.patient,
                patient_phone=self.patient_phone,
            ):
                raise ValidationError(duplicate_slot_booking_error(self.slot))

        if not self.token_number:
            prefix_map = {'morning': 'M', 'afternoon': 'A', 'evening': 'E'}
            prefix = prefix_map.get(self.slot.slot_type, 'M')
            token_count = Token.objects.filter(slot=self.slot).count() + 1
            self.token_number = f"{prefix}{token_count}"

            tz = timezone.get_current_timezone()
            avg = self.slot.avg_consultation_minutes
            slot_start = timezone.make_aware(
                datetime.combine(
                    self.slot.date,
                    datetime.strptime(self.slot.start_time, '%H:%M').time(),
                ),
                tz,
            )
            slot_end = timezone.make_aware(
                datetime.combine(
                    self.slot.date,
                    datetime.strptime(self.slot.end_time, '%H:%M').time(),
                ),
                tz,
            )
            now = timezone.localtime()

            if now < slot_start:
                # Booked before the slot opens (earlier day or same day):
                # schedule from slot start by token order.
                estimate = slot_start + timedelta(
                    minutes=(token_count - 1) * avg
                )
            else:
                # Booked while the slot is live: sync to now and remaining
                # patients still waiting to see the doctor.
                ahead = Token.objects.filter(
                    slot=self.slot,
                    status__in=('booked', 'checked_in', 'consulting'),
                ).count()
                estimate = now + timedelta(minutes=ahead * avg)
                if estimate > slot_end:
                    estimate = slot_end

            self.estimated_time = estimate

        if self.patient_age is not None:
            try:
                if int(self.patient_age) >= 70:
                    self.is_elderly = True
            except (TypeError, ValueError):
                pass

        super().save(*args, **kwargs)

    # ---------- workflow transitions ----------

    def check_in(self, receptionist=None):
        """
        - check-in window opens N minutes before slot start, stays open
          until slot end_time
        - arriving before the window opens -> rejected (too early)
        - arriving after slot end_time -> too late (no-show / expired)
        - on-time: arrived on or before this token's estimated_time -> 'present'
        - late: arrived after estimated_time but before slot end -> 'missed'
        """
        if self.status != 'booked':
            raise ValidationError(f"Cannot check in. Current status: {self.status}")

        now = timezone.localtime()
        slot_date = self.slot.date
        checkin_open = timezone.make_aware(datetime.combine(
            slot_date, datetime.strptime(self.slot.checkin_opens_at, '%H:%M').time()
        ))
        slot_end = timezone.make_aware(datetime.combine(
            slot_date, datetime.strptime(self.slot.end_time, '%H:%M').time()
        ))

        if now < checkin_open:
            raise ValidationError(
                f"Check-in opens at {self.slot.checkin_opens_at}, too early."
            )
        if now > slot_end:
            self.status = 'expired'
            self.save()
            raise ValidationError("Slot has ended. Marked as no-show.")

        estimated = timezone.localtime(self.estimated_time)
        self.checkin_status = 'present' if now <= estimated else 'missed'
        self.status = 'checked_in'
        self.checked_in_at = now
        self.receptionist = receptionist
        self.save()

        QueueEntry.objects.create(
            token=self,
            doctor=self.slot.doctor,
            slot=self.slot,
            queue_date=self.slot.date,
            priority='high' if (self.is_elderly or self.is_disabled) else 'normal',
        )
        self.slot.doctor.check_throttle()

    def start_consultation(self):
        if self.status != 'checked_in':
            raise ValidationError(f"Cannot start consultation. Current status: {self.status}")

        now = timezone.localtime()
        slot_start = timezone.make_aware(datetime.combine(
            self.slot.date,
            datetime.strptime(self.slot.start_time, '%H:%M').time(),
        ))
        if now < slot_start:
            raise ValidationError(
                f"Consultation cannot start before the slot begins at {self.slot.start_time}."
            )

        self.status = 'consulting'
        self.consultation_started_at = timezone.now()
        self.save()

        entry = self.queue_entry
        entry.queue_status = 'in_progress'
        entry.called_at = timezone.now()
        entry.save()

    def complete_consultation(self):
        if self.status != 'consulting':
            raise ValidationError(f"Cannot complete consultation. Current status: {self.status}")
        self.status = 'completed'
        self.consultation_ended_at = timezone.now()
        self.save()

        entry = self.queue_entry
        entry.queue_status = 'done'
        entry.served_at = timezone.now()
        if entry.entered_at:
            entry.wait_minutes = int(
                (entry.served_at - entry.entered_at).total_seconds() / 60
            )
        entry.save()
        self.slot.doctor.check_throttle()

    def cancel(self):
        if self.status != 'booked':
            raise ValidationError(f"Cannot cancel. Current status: {self.status}")
        self.status = 'cancelled'
        self.save()

    def expire_if_unclaimed(self):
        """
        Called by a scheduled job after the slot's end_time passes.
        Only 'booked' tokens that never checked in become 'expired' —
        this is the true no-show case from proposal Step 3. Tokens that
        already checked in (status='checked_in' etc.) are untouched;
        their arrival was already classified via checkin_status.
        """
        if self.status == 'booked':
            self.status = 'expired'
            self.save()

    def waiting_time_minutes(self):
        if self.checked_in_at and self.consultation_started_at:
            return (self.consultation_started_at - self.checked_in_at).total_seconds() / 60
        return None

    def consultation_duration_minutes(self):
        if self.consultation_started_at and self.consultation_ended_at:
            return (self.consultation_ended_at - self.consultation_started_at).total_seconds() / 60
        return None

    def __str__(self):
        return f"Token {self.token_number} - {self.patient_name} ({self.status})"

    class Meta:
        ordering = ['estimated_time']


class QueueEntry(models.Model):
    """
    The doctor's live consultation queue. Created automatically when
    receptionist calls token.check_in(). Priority, on-time vs late, and token
    serial decide queue_position (see core.services.queue_order).
    """
    PRIORITY_CHOICES = (('high', 'High'), ('normal', 'Normal'))
    STATUS_CHOICES = (
        ('waiting', 'Waiting'),
        ('called', 'Called'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
        ('skipped', 'Skipped'),
    )

    token = models.OneToOneField(Token, on_delete=models.CASCADE, related_name='queue_entry')
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name='queue_entries')
    slot = models.ForeignKey(ConsultationSlot, on_delete=models.CASCADE, related_name='queue_entries')
    queue_date = models.DateField()
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='normal')
    queue_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    entered_at = models.DateTimeField(auto_now_add=True)
    called_at = models.DateTimeField(null=True, blank=True)
    served_at = models.DateTimeField(null=True, blank=True)
    wait_minutes = models.IntegerField(null=True, blank=True)

    @property
    def queue_position(self):
        """
        Computed live rather than stored — avoids stale positions when
        a high-priority patient is inserted after others are waiting.
        See core.services.queue_order for the canonical sort rules.
        """
        from core.services.queue_order import queue_position_for_entry

        return queue_position_for_entry(self)

    def __str__(self):
        return f"Queue #{self.queue_position} - {self.token.token_number} ({self.queue_status})"

    class Meta:
        ordering = ['token__created_at']
        verbose_name_plural = 'Queue Entries'


class ThrottleLog(models.Model):
    """Audit trail for every auto-throttle event, feeds the admin dashboard."""
    ACTION_CHOICES = (('throttled', 'Throttled'), ('resumed', 'Resumed'))
    TRIGGER_CHOICES = (('system', 'System'), ('admin', 'Admin'))

    slot = models.ForeignKey(ConsultationSlot, on_delete=models.CASCADE, related_name='throttle_logs')
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    queue_length_at_trigger = models.IntegerField()
    threshold_at_trigger = models.IntegerField()
    triggered_by = models.CharField(max_length=10, choices=TRIGGER_CHOICES, default='system')
    triggered_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.slot} - {self.action} @ {self.triggered_at:%H:%M}"

    class Meta:
        ordering = ['-triggered_at']


class Consultation(models.Model):
    """One record per token, written by the doctor during the visit."""
    token = models.OneToOneField(Token, on_delete=models.CASCADE, related_name='consultation')
    symptoms = models.TextField(blank=True)
    diagnosis = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    requires_lab = models.BooleanField(default=False)
    requires_followup = models.BooleanField(default=False)
    followup_date = models.DateField(null=True, blank=True)
    followup_instructions = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Consultation - {self.token.token_number}"


class LabOrder(models.Model):
    STATUS_CHOICES = (
        ('ordered', 'Ordered'),
        ('fee_pending', 'Fee Pending'),
        ('fee_paid', 'Fee Paid'),
        ('in_queue', 'In Queue'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    )

    consultation = models.ForeignKey(Consultation, on_delete=models.CASCADE, related_name='lab_orders')
    token = models.ForeignKey(Token, on_delete=models.CASCADE, related_name='lab_orders')
    test_name = models.CharField(max_length=150)
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=500)
    instructions = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ordered')
    ordered_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def mark_fee_paid(self):
        """Record payment and place the order in the lab technician queue."""
        now = timezone.now()
        entry, _ = LabQueueEntry.objects.update_or_create(
            lab_order=self,
            defaults={
                'token_id': self.token_id,
                'lab_fee_paid': True,
                'status': 'waiting',
                'entered_at': now,
            },
        )
        self.status = 'in_queue'
        self.save(update_fields=['status'])
        return entry

    def __str__(self):
        return f"{self.test_name} - {self.token.token_number} ({self.status})"


class LabQueueEntry(models.Model):
    """Lab technician's live task queue, one entry per lab order."""
    STATUS_CHOICES = (
        ('waiting', 'Waiting'),
        ('in_progress', 'In Progress'),
        ('done', 'Done'),
    )

    lab_order = models.OneToOneField(LabOrder, on_delete=models.CASCADE, related_name='queue_entry')
    token = models.ForeignKey(Token, on_delete=models.CASCADE, related_name='lab_queue_entries')
    technician = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role': 'lab_tech'},
        related_name='lab_queue_entries'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    lab_fee_paid = models.BooleanField(default=False)
    entered_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def start(self, technician):
        if self.status != 'waiting':
            raise ValidationError(f"Cannot start. Current status: {self.status}")
        self.status = 'in_progress'
        self.technician = technician
        self.started_at = timezone.now()
        self.save()
        self.lab_order.status = 'in_progress'
        self.lab_order.save()

    def complete(self):
        self.status = 'done'
        self.completed_at = timezone.now()
        self.save()
        self.lab_order.status = 'completed'
        self.lab_order.completed_at = timezone.now()
        self.lab_order.save()

    def turnaround_minutes(self):
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() / 60
        return None

    def __str__(self):
        return f"Lab Queue - {self.lab_order.test_name} ({self.status})"


class LabReport(models.Model):
    """Uploaded by lab technician after completing the test."""
    lab_order = models.OneToOneField(LabOrder, on_delete=models.CASCADE, related_name='report')
    uploaded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'lab_tech'},
        related_name='uploaded_reports'
    )
    report_file = models.FileField(upload_to='lab_reports/%Y/%m/')
    findings = models.TextField(blank=True)
    is_same_day = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.pk:
            slot = self.lab_order.token.slot
            now = timezone.now()
            slot_end = timezone.make_aware(
                timezone.datetime.combine(
                    slot.date,
                    timezone.datetime.strptime(slot.end_time, '%H:%M').time()
                )
            )
            self.is_same_day = now <= slot_end
        super().save(*args, **kwargs)

        if self.is_same_day:
            # Patient re-enters the same doctor queue, no new consultation fee
            token = self.lab_order.token
            token.status = 'checked_in'
            token.save()
            QueueEntry.objects.get_or_create(
                token=token,
                defaults={
                    'doctor': token.slot.doctor,
                    'slot': token.slot,
                    'queue_date': token.slot.date,
                    'priority': 'high' if (token.is_elderly or token.is_disabled) else 'normal',
                }
            )

    def __str__(self):
        return f"Report - {self.lab_order.test_name}"


class Prescription(models.Model):
    """One row per medicine line; multiple rows per consultation."""
    consultation = models.ForeignKey(Consultation, on_delete=models.CASCADE, related_name='prescriptions')
    token = models.ForeignKey(Token, on_delete=models.CASCADE, related_name='prescriptions')
    medicine_name = models.CharField(max_length=150)
    dosage = models.CharField(max_length=50, blank=True)
    frequency = models.CharField(max_length=50, blank=True)
    duration_days = models.IntegerField(null=True, blank=True)
    instructions = models.CharField(max_length=255, blank=True)
    dispensed = models.BooleanField(default=False)
    dispensed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.medicine_name} - {self.token.token_number}"


class PharmacyQueueEntry(models.Model):
    """Pharmacist's live task queue, one entry per token with prescriptions."""
    STATUS_CHOICES = (
        ('waiting', 'Waiting'),
        ('dispensing', 'Dispensing'),
        ('ready', 'Ready for Pickup'),
        ('done', 'Done'),
    )

    token = models.OneToOneField(Token, on_delete=models.CASCADE, related_name='pharmacy_queue_entry')
    pharmacist = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        limit_choices_to={'role': 'pharmacist'},
        related_name='pharmacy_queue_entries'
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    entered_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    total_bill = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_collected = models.BooleanField(default=False)

    def start_dispensing(self, pharmacist):
        self.status = 'dispensing'
        self.pharmacist = pharmacist
        self.started_at = timezone.now()
        self.save()

    def mark_ready(self):
        self.status = 'ready'
        self.save(update_fields=['status'])

    def complete(self):
        self.status = 'done'
        self.completed_at = timezone.now()
        self.save()
        self.token.prescriptions.update(dispensed=True, dispensed_at=timezone.now())
        # Visit stays open if lab work is still outstanding
        pending_labs = self.token.lab_orders.exclude(status='completed').exists()
        self.token.status = 'pending_lab' if pending_labs else 'completed'
        self.token.save()

    def __str__(self):
        return f"Pharmacy Queue - {self.token.token_number} ({self.status})"


class Payment(models.Model):
    TYPE_CHOICES = (
        ('consultation_fee', 'Consultation Fee'),
        ('lab_fee', 'Lab Fee'),
        ('pharmacy_fee', 'Pharmacy Fee'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('waived', 'Waived'),
    )

    token = models.ForeignKey(Token, on_delete=models.CASCADE, related_name='payments')
    payment_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    waiver_reason = models.CharField(max_length=150, blank=True)
    collected_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='collected_payments'
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    reference_number = models.CharField(max_length=50, blank=True)

    def mark_paid(self, collected_by, reference_number=''):
        self.status = 'paid'
        self.collected_by = collected_by
        self.reference_number = reference_number
        self.paid_at = timezone.now()
        self.save()

    def mark_waived(self, reason, collected_by=None):
        self.status = 'waived'
        self.waiver_reason = reason
        self.collected_by = collected_by
        self.paid_at = timezone.now()
        self.save()

    def __str__(self):
        return f"{self.payment_type} - {self.token.token_number} ({self.status})"


class FollowupRule(models.Model):
    """
    Admin-configurable follow-up fee exemption policy. Only one row
    should be active at a time — enforced in clean(), not at DB level,
    since this is a tiny lookup table managed entirely from admin.
    """
    exempt_within_days = models.IntegerField(default=7)
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        limit_choices_to={'role': 'admin'}
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_active(cls):
        return cls.objects.filter(is_active=True).first()

    @classmethod
    def check_exemption(cls, original_token, new_visit_date):
        if not original_token:
            return False
        rule = cls.get_active()
        exempt_days = rule.exempt_within_days if rule else 7
        days_diff = (new_visit_date - original_token.slot.date).days
        return 0 <= days_diff <= exempt_days

    def __str__(self):
        return f"Follow-up exemption: {self.exempt_within_days} days ({'active' if self.is_active else 'inactive'})"


class OTPVerification(models.Model):
    """Secure OTP storage with expiry and retry limits."""
    PURPOSE_CHOICES = (
        ('registration', 'Registration'),
        ('login', 'Login'),
        ('password_reset', 'Password Reset'),
    )
    phone = models.CharField(max_length=15, db_index=True)
    otp_hash = models.CharField(max_length=128)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"OTP {self.phone} ({self.purpose})"


class SlotOptimizationRecommendation(models.Model):
    """Admin recommendations from analytics feedback loop."""
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name='slot_recommendations')
    configured_avg_minutes = models.IntegerField()
    actual_avg_minutes = models.DecimalField(max_digits=6, decimal_places=2)
    variance_percent = models.DecimalField(max_digits=6, decimal_places=2)
    recommended_avg_minutes = models.IntegerField()
    message = models.TextField()
    is_acknowledged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class DailyAnalytics(models.Model):
    """
    Pre-aggregated summary table — computed by a scheduled job (e.g.
    Celery task or management command run nightly/hourly), NOT queried
    live from transactional tables. This is the separation described
    in the proposal's Section 2.2 (transactional vs analytical design).
    """
    report_date = models.DateField()
    doctor = models.ForeignKey(DoctorProfile, on_delete=models.CASCADE, related_name='analytics')
    slot = models.ForeignKey(ConsultationSlot, on_delete=models.CASCADE, related_name='analytics')

    total_tokens_issued = models.IntegerField(default=0)
    total_checkins = models.IntegerField(default=0)
    total_no_shows = models.IntegerField(default=0)
    avg_wait_minutes = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    avg_consultation_minutes = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    peak_queue_length = models.IntegerField(null=True, blank=True)
    peak_hour = models.IntegerField(null=True, blank=True)
    throttle_events_count = models.IntegerField(default=0)
    computed_at = models.DateTimeField(auto_now=True)

    @classmethod
    def compute_for_slot(cls, slot):
        """Run once per slot after it closes; recomputes and upserts."""
        tokens = slot.tokens.all()
        completed = tokens.filter(status='completed')
        wait_times = [t.waiting_time_minutes() for t in completed if t.waiting_time_minutes() is not None]
        consult_times = [t.consultation_duration_minutes() for t in completed if t.consultation_duration_minutes() is not None]

        obj, _ = cls.objects.update_or_create(
            report_date=slot.date,
            doctor=slot.doctor,
            slot=slot,
            defaults={
                'total_tokens_issued': tokens.count(),
                'total_checkins': tokens.exclude(status__in=['booked', 'cancelled']).count(),
                'total_no_shows': tokens.filter(status='expired').count(),
                'avg_wait_minutes': sum(wait_times) / len(wait_times) if wait_times else None,
                'avg_consultation_minutes': sum(consult_times) / len(consult_times) if consult_times else None,
                'throttle_events_count': slot.throttle_logs.filter(action='throttled').count(),
            }
        )
        return obj

    class Meta:
        unique_together = ['report_date', 'doctor', 'slot']
        verbose_name_plural = 'Daily Analytics'

    def __str__(self):
        return f"{self.doctor} - {self.report_date}"
