"""Tests cho retry hạ tầng trong providers.post_json.

Bối cảnh: khi chạy eval thật, một blip DNS làm 8 case fail tức thì vì URLError
không được retry. Retry phải có trần cứng để không gọi vô hạn.
"""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers


class FakeResponse:
    def __init__(self, payload=b'{"ok": true}', headers=None):
        self.payload = payload
        self.headers = headers or {}

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, headers=None):
    # fp=BytesIO để HTTPError không mở tempfile -> không sinh ResourceWarning.
    return urllib.error.HTTPError('http://x', code, 'err', headers or {}, io.BytesIO(b''))


class RetryTests(unittest.TestCase):
    def call(self, side_effect):
        try:
            with patch('providers.time.sleep') as sleep, \
                 patch('urllib.request.urlopen', side_effect=side_effect) as open_:
                try:
                    result = providers.post_json('http://x', {}, {'a': 1})
                except RuntimeError as exc:
                    result = exc
            return result, open_.call_count, sleep.call_count
        finally:
            # Đóng HTTPError để không để lại file handle (ResourceWarning).
            for item in side_effect:
                if hasattr(item, 'close'):
                    item.close()

    def test_transient_dns_failure_is_retried_then_succeeds(self):
        result, calls, sleeps = self.call([urllib.error.URLError('getaddrinfo failed'),
                                           FakeResponse()])
        self.assertEqual(result, {'ok': True})
        self.assertEqual((calls, sleeps), (2, 1))

    def test_rate_limit_is_retried_then_succeeds(self):
        result, calls, sleeps = self.call([http_error(429), FakeResponse()])
        self.assertEqual(result, {'ok': True})
        self.assertEqual((calls, sleeps), (2, 1))

    def test_retry_has_a_hard_attempt_cap(self):
        result, calls, _ = self.call([http_error(429)] * 20)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(calls, providers.RETRY_ATTEMPTS)

    def test_client_errors_are_not_retried(self):
        for code in (400, 401, 404):
            with self.subTest(code=code):
                result, calls, _ = self.call([http_error(code), FakeResponse()])
                self.assertIsInstance(result, RuntimeError)
                self.assertEqual(calls, 1)

    def test_retry_after_header_is_respected(self):
        exc = http_error(429, {'Retry-After': '7'})
        try:
            with patch('providers.time.sleep') as sleep, \
                 patch('urllib.request.urlopen', side_effect=[exc, FakeResponse()]):
                providers.post_json('http://x', {}, {})
            self.assertEqual(sleep.call_args.args[0], 7.0)
        finally:
            exc.close()

    def test_retry_after_never_shortens_the_backoff(self):
        """Groq trả Retry-After ~2s cho giới hạn token/phút, nhưng bucket cần ~60s.

        Nghe theo con số nhỏ đó thì cả 8 lần thử cháy trong vài giây -> mất case.
        """
        exc = http_error(429, {'Retry-After': '2'})
        try:
            for attempt, floor in ((3, 8.0), (5, 32.0)):
                with self.subTest(attempt=attempt):
                    self.assertEqual(providers._retry_delay(exc, attempt), floor)
        finally:
            exc.close()

    def test_absurd_retry_after_fails_fast_instead_of_grinding(self):
        """Hết quota ngày: Groq xin chờ 349s. Thử lại 8 lượt chỉ tốn 7 phút rồi vẫn fail."""
        result, calls, sleeps = self.call([http_error(429, {'Retry-After': '349'})] * 3)
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('349s', str(result))
        self.assertEqual((calls, sleeps), (1, 0))

    def test_retry_after_within_cap_is_still_retried(self):
        result, calls, _ = self.call([http_error(429, {'Retry-After': '45'}), FakeResponse()])
        self.assertEqual(result, {'ok': True})
        self.assertEqual(calls, 2)

    def test_total_wait_covers_a_full_token_window(self):
        delays = [providers._retry_delay(None, n)
                  for n in range(1, providers.RETRY_ATTEMPTS + 1)]
        self.assertGreaterEqual(sum(delays), 60.0)
        self.assertLessEqual(sum(delays), providers.RETRY_BUDGET_SECONDS)

    def test_duration_header_formats_are_parsed(self):
        for text, want in (('7.66s', 7.66), ('2m59.56s', 179.56), ('500ms', 0.5),
                           ('1h', 3600.0), ('', None), ('abc', None), (None, None)):
            with self.subTest(text=text):
                self.assertEqual(providers._parse_duration(text), want)

    def test_low_token_budget_paces_before_returning(self):
        response = FakeResponse(headers={'x-ratelimit-remaining-tokens': '120',
                                         'x-ratelimit-reset-tokens': '30s'})
        with patch('providers.time.sleep') as sleep, \
             patch('urllib.request.urlopen', return_value=response):
            providers.post_json('http://x', {}, {})
        self.assertEqual(sleep.call_args.args[0], 31.0)

    def test_healthy_token_budget_does_not_pace(self):
        response = FakeResponse(headers={'x-ratelimit-remaining-tokens': '7900',
                                         'x-ratelimit-reset-tokens': '30s'})
        with patch('providers.time.sleep') as sleep, \
             patch('urllib.request.urlopen', return_value=response):
            providers.post_json('http://x', {}, {})
        sleep.assert_not_called()

    def test_missing_rate_limit_headers_do_not_pace(self):
        with patch('providers.time.sleep') as sleep, \
             patch('urllib.request.urlopen', return_value=FakeResponse()):
            self.assertEqual(providers.post_json('http://x', {}, {}), {'ok': True})
        sleep.assert_not_called()

    def test_user_agent_is_always_sent(self):
        with patch('urllib.request.urlopen', return_value=FakeResponse()) as open_:
            providers.post_json('http://x', {'Authorization': 'Bearer k'}, {})
        request = open_.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'), 'StatusAgentLab/1.0')


if __name__ == '__main__':
    unittest.main()
