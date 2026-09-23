"""Custom-tone loading gate (JEV_TONES quality floor). Offline, no screen, no API."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import styles


class CustomToneTests(unittest.TestCase):
    def setUp(self):
        styles.REJECTED_TONES.clear()

    def tearDown(self):
        styles.REJECTED_TONES.clear()

    def test_short_description_is_refused_with_reason(self):
        with patch.object(styles.userconfig, 'get',
                          return_value='夸我=夸我|好的=嗯'):
            out = styles._custom_tones()
        self.assertNotIn('夸我', out)
        self.assertNotIn('好的', out)
        self.assertTrue(any('「夸我」说明仅 2 字' in r for r in styles.REJECTED_TONES))

    def test_well_formed_tone_loads_and_overrides_builtin(self):
        desc = '像个资深摸鱼选手，把活推得很得体又不失礼'
        with patch.object(styles.userconfig, 'get',
                          return_value=f'摸鱼大师={desc}|夸夸=夸夸群金牌群友，夸细节不空泛，别夸成阴阳怪气'):
            out = styles._custom_tones()
        self.assertEqual(out['摸鱼大师'], desc)
        self.assertIn('夸夸', out)          # 同名覆盖内置
        self.assertEqual(styles.REJECTED_TONES, [])

    def test_exactly_at_floor_loads(self):
        with patch.object(styles.userconfig, 'get',
                          return_value='测试语气=这一句刚好十个字了吗'):
            out = styles._custom_tones()
        self.assertIn('测试语气', out)
        self.assertEqual(styles.REJECTED_TONES, [])

    def test_empty_description_is_refused_not_dropped_silently(self):
        with patch.object(styles.userconfig, 'get', return_value='空说明='):
            out = styles._custom_tones()
        self.assertNotIn('空说明', out)
        self.assertEqual(len(styles.REJECTED_TONES), 1)


if __name__ == '__main__':
    unittest.main()
