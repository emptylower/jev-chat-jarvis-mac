# tests/test_qq_adapter.py
"""QQ 适配器离线回归：内存伪 AX 树，不读屏、不碰 QQ 进程、不需要权限。

节点形状按 2026-09-23 对 QQ 6.9.96 的实测（见 specs/2026-09-23-qq-adapter-design.md 第 2 节）。
Run: uv run python -B -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from apps import qq


class N:
    """一个伪 AX 节点。rect 为屏幕坐标 (x, y, w, h)。"""
    def __init__(self, role='AXGroup', desc='', value='', title='', classes=(), rect=None, children=()):
        self.role, self.desc, self.value, self.title = role, desc, value, title
        self.classes, self.rect, self.children = tuple(classes), rect, list(children)


class FakeAX:
    def __init__(self, windows=(), focused=None):
        self._windows, self._focused = list(windows), focused
    def role(self, el): return el.role
    def desc(self, el): return el.desc
    def value(self, el): return el.value
    def title(self, el): return el.title
    def classes(self, el): return el.classes
    def rect(self, el): return el.rect
    def children(self, el): return el.children
    def windows(self, app_el): return self._windows
    def focused_window(self, app_el): return self._focused


WIN = (311.0, 143.0, 1106.0, 782.0)


def editor(desc='很难约的王小姐', value='\n'):
    return N('AXTextArea', desc=desc, value=value, classes=('ProseMirror', 'is-empty', 'ExEditor-qq-msg-editor'),
             rect=(311.0, 753.0, 1098.0, 169.0))


def message(text, side, y, sender=None, h=38.0, x=None, image_only=False):
    """一行消息：头像节点在前、正文容器在后（与真实 DFS 顺序一致）。"""
    x = x if x is not None else (1200.0 if side == 'me' else 400.0)
    cls = ('msg-content-container', 'mix-message__container') + (('container--self',) if side == 'me' else ())
    body = (N('AXImage', classes=('image', 'market-face-element'), rect=(x, y, 150.0, h)) if image_only
            else N('AXStaticText', value=text, rect=(x + 1, y + 1, 140.0, 16.0)))
    return [
        N(desc=sender or '', classes=('avatar-span',), rect=(1361.0 if side == 'me' else 340.0, y, 32.0, 32.0)),
        N(classes=('message-content__wrapper',), rect=(x, y, 160.0, h), children=[
            N(classes=cls, rect=(x, y, 160.0, h), children=[
                N(classes=('message-content', 'mix-message__inner'), rect=(x + 1, y + 1, 140.0, 22.0), children=[body]),
            ]),
        ]),
    ]


def chat_window(rows, title='很难约的王小…', ed=None):
    ed = ed or editor()
    return N('AXWindow', title=title, rect=WIN, children=[
        N(classes=('aio',), rect=WIN, children=[
            N('AXStaticText', value='星期一 17:43', rect=(830.0, 237.0, 71.0, 14.0)),
            *[n for row in rows for n in row],
            N(classes=('chat-input-area',), rect=(311.0, 753.0, 1098.0, 169.0), children=[ed]),
        ]),
    ])


def list_window():
    """紧凑模式的会话列表窗口：没有编辑器。"""
    return N('AXWindow', title='QQ', rect=(599.0, 260.0, 369.0, 580.0), children=[
        N(desc='会话列表', rect=(647.0, 403.0, 317.0, 433.0), children=[
            N('AXStaticText', value='杀戮尖塔pdd', rect=(711.0, 417.0, 82.0, 16.0)),
        ]),
    ])


class ParseTests(unittest.TestCase):
    def test_find_editor_only_in_chat_window(self):
        ax = FakeAX()
        self.assertIsNotNone(qq.find_editor(ax, chat_window([])))
        self.assertIsNone(qq.find_editor(ax, list_window()))

    def test_title_prefers_editor_description_then_window_title(self):
        ax = FakeAX()
        win = chat_window([])
        self.assertEqual(qq.chat_title(ax, win, qq.find_editor(ax, win)), '很难约的王小姐')
        win2 = chat_window([], ed=editor(desc=''))
        self.assertEqual(qq.chat_title(ax, win2, qq.find_editor(ax, win2)), '很难约的王小…')

    def test_sides_text_and_order(self):
        ax = FakeAX()
        win = chat_window([message('在吗', 'them', 300.0, sender='王小姐'),
                           message('在的', 'me', 360.0, sender='西行树'),
                           message('周三有空吗', 'them', 420.0, sender='王小姐')])
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win))
        self.assertEqual([(m.side, m.text, m.sender) for m in msgs],
                         [('them', '在吗', '王小姐'), ('me', '在的', '西行树'), ('them', '周三有空吗', '王小姐')])
        self.assertTrue(all(0.0 <= m.y < 1.0 and 0.0 < m.h < 1.0 and 0.0 <= m.x < 1.0 for m in msgs))
        self.assertLess(msgs[0].y, msgs[1].y)
        self.assertEqual(msgs[0].lines, ['在吗'])
        self.assertEqual(msgs[0].conf, 1.0)

    def test_image_only_message_is_skipped(self):
        ax = FakeAX()
        win = chat_window([message('', 'them', 300.0, image_only=True), message('好', 'me', 360.0)])
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win))
        self.assertEqual([m.text for m in msgs], ['好'])

    def test_virtualized_and_offscreen_nodes_are_dropped(self):
        ax = FakeAX()
        rows = [message('滚出去的旧消息', 'them', 237.0, h=1.0),           # 高度 1：虚拟列表占位
                message('窗口外', 'them', 20.0),                          # y 在窗口顶边（143）之上
                message('可见', 'them', 420.0)]
        win = chat_window(rows)
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win))
        self.assertEqual([m.text for m in msgs], ['可见'])

    def test_node_overlapping_editor_is_dropped(self):
        ax = FakeAX()
        win = chat_window([message('草稿回显', 'me', 800.0), message('正文', 'them', 420.0)])
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win))
        self.assertEqual([m.text for m in msgs], ['正文'])

    def test_keeps_only_newest_max_messages(self):
        ax = FakeAX()
        win = chat_window([message(f'第{i}条', 'them', 250.0 + i * 30.0) for i in range(15)])   # 最后一条 670+38 仍在编辑器（y≥753）之上
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win), max_messages=12)
        self.assertEqual(len(msgs), 12)
        self.assertEqual(msgs[0].text, '第3条')
        self.assertEqual(msgs[-1].text, '第14条')

    def test_multiple_static_texts_join_in_x_order(self):
        ax = FakeAX()
        row = message('', 'them', 300.0)
        content = row[1].children[0].children[0]
        content.children = [N('AXStaticText', value=' 复活吧', rect=(560.0, 301.0, 40.0, 16.0)),
                            N('AXStaticText', value='@缓缓', rect=(401.0, 301.0, 60.0, 16.0))]
        win = chat_window([row])
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win))
        self.assertEqual(msgs[0].text, '@缓缓 复活吧')

    def test_fingerprint_tracks_title_and_messages(self):
        ax = FakeAX()
        win = chat_window([message('在吗', 'them', 300.0)])
        msgs = qq.extract_messages(ax, win, qq.find_editor(ax, win))
        a = qq.fingerprint('王小姐', msgs)
        self.assertEqual(a, qq.fingerprint('王小姐', msgs))
        self.assertNotEqual(a, qq.fingerprint('李经理', msgs))
        win2 = chat_window([message('在吗', 'them', 300.0), message('在', 'me', 360.0)])
        self.assertNotEqual(a, qq.fingerprint('王小姐', qq.extract_messages(ax, win2, qq.find_editor(ax, win2))))


if __name__ == '__main__':
    unittest.main()
