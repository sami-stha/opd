"""Discover which UAT secret matches eSewa published signature example."""
import base64
import hashlib
import hmac
import unittest

OFFICIAL_SIG = 'i94zsd3oXF6ZsSr/kGqT4sSzYQzjj1W/waxjWyRwaME='
MESSAGE = 'total_amount=110,transaction_uuid=241028,product_code=EPAYTEST'
CANDIDATES = [
    '8gBm/:&EnhH.1/q',
    '8gBm/:&EnhH.1/q(',
    '8gBm/:&EnhH.1/q)',
]


def sign(secret, message):
    digest = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).digest()
    return base64.b64encode(digest).decode('utf-8')


class EsewaSecretKeyTests(unittest.TestCase):
    def test_official_example_matches_uat_secret_without_paren(self):
        self.assertEqual(sign('8gBm/:&EnhH.1/q', MESSAGE), OFFICIAL_SIG)
        self.assertNotEqual(sign('8gBm/:&EnhH.1/q(', MESSAGE), OFFICIAL_SIG)
