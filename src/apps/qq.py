"""QQ 适配器：通过系统无障碍（AX）树读 QQNT（Electron）聊天窗口，不截图、不 OCR。

实测事实（2026-09-23，QQ 6.9.96）见 docs/superpowers/specs/2026-09-23-qq-adapter-design.md
第 2 节：Electron 在设 AXManualAccessibility 后暴露完整树并附带 AXDOMClassList；消息容器
class 含 msg-content-container，我方另含 container--self；输入框是 class 含
ExEditor-qq-msg-editor 的 AXTextArea，其 AXDescription 是未截断的联系人名。

纯读：唯一写动作是「填入」（Task 6），AX 设值优先、后备为键盘事件；不发送、不用剪贴板。
解析函数全部经由一个读取器对象访问 AX（AXReader），测试用内存树替换。
"""
from __future__ import annotations

import hashlib

import ApplicationServices as AS

import fill
from perception import Message

KEY = "qq"
DISPLAY_NAME = "QQ"
BUNDLE_IDS = ("com.tencent.qq",)
APP_NAMES = ("QQ",)

EDITOR_CLASS = "ExEditor-qq-msg-editor"
MSG_CLASS = "msg-content-container"
SELF_CLASS = "container--self"
AVATAR_CLASS = "avatar-span"
MAX_NODES = 3000        # 一次遍历的节点上限：一个繁忙群聊每条可见消息约 8 个节点
MAX_MESSAGES = 12


class AXReader:
    """AX 属性读取的最小接口；测试用同名方法的内存树替换（tests/test_qq_adapter.py）。"""

    def _str(self, el, name) -> str:
        v = fill._ax_attr(el, name)
        return v if isinstance(v, str) else ""

    def role(self, el) -> str:
        return self._str(el, AS.kAXRoleAttribute)

    def desc(self, el) -> str:
        return self._str(el, AS.kAXDescriptionAttribute)

    def value(self, el) -> str:
        return self._str(el, AS.kAXValueAttribute)

    def title(self, el) -> str:
        return self._str(el, AS.kAXTitleAttribute)

    def classes(self, el) -> tuple[str, ...]:
        v = fill._ax_attr(el, "AXDOMClassList")
        try:
            return tuple(str(c) for c in (v or ()))
        except TypeError:
            return ()

    def rect(self, el):
        return fill._ax_rect(el)

    def children(self, el) -> list:
        v = fill._ax_attr(el, AS.kAXChildrenAttribute)
        try:
            return list(v or [])
        except TypeError:
            return []

    def windows(self, app_el) -> list:
        v = fill._ax_attr(app_el, AS.kAXWindowsAttribute)
        try:
            return list(v or [])
        except TypeError:
            return []

    def focused_window(self, app_el):
        return fill._ax_attr(app_el, AS.kAXFocusedWindowAttribute)


# ------------------------------------------------------------------ 纯解析

def walk(ax, root, limit: int = MAX_NODES):
    """有界深度优先遍历。DFS 顺序保证同一行里头像节点先于正文容器出现。"""
    stack = [root]
    seen = 0
    while stack and seen < limit:
        el = stack.pop()
        seen += 1
        yield el
        stack.extend(reversed(ax.children(el)))


def find_editor(ax, window):
    """聊天窗口的消息编辑器，或 None（会话列表窗口没有）。"""
    for el in walk(ax, window):
        if ax.role(el) == "AXTextArea" and EDITOR_CLASS in ax.classes(el):
            return el
    return None


def chat_title(ax, window, editor) -> str:
    """编辑器描述是未截断的联系人名；窗口标题（会截断成「…」）只做兜底。"""
    title = ax.desc(editor).strip() if editor is not None else ""
    return title or ax.title(window).strip()


def _intersects(a, b) -> bool:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    return ax0 < bx0 + bw and bx0 < ax0 + aw and ay0 < by0 + bh and by0 < ay0 + ah


def _inside(inner, outer, slack: float = 3.0) -> bool:
    x, y, w, h = inner
    ox, oy, ow, oh = outer
    return (x >= ox - slack and y >= oy - slack
            and x + w <= ox + ow + slack and y + h <= oy + oh + slack)


def _texts(ax, container) -> str:
    """容器里全部 AXStaticText 按 x 顺序拼接；纯图 / 表情包没有文字，返回空串。"""
    parts = []
    for el in walk(ax, container, limit=200):
        if ax.role(el) == "AXStaticText":
            t = ax.value(el)
            if t.strip():
                r = ax.rect(el)
                parts.append((r[0] if r else 0.0, t))
    parts.sort(key=lambda p: p[0])
    return "".join(t for _, t in parts).strip()


def extract_messages(ax, window, editor, max_messages: int = MAX_MESSAGES) -> list[Message]:
    """窗口子树 → 有序消息列表（底部最新）。坐标按窗口归一化、顶部原点，与 OCR 路径口径一致。"""
    win_rect = ax.rect(window)
    if win_rect is None:
        return []
    ed_rect = ax.rect(editor) if editor is not None else None
    wx, wy, ww, wh = win_rect
    out: list[Message] = []
    sender = None
    for el in walk(ax, window):
        cls = ax.classes(el)
        if AVATAR_CLASS in cls:
            sender = ax.desc(el).strip() or None
            continue
        if MSG_CLASS not in cls:
            continue
        this_sender, sender = sender, None
        r = ax.rect(el)
        if (r is None or r[3] <= 1 or not _inside(r, win_rect)
                or (ed_rect is not None and _intersects(r, ed_rect))):
            continue
        text = _texts(ax, el)
        if not text:
            continue
        x, y, w, h = r
        ny = (y - wy) / wh
        out.append(Message(text=text, side="me" if SELF_CLASS in cls else "them",
                           y=ny, conf=1.0, h=h / wh, sender=this_sender, lines=[text],
                           x=(x - wx) / ww, w=w / ww, last_y=ny))
    out.sort(key=lambda m: m.y)
    return out[-max_messages:]


def fingerprint(title: str, msgs: list[Message]) -> bytes:
    """(标题, [(方向, 正文)…]) 的摘要，替代像素指纹做 unchanged 短路。"""
    h = hashlib.sha1(title.encode("utf-8"))
    for m in msgs:
        h.update(b"\n" + m.side.encode("ascii") + b"\t" + m.text.encode("utf-8"))
    return h.digest()


class QQApp:
    key = KEY
    display_name = DISPLAY_NAME
    bundle_ids = BUNDLE_IDS
    app_names = APP_NAMES
    needs_screen_capture = False
