import os
from django.core.management.base import BaseCommand
from django.contrib.auth.hashers import make_password

from accounts.models import User
from core.models import DoctorProfile, FollowupRule
from core.utils import ensure_today_tomorrow_slots


class Command(BaseCommand):
    help = 'Seed demo users, doctors, and consultation slots'

    def handle(self, *args, **options):
        users = [
            ('admin', 'admin', 'admin', 'Admin User'),
            ('reception', 'reception123', 'receptionist', 'Reception Desk'),
            ('doctor1', 'doctor123', 'doctor', 'Rajesh Sharma'),
            ('doctor2', 'doctor123', 'doctor', 'Naresh Kharbuja'),
            ('doctor3', 'doctor123', 'doctor', 'Sita Thapa'),
            ('labtech', 'lab123', 'lab_tech', 'Lab Technician'),
            ('pharmacist', 'pharmacy123', 'pharmacist', 'Pharmacist User'),
        ]
        for username, password, role, name in users:
            parts = name.split()
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    'password': make_password(password),
                    'role': role,
                    'first_name': parts[0],
                    'last_name': ' '.join(parts[1:]) if len(parts) > 1 else '',
                },
            )
            if not created:
                user.password = make_password(password)
                user.role = role
                user.save()
            self.stdout.write(f'  User: {username} / {password} ({role})')

        doctors = [
            ('doctor1', 'General Physician', 'MBBS', 10),
            ('doctor2', 'General Physician', 'MBBS', 10),
            ('doctor3', 'General Physician', 'MBBS', 10),
        ]
        for username, spec, qual, avg in doctors:
            user = User.objects.get(username=username)
            DoctorProfile.objects.get_or_create(
                user=user,
                defaults={
                    'specialization': spec,
                    'qualification': qual,
                    'avg_consultation_time': avg,
                },
            )

        FollowupRule.objects.filter(is_active=True).update(is_active=False)
        FollowupRule.objects.create(exempt_within_days=7, is_active=True)

        ensure_today_tomorrow_slots()
        self.stdout.write(self.style.SUCCESS('Seed data created. Run server and login with staff accounts.'))
