"""POST a signed form to eSewa UAT and assert we get login HTML (not ES104)."""
import urllib.error
import urllib.parse
import urllib.request

from django.test import SimpleTestCase

from core.services.esewa_gateway import build_payment_form_fields, esewa_form_url


class EsewaLiveUatTests(SimpleTestCase):
    def test_uat_form_accepts_signature(self):
        fields = build_payment_form_fields(
            'OPD-live-test-241028',
            amount=100,
            tax_amount=0,
            service_charge=0,
            delivery_charge=0,
            total_amount=100,
        )
        body = urllib.parse.urlencode(fields).encode('utf-8')
        req = urllib.request.Request(
            esewa_form_url(),
            data=body,
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode('utf-8', errors='replace')
                status = resp.status
        except urllib.error.HTTPError as exc:
            html = exc.read().decode('utf-8', errors='replace')
            status = exc.code

        self.assertNotIn('ES104', html)
        self.assertNotIn('Invalid payload signature', html)
        # UAT should return payment page HTML, not JSON error
        self.assertNotIn('"code":"ES104"', html)
        if status == 200:
            self.assertTrue(
                'esewa' in html.lower() or 'login' in html.lower() or 'epay' in html.lower(),
                f'Unexpected UAT response (first 300 chars): {html[:300]}',
            )
