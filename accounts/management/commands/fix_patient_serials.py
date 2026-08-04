from django.core.management.base import BaseCommand

from accounts.models import PatientSerial


class Command(BaseCommand):
    help = 'Renumber real patient IDs to PAT0001..PATnnnn (demo seed phones excluded).'

    def handle(self, *args, **options):
        count = PatientSerial.renumber_all_patients_serial()
        self.stdout.write(self.style.SUCCESS(f'Renumbered {count} patients (PAT0001–PAT{count:04d}).'))
