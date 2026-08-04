import re

from django.contrib.auth.models import AbstractUser
from django.db import models, transaction

from core import constants as C

_PAT_CODE_RE = re.compile(r'^PAT(\d+)$', re.IGNORECASE)


def _exclude_demo_phone_prefixes(queryset):
    for prefix in C.DEMO_PATIENT_PHONE_PREFIXES:
        queryset = queryset.exclude(phone__startswith=prefix)
    return queryset


def _is_demo_patient_phone(phone):
    phone = str(phone or '').strip()
    return any(phone.startswith(prefix) for prefix in C.DEMO_PATIENT_PHONE_PREFIXES)


class PatientSerial(models.Model):
    """Atomic counter for serial Patient IDs (PAT0001, PAT0002, ...)."""
    last_serial = models.IntegerField(default=0)

    class Meta:
        verbose_name = 'Patient serial counter'

    @classmethod
    def serial_patient_queryset(cls):
        """Real registered patients — demo seed accounts are excluded from PAT serial."""
        return _exclude_demo_phone_prefixes(User.objects.filter(role='patient'))

    @classmethod
    def max_assigned_serial(cls):
        """Highest numeric suffix among real (non-demo) patient codes."""
        max_num = 0
        for code in cls.serial_patient_queryset().values_list('patient_code', flat=True):
            if not code:
                continue
            match = _PAT_CODE_RE.match(str(code).strip())
            if match:
                max_num = max(max_num, int(match.group(1)))
        return max_num

    @classmethod
    def code_sort_key(cls, code):
        """Numeric suffix for ordering PAT0001, PAT0002, …"""
        match = _PAT_CODE_RE.match(str(code or '').strip())
        return int(match.group(1)) if match else 999999

    @classmethod
    def sync_from_database(cls):
        """Align counter with the highest PAT code on real (non-demo) patients."""
        db_max = cls.max_assigned_serial()
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(pk=1)
            if seq.last_serial != db_max:
                seq.last_serial = db_max
                seq.save(update_fields=['last_serial'])
        return db_max

    @classmethod
    def clear_demo_patient_codes(cls):
        """Remove PAT codes from demo seed accounts so they don't affect serials."""
        qs = User.objects.filter(role='patient')
        for prefix in C.DEMO_PATIENT_PHONE_PREFIXES:
            qs.filter(phone__startswith=prefix).update(patient_code='')

    @classmethod
    def renumber_all_patients_serial(cls, clear_demo_codes=True):
        """Assign PAT0001..PATnnnn to real patients in registration order."""
        if clear_demo_codes:
            cls.clear_demo_patient_codes()

        patients = list(
            cls.serial_patient_queryset().order_by('date_joined', 'id')
        )
        with transaction.atomic():
            if not patients:
                seq, _ = cls.objects.select_for_update().get_or_create(pk=1)
                seq.last_serial = 0
                seq.save(update_fields=['last_serial'])
                return 0

            for i, patient in enumerate(patients, start=1):
                patient.patient_code = f'_TMP{i:04d}'
                patient.save(update_fields=['patient_code'])
            for i, patient in enumerate(patients, start=1):
                patient.patient_code = f'PAT{i:04d}'
                patient.save(update_fields=['patient_code'])

            seq, _ = cls.objects.select_for_update().get_or_create(pk=1)
            seq.last_serial = len(patients)
            seq.save(update_fields=['last_serial'])
        return len(patients)

    @classmethod
    def next_code(cls):
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(pk=1)
            db_max = cls.max_assigned_serial()
            if seq.last_serial < db_max:
                seq.last_serial = db_max
            seq.last_serial += 1
            seq.save(update_fields=['last_serial'])
            return f'PAT{seq.last_serial:04d}'


class User(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('receptionist', 'Receptionist'),
        ('doctor', 'Doctor'),
        ('lab_tech', 'Lab Technician'),
        ('pharmacist', 'Pharmacist'),
        ('patient', 'Patient'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='patient')
    phone = models.CharField(max_length=15, blank=True)
    age = models.IntegerField(null=True, blank=True)
    address = models.CharField(max_length=255, blank=True)
    patient_code = models.CharField(max_length=12, blank=True, unique=True, null=True)
    is_disabled = models.BooleanField(default=False)
    GENDER_CHOICES = (
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other'),
    )
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True, default='')

    @property
    def patient_id(self):
        if self.role == 'patient':
            return self.patient_code or None
        return None

    def should_assign_patient_code(self):
        return (
            self.role == 'patient'
            and not self.patient_code
            and not _is_demo_patient_phone(self.phone)
        )

    def assign_patient_code(self):
        if self.should_assign_patient_code():
            PatientSerial.sync_from_database()
            self.patient_code = PatientSerial.next_code()

    def save(self, *args, **kwargs):
        if self.should_assign_patient_code():
            self.assign_patient_code()
        super().save(*args, **kwargs)

    @classmethod
    def resolve_patient_id(cls, patient_id_str):
        """Resolve patient by canonical patient_code (PAT0001)."""
        if not patient_id_str:
            return None
        raw = patient_id_str.strip().upper()
        try:
            return cls.objects.get(patient_code=raw, role='patient')
        except cls.DoesNotExist:
            pass
        # Legacy P0001 format (pre-migration)
        if raw.startswith('P') and not raw.startswith('PAT') and raw[1:].isdigit():
            legacy_pat = f'PAT{int(raw[1:]):04d}'
            try:
                return cls.objects.get(patient_code=legacy_pat, role='patient')
            except cls.DoesNotExist:
                pass
        return None

    def __str__(self):
        return f"{self.username} ({self.role})"


class APIToken(models.Model):
    """Per-login API token — each browser tab can hold its own token."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='api_tokens')
    key = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f'Token for {self.user.username}'
