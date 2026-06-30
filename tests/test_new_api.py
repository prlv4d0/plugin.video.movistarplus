import base64
import json
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")

    class RequestsSession:
        headers = {}

        def __init__(self, *args, **kwargs):
            self.headers = {}

    class HTTPAdapter:
        pass

    class PoolManager:
        def __init__(self, *args, **kwargs):
            pass

    requests_stub.Session = RequestsSession
    adapters_stub = types.ModuleType("requests.adapters")
    adapters_stub.HTTPAdapter = HTTPAdapter
    packages_stub = types.SimpleNamespace(
        urllib3=types.SimpleNamespace(
            poolmanager=types.SimpleNamespace(PoolManager=PoolManager)
        )
    )
    requests_stub.adapters = adapters_stub
    requests_stub.packages = packages_stub
    sys.modules["requests"] = requests_stub
    sys.modules["requests.adapters"] = adapters_stub
    sys.modules["requests.packages"] = packages_stub
    sys.modules["requests.packages.urllib3"] = packages_stub.urllib3
    sys.modules["requests.packages.urllib3.poolmanager"] = packages_stub.urllib3.poolmanager

if "pytz" not in sys.modules:
    pytz_stub = types.ModuleType("pytz")

    class Timezone:
        def localize(self, value):
            return value

    pytz_stub.timezone = lambda name: Timezone()
    sys.modules["pytz"] = pytz_stub

if "dateutil" not in sys.modules:
    dateutil_stub = types.ModuleType("dateutil")
    parser_stub = types.ModuleType("dateutil.parser")
    parser_stub.parse = lambda value: __import__("datetime").datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ) if value else __import__("datetime").datetime.now()
    dateutil_stub.parser = parser_stub
    sys.modules["dateutil"] = dateutil_stub
    sys.modules["dateutil.parser"] = parser_stub

from resources.lib import movistar_auth
from resources.lib.movistar import Movistar


class Response:
    def __init__(self, text):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = 200

    def raise_for_status(self):
        return None


class Session:
    def __init__(self, text):
        self.text = text

    def get(self, url, timeout=None, allow_redirects=True):
        return Response(self.text)


class NewApiTests(unittest.TestCase):
    def test_signature_is_deterministic_with_nonce_and_timestamp(self):
        sig1 = movistar_auth.get_signature(
            "POST",
            "https://example.test/service?deviceClass=webplayer",
            "token",
            "Bearer",
            "consumer",
            "secret",
            params={"username": "user@example.test"},
            nonce="abc",
            timestamp="123",
        )
        sig2 = movistar_auth.get_signature(
            "POST",
            "https://example.test/service?deviceClass=webplayer",
            "token",
            "Bearer",
            "consumer",
            "secret",
            params={"username": "user@example.test"},
            nonce="abc",
            timestamp="123",
        )
        self.assertEqual(sig1, sig2)
        decoded = base64.b64decode(sig1).decode("utf-8")
        self.assertIn('consumer_key="consumer"', decoded)
        self.assertIn('nonce="abc"', decoded)
        self.assertIn('timestamp="123"', decoded)

    def test_signed_headers_include_opplus_authorization(self):
        headers = movistar_auth.signed_headers(
            "GET",
            "https://example.test/service",
            "abc",
            "Bearer",
            "consumer",
            "secret",
            {"Accept": "application/json"},
        )
        self.assertEqual(headers["Accept"], "application/json")
        self.assertTrue(headers["Authorization"].startswith("OPPlus Bearer=abc,Signature="))

    def test_decode_jwt_and_expiration(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1, "did": "dev"}).encode()).decode().rstrip("=")
        token = "header.{}.sig".format(payload)
        self.assertEqual(movistar_auth.decode_jwt(token)["did"], "dev")
        self.assertTrue(movistar_auth.token_expired(token))

    def test_fetch_webplayer_credentials_parses_conf(self):
        old_decrypt = movistar_auth.decrypt_ua
        try:
            movistar_auth.decrypt_ua = lambda segments, password: "consumer-key" if segments == ["a"] else "consumer-secret"
            conf = (
                'yomvi.setupConf({"config":{"version":"1","name":"n",'
                '"timestamp":"2","language":"es","ua":{"query":"[\\"a\\"]",'
                '"param":"[\\"b\\"]"}}});'
            )
            key, secret = movistar_auth.fetch_webplayer_credentials(Session(conf))
            self.assertEqual(key, "consumer-key")
            self.assertEqual(secret, "consumer-secret")
        finally:
            movistar_auth.decrypt_ua = old_decrypt

    def test_normalize_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            movistar = Movistar(tmp + os.sep)
            devices = movistar.normalize_devices({
                "devices": [{
                    "Id": "dev1",
                    "Name": "Web",
                    "DeviceType": "Web",
                    "DeviceTypeCode": "WP_DASH",
                    "ContentPlaying": "",
                    "IsInSsp": True,
                }]
            })
        self.assertEqual(devices[0]["id"], "dev1")
        self.assertEqual(devices[0]["type_code"], "WP_DASH")
        self.assertTrue(devices[0]["in_ssp"])


if __name__ == "__main__":
    unittest.main()
