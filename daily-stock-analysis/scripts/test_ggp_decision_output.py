"""Decision evidence must survive validation and human rendering."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.render_ggp_output import render
from tools.validate_ggp_output import validate


class DecisionOutputTests(unittest.TestCase):
    def setUp(self):
        prompt = (ROOT / 'docs/ggp_prompt.md').read_text()
        self.obj = json.loads(prompt.split('```json\n', 1)[1].split('```', 1)[0])
        self.candidate = {
            'code': '000001', 'name': '测试标的', 'source': 'state_machine',
            'classification': 'real_open', 'signal_type': 'absolute',
            'current': {'price': 10, 'change_pct': 1, 'pullback_pct': 0.3, 'vwap_status': 'near'},
            'capital': {'main_net_wan': 8000, 'main_pct': 10, 'flow_5m_wan': 800, 'super_net_wan': 6000, 'dominance_type': 'absolute'},
            'risk': {'announcement': 'clean', 'vetoes': []},
            'buy_plan': {'buy_low': 9.99, 'buy_high': 10.01, 'stop': 9.7, 'take_profit': 10.2, 't1_action': '次日开盘先核验止损，09:45常规退出'},
            'blockers': [],
            'support': {
                'sector': '板块三只共振，资金排名第一',
                'capital': '主力8000万，超单6000万',
                'order_book': '分笔主买比1.8，五档承接已核验',
                'vwap': '均价10.00，买点贴近均价',
                'fundamentals': 'EPS为正，动态PE为正，YTD已核验',
                'simulation': '正式absolute，无前置模拟成交要求',
                'risk_reward': '目标与止损按计划核算，非保证收益',
            },
        }
        self.obj['decision'].update(overall='real_open', real_open=[self.candidate], fully_cash=False)

    def test_seven_support_fields_and_plan_are_visible(self):
        self.assertFalse(validate(self.obj))
        result = render(self.obj)
        for value in self.candidate['support'].values():
            self.assertIn(value, result)
        self.assertIn(self.candidate['buy_plan']['t1_action'], result)
        self.assertIn('止盈：10.2', result)

    def test_missing_support_or_plan_cannot_pass_as_real_open(self):
        for key in self.candidate['support']:
            with self.subTest(key=key):
                broken = copy.deepcopy(self.obj)
                broken['decision']['real_open'][0]['support'][key] = ''
                self.assertTrue(validate(broken))
        self.candidate['buy_plan']['t1_action'] = ''
        self.assertTrue(validate(self.obj))

    def test_veto_and_experimental_signal_cannot_enter_real_open(self):
        for status in ('avoid', 'unknown'):
            self.candidate['risk']['announcement'] = status
            self.assertTrue(validate(self.obj))
        self.candidate['risk']['announcement'] = 'clean'
        self.candidate['signal_type'] = 'coalition'
        self.assertTrue(validate(self.obj))

    def test_shadow_collection_is_not_simulated_buy(self):
        self.candidate.update(classification='simulated_buy', signal_type='divergence_leader')
        self.obj['decision'].update(overall='simulated_only', real_open=[], simulated_buy=[self.candidate])
        self.assertTrue(validate(self.obj))

    def test_conclusion_cannot_hide_real_candidate(self):
        self.obj['decision']['overall'] = 'fully_cash'
        self.assertTrue(validate(self.obj))

    def test_t1_execution_time_is_visible(self):
        self.obj['t1_plan'] = [{'code': '000001', 'name': '测试标的', 'priority': 'real', 'action': '核验风险', 'condition': '开盘低于止损', 'execute_at': '2026-09-09T09:30:00+08:00'}]
        self.assertFalse(validate(self.obj))
        self.assertIn('2026-09-09T09:30:00+08:00', render(self.obj))


if __name__ == '__main__':
    unittest.main()
