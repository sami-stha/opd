from django.core.management import call_command
from django.core.management.base import BaseCommand

from core.services.demo_workflow_seed import DemoWorkflowSeeder


class Command(BaseCommand):
    help = (
        'Seed full demo patient workflow into the REAL database: bookings, check-in, '
        'consultations with prescriptions, lab and pharmacy queues (mixed states), '
        'follow-up reminders, and analytics aggregates.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--slot',
            choices=['all', 'morning', 'afternoon', 'evening'],
            default='all',
            help='Seed all slot types (default) or only one slot type',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Remove prior demo patients (phones 9800100xxx) before seeding',
        )
        parser.add_argument(
            '--with-followup-book',
            action='store_true',
            help='Also seed a patient with a booked fee-exempt follow-up token',
        )
        parser.add_argument(
            '--skip-base-seed',
            action='store_true',
            help='Do not run seed_opd_data first (use if staff/slots already exist)',
        )
        parser.add_argument(
            '--no-history',
            action='store_true',
            help='Skip seeding historical visits for analytics trends',
        )
        parser.add_argument(
            '--history-days',
            type=int,
            default=29,
            help='Days of historical demo visits before today (default: 29, ~1 month)',
        )

    def handle(self, *args, **options):
        if not options['skip_base_seed']:
            self.stdout.write('Running seed_opd_data (staff, doctors, slots)...')
            call_command('seed_opd_data')

        seeder = DemoWorkflowSeeder(stdout=self.stdout, style=self.style)
        seeder.run(
            slot_type=options['slot'],
            with_followup_book=options['with_followup_book'],
            clear=options['clear'],
            with_history=not options['no_history'],
            history_days=options['history_days'],
        )
