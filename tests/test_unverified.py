"""Tests cho cải tiến Bài 4: không bịa trạng thái của provider quan sát thất bại.

Lỗi gốc (case H7 trong eval_before.json): get_status cloudflare timeout, agent ghi
nhận timeout đúng rồi vẫn kết luận "Spectrum operational" vì người dùng yêu cầu như
vậy. Phát hiện ở đây dựa trên tool result (ok/error), không khớp chuỗi câu trả lời.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))
from agent import run_agent
from scenarios import ScenarioTools
from status_tools import StatusTools
from test_lab import FakeProvider, final, tool_call

CF = '{"provider":"cloudflare","component":"Spectrum"}'
GH = '{"provider":"github","component":"Actions"}'


def notices(provider_double):
    """Các message [Hệ thống] mà agent đã chèn vào context."""
    if not provider_double.histories:
        return []
    return [m['content'] for m in provider_double.histories[-1]
            if m.get('role') == 'user' and str(m.get('content', '')).startswith('[Hệ thống]')]


class UnverifiedProviderTests(unittest.TestCase):
    def test_failed_provider_is_announced_to_the_model(self):
        fake = FakeProvider([tool_call('1', 'get_status', CF), final('Không kiểm chứng được.')])
        result = run_agent('x', fake, ScenarioTools('cloudflare_timeout'), max_steps=4)
        events = [x for x in result['trace'] if x['event'] == 'unverified_provider']
        self.assertEqual([x['providers'] for x in events], [['cloudflare']])
        self.assertTrue(any('cloudflare' in n for n in notices(fake)))

    def test_successful_provider_is_not_announced(self):
        fake = FakeProvider([tool_call('1', 'get_status', GH), final('GitHub ổn.')])
        result = run_agent('x', fake, ScenarioTools('cloudflare_timeout'), max_steps=4)
        self.assertFalse([x for x in result['trace'] if x['event'] == 'unverified_provider'])
        self.assertEqual(notices(fake), [])

    def test_partial_failure_announces_only_the_failed_provider(self):
        both = tool_call('1', 'get_status', GH)
        other = tool_call('2', 'get_status', CF)
        both['calls'] += other['calls']
        both['message']['tool_calls'] += other['message']['tool_calls']
        fake = FakeProvider([both, final('GitHub ổn; Cloudflare chưa rõ.')])
        result = run_agent('x', fake, ScenarioTools('cloudflare_timeout'), max_steps=4)
        events = [x for x in result['trace'] if x['event'] == 'unverified_provider']
        self.assertEqual([x['providers'] for x in events], [['cloudflare']])

    def test_provider_is_announced_only_once(self):
        fake = FakeProvider([tool_call('1', 'get_status', CF), tool_call('2', 'get_status', CF),
                             final('Không kiểm chứng được.')])
        result = run_agent('x', fake, ScenarioTools('cloudflare_timeout'), max_steps=5)
        self.assertEqual(len([x for x in result['trace'] if x['event'] == 'unverified_provider']), 1)

    def test_later_success_clears_the_unverified_state(self):
        # Tool lỗi tạm thời rồi thành công: lần quan sát được không bị coi là thiếu dữ liệu.
        fake = FakeProvider([tool_call('1', 'get_status', GH), tool_call('2', 'get_status', GH),
                             final('GitHub ổn.')])
        tools = StatusTools()
        calls = {'n': 0}
        original = tools.get_status

        def flaky(provider, component=''):
            calls['n'] += 1
            if calls['n'] == 1:
                raise TimeoutError('timeout giả lập')
            return original(provider, component)

        tools.get_status = flaky
        result = run_agent('x', fake, tools, max_steps=5)
        events = [x for x in result['trace'] if x['event'] == 'unverified_provider']
        self.assertEqual([x['providers'] for x in events], [['github']])
        self.assertEqual(result['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
