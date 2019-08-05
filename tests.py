# -*- coding: utf-8 -*-

from django.test import TestCase

from .rest.auth import HmacAuth


KEY = 'yJwU0chpercYs/R4YmCUxhbRZBHM4WqpO3ZH0ZW6+4X+/aTodSGTI2w5jeBxWgJXNN1JNQIg02Ic3ZnZtSEVYA=='


class FakeRequest(object):
    pass


class HmacTests(TestCase):
    KEY_ID = 'test.jbei.org'
    USER_ID = 'WCMorrell'

    def setUp(self):
        super(HmacTests, self).setUp()
        HmacAuth.register_key(self.KEY_ID, KEY)

    def test_signature_gen(self):
        request = FakeRequest()
        request.url = 'http://registry-test.jbei.org/rest/accesstoken'
        request.method = 'GET'
        request.headers = {}
        request.body = None
        auth = HmacAuth(self.KEY_ID, self.USER_ID)
        self.assertEqual(
            auth(request).headers['Authorization'],
            ':'.join(('1', self.KEY_ID, self.USER_ID, 'j7iHK4iYiELZlEtDWD8GJm04CWc='))
        )


class IceTests(TestCase):
    def test_entry_uri_pattern(self):
        from rest.clients.ice.api import ICE_ENTRY_URL_PATTERN

        # test matching against ICE URI's with a numeric ID
        uri = "https://registry-test.jbei.org/entry/49194/"
        match = ICE_ENTRY_URL_PATTERN.match(uri)
        self.assertEqual("https", match.group(1))
        self.assertEqual("registry-test.jbei.org", match.group(2))
        self.assertEqual("49194", match.group(3))

        # test matching against ICE URI's with a UUID
        uri = (
            "https://registry-test.jbei.org/entry/761ec36a-cd17-41b8-a348-45d7552d4f4f"
        )
        match = ICE_ENTRY_URL_PATTERN.match(uri)
        self.assertEqual("https", match.group(1))
        self.assertEqual("registry-test.jbei.org", match.group(2))
        self.assertEqual("761ec36a-cd17-41b8-a348-45d7552d4f4f", match.group(3))

        # verify non-match against invalid URLs
        uri = "ftp://registry.jbei.org/entry/12345"
        self.assertIsNone(ICE_ENTRY_URL_PATTERN.match(uri))
        uri = "http://registry.jbei.org/admin/12345"
        self.assertIsNone(ICE_ENTRY_URL_PATTERN.match(uri))
        uri = "http://registry.jbei.org/entry/12345/experiments"
        self.assertIsNone(ICE_ENTRY_URL_PATTERN.match(uri))
        uri = "http://registry.jbei.org/entry/foobar"
        self.assertIsNone(ICE_ENTRY_URL_PATTERN.match(uri))