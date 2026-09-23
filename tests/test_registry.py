# tests/test_registry.py
"""apps 适配器层离线回归：微信封装只做转调；registry 按 bundle id / 名字分发。

Run: uv run python -B -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class WeChatAppTests(unittest.TestCase):
    def test_identity(self):
        from apps.wechat import WeChatApp
        app = WeChatApp()
        self.assertEqual(app.key, 'wechat')
        self.assertEqual(app.display_name, '微信')
        self.assertIn('com.tencent.xinWeChat', app.bundle_ids)
        self.assertIn('微信', app.app_names)
        self.assertTrue(app.needs_screen_capture)

    def test_delegates_to_perception_and_fill(self):
        from apps.wechat import WeChatApp
        app = WeChatApp()
        with patch('perception.find_wechat_window', return_value='win') as fw, \
             patch('perception.read_conversation', return_value={'ok': True}) as rc, \
             patch('fill.locate_input', return_value={'box': None}) as li, \
             patch('fill.fill_text', return_value=(True, '已填入')) as ft, \
             patch('perception.warm_ocr', return_value=12.0) as wo:
            self.assertEqual(app.find_window(7), 'win')
            fw.assert_called_once_with(7)
            self.assertEqual(app.read_conversation(previous_wid=1, prev_fingerprint=b'x', prev_layout=(1, 2, 3)),
                             {'ok': True})
            rc.assert_called_once_with(previous_wid=1, prev_fingerprint=b'x', prev_layout=(1, 2, 3))
            self.assertEqual(app.locate_input({'wid': 1}), {'box': None})
            li.assert_called_once_with({'wid': 1})
            self.assertEqual(app.fill_text('hi', target={'box': 'b'}), (True, '已填入'))
            ft.assert_called_once_with('hi', target={'box': 'b'})
            self.assertEqual(app.warm(), 12.0)
            wo.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
