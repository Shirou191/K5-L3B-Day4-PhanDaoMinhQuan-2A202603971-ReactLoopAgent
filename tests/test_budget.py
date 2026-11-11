"""Tests cho budget guard ở Bài 4: agent phải kết luận trong giới hạn max_steps.

Lỗi gốc (case H8 trong eval_before.json): model liên tục gọi get_incident_details
cho tới khi hết max_steps và trả answer rỗng -> status max_steps -> rubric tính 0.
GreedyProvider dưới đây mô phỏng đúng hành vi đó: nó chỉ chịu kết luận khi không
còn tools để gọi. Đây là test double, không đánh giá năng lực model thật.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import run_agent
from status_tools import StatusTools
from test_lab import final, tool_call


class GreedyProvider:
    """Luôn gọi thêm tool khi còn schemas; chỉ trả lời khi schemas rỗng."""

    def __init__(self, answer='Không thể bảo đảm đầy đủ: tool chỉ thấy 50 incident gần nhất.'):
        self.answer, self.calls = answer, 0
        self.schema_log, self.histories = [], []

    def complete(self, messages, schemas):
        self.calls += 1
        self.schema_log.append(bool(schemas))
        self.histories.append(list(messages))
        if schemas:
            return tool_call(f'c{self.calls}', 'get_status', '{"provider":"github"}')
        return final(self.answer)


class BudgetGuardTests(unittest.TestCase):
    def test_last_step_disables_tools_and_forces_an_answer(self):
        fake = GreedyProvider()
        result = run_agent('Liệt kê toàn bộ incident 2020-2026', fake, StatusTools(), max_steps=4)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['answer'])
        # Lượt cuối được gọi mà không có tools.
        self.assertEqual(fake.schema_log, [True, True, True, False])
        self.assertTrue(any(x['event'] == 'budget_final_step' for x in result['trace']))

    def test_budget_warning_fires_one_step_before_the_limit(self):
        result = run_agent('x', GreedyProvider(), StatusTools(), max_steps=4)
        warnings = [x for x in result['trace'] if x['event'] == 'budget_warning']
        self.assertEqual([x['step'] for x in warnings], [3])
        self.assertEqual(warnings[0]['steps_left'], 1)

    def test_single_step_budget_still_reports_max_steps(self):
        # max_steps=1 không còn lượt nào để kết luận: vẫn phải báo max_steps, không bịa.
        result = run_agent('x', GreedyProvider(), StatusTools(), max_steps=1)
        self.assertEqual(result['status'], 'max_steps')

    def test_tool_results_still_reach_the_model_on_the_forced_last_step(self):
        fake = GreedyProvider()
        run_agent('x', fake, StatusTools(), max_steps=3)
        self.assertEqual(fake.schema_log, [True, True, False])
        # Lượt cuối vẫn thấy tool result của các step trước, chỉ mất quyền gọi tool.
        last_history = fake.histories[-1]
        self.assertEqual(sum(1 for m in last_history if m.get('role') == 'tool'), 2)
        self.assertEqual(last_history[-1]['role'], 'user')
        self.assertIn('CUỐI CÙNG', last_history[-1]['content'])


if __name__ == '__main__':
    unittest.main()
