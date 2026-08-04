from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run automated OPD smoke / E2E tests (full workflow + analytics + lab + follow-up)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--keepdb',
            action='store_true',
            help='Reuse the test database between runs',
        )

    def handle(self, *args, **options):
        labels = [
            'core.tests.test_e2e_workflow',
            'core.tests.test_analytics',
            'core.tests.test_lab_flow',
            'core.tests.test_followup',
        ]
        argv = ['test', *labels, '--verbosity', '2']
        if options['keepdb']:
            argv.append('--keepdb')
        self.stdout.write('Running OPD automated test suite...\n')
        call_command(*argv)
