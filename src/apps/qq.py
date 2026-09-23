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
import sys
import threading
import time
import unicodedata
from pathlib import Path

import AppKit
import ApplicationServices as AS
import Quartz

if __name__ == "__main__" and not __package__:   # CLI 自测：python src/apps/qq.py 时把 src/ 挂进 path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fill
from perception import Message, WindowInfo

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

ERR_NO_APP = "没找到 QQ 应用"
ERR_NO_WINDOW = "QQ 聊天窗口未找到"
ERR_EMPTY_TREE = "QQ 无障碍树为空，请重启 QQ 后重试"

REASON_NO_INPUT = "未取得可用的 QQ 输入控件"
REASON_DRAFT = "输入区已有草稿；请使用复制手动插入，避免改动现有内容"


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


# ------------------------------------------------------------------ 进程与窗口

def qq_app():
    """运行中的 QQ：bundle id 优先，显示名兜底；没有则 None。"""
    try:
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_IDS[0])
        if apps and len(apps) > 0:
            return apps[0]
    except Exception:
        pass
    try:
        for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            if (app.localizedName() or "") in APP_NAMES:
                return app
    except Exception:
        pass
    return None


_enabled_pids: set[int] = set()


def app_element(pid: int, force: bool = False):
    """QQ 进程的 AX 根元素。Electron 只有在设过 AXManualAccessibility 后才建完整树，
    每个 pid 设一次；树为空时调用方传 force=True 重设（不重启任何进程）。"""
    el = AS.AXUIElementCreateApplication(pid)
    if force or pid not in _enabled_pids:
        try:
            AS.AXUIElementSetAttributeValue(el, "AXManualAccessibility", True)
        except Exception:
            pass
        _enabled_pids.add(pid)
    return el


def chat_window(ax, app_el):
    """带消息编辑器的窗口：焦点窗口优先，否则面积最大。返回 (window, editor) 或 (None, None)。"""
    cands = []
    for w in ax.windows(app_el):
        ed = find_editor(ax, w)
        if ed is not None:
            cands.append((w, ed))
    if not cands:
        return None, None
    focused = ax.focused_window(app_el)
    if focused is not None:
        for w, ed in cands:
            if w == focused:
                return w, ed

    def area(pair):
        r = ax.rect(pair[0])
        return r[2] * r[3] if r else 0.0
    return max(cands, key=area)


def window_id(pid: int, rect) -> int:
    """按 pid + 几何在 CGWindowList 反查窗口 id；找不到返回 0，不阻断读消息。"""
    if rect is None:
        return 0
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    try:
        wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    except Exception:
        return 0
    for wi in wins:
        if int(wi.get("kCGWindowOwnerPID") or 0) != pid:
            continue
        b = dict(wi.get("kCGWindowBounds") or {})
        cand = (float(b.get("X", 0)), float(b.get("Y", 0)),
                float(b.get("Width", 0)), float(b.get("Height", 0)))
        if fill._same_rect(cand, tuple(rect)):
            return int(wi.get("kCGWindowNumber") or 0)
    return 0


def find_window(previous_wid=None, ax=None) -> WindowInfo | None:
    """当前 QQ 聊天窗口（previous_wid 只为接口对齐：焦点窗口优先于粘住旧窗口）。"""
    ax = ax or AXReader()
    app = qq_app()
    if app is None:
        return None
    pid = app.processIdentifier()
    win, editor = chat_window(ax, app_element(pid))
    if win is None:
        return None
    r = ax.rect(win)
    if r is None:
        return None
    return WindowInfo(wid=window_id(pid, r), pid=pid, title=chat_title(ax, win, editor),
                      x=r[0], y=r[1], w=r[2], h=r[3])


def read_conversation(max_messages: int = MAX_MESSAGES, previous_wid=None,
                      prev_fingerprint=None, prev_layout=None, ax=None) -> dict:
    """一次读取：找窗口 → 遍历 AX 树 → 消息。返回结构与 perception.read_conversation 同构。"""
    t0 = time.perf_counter()
    ax = ax or AXReader()
    if not fill.has_accessibility():
        return {"ok": False, "error": fill.REASON_NO_ACCESS, "messages": []}
    app = qq_app()
    if app is None:
        return {"ok": False, "error": ERR_NO_APP, "messages": []}
    pid = app.processIdentifier()
    app_el = app_element(pid)
    if not ax.windows(app_el):
        app_element(pid, force=True)      # 下一跳再试；不自动重启进程
        return {"ok": False, "error": ERR_EMPTY_TREE, "messages": []}
    win, editor = chat_window(ax, app_el)
    r = ax.rect(win) if win is not None else None
    if win is None or r is None:
        return {"ok": False, "error": ERR_NO_WINDOW, "messages": []}
    wid = window_id(pid, r)
    title = chat_title(ax, win, editor)
    window = {"wid": wid, "title": title, "x": r[0], "y": r[1], "w": r[2], "h": r[3]}
    ed_rect = ax.rect(editor)
    input_rect = tuple(ed_rect) if ed_rect else None
    layout = (wid, r[2], r[3])
    msgs = extract_messages(ax, win, editor, max_messages=max_messages)
    fp = fingerprint(title, msgs)
    total = (time.perf_counter() - t0) * 1000
    timing = {"capture": total, "ocr": 0.0, "total": total, "capture_path": "ax"}
    base = {"ok": True, "layout": layout, "input_rect": input_rect, "input_unresolved": False,
            "chat_title": title, "window": window, "fingerprint": fp, "timing_ms": timing}
    if layout == prev_layout and fp == prev_fingerprint:
        return dict(base, unchanged=True, messages=[], n_blocks=0)
    return dict(base, unchanged=False, messages=msgs, n_blocks=len(msgs))


# ------------------------------------------------------------------ 填入（唯一写动作）

def locate_input(win: dict, ax=None) -> dict:
    """只读定位：编辑器 AXTextArea 及其屏幕矩形。结构与 fill.locate_input 相同。"""
    result = {"box": None, "rect": None, "window": win, "reason": REASON_NO_INPUT}
    if not fill.has_accessibility():
        result["reason"] = fill.REASON_NO_ACCESS
        return result
    ax = ax or AXReader()
    app = qq_app()
    if app is None:
        result["reason"] = ERR_NO_APP
        return result
    _win, editor = chat_window(ax, app_element(app.processIdentifier()))
    if editor is None:
        return result
    rect = ax.rect(editor)
    if rect is None:
        result["reason"] = "输入控件坐标不可读取"
        return result
    bounds = tuple(float(win[k]) for k in ("x", "y", "w", "h"))
    if not _inside(rect, bounds):
        result["reason"] = "输入控件不在当前 QQ 窗口内"
        return result
    result.update(box=editor, rect=tuple(rect), reason="填入目标")
    return result


_FILL_LOCK = threading.Lock()
_LAST_FILL: tuple[str, str, float] | None = None   # (text, editor content after fill, ts)


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", s or "") if not c.isspace())


def _landed(current: str, text: str) -> bool:
    return bool(current) and _norm(text) in _norm(current)


def fill_text(text: str, target=None, ax=None) -> tuple[bool, str]:
    """把候选写进 QQ 输入框：AX 设值优先并读回校验；ProseMirror 拒收时退到键盘事件。
    不发送、不用剪贴板、失败不自动重试。"""
    global _LAST_FILL
    text = (text or "").strip()
    if not text:
        return False, fill.REASON_EMPTY
    if not _FILL_LOCK.acquire(blocking=False):
        return False, fill.REASON_BUSY
    try:
        if not fill.has_accessibility():
            return False, fill.REASON_NO_ACCESS
        ax = ax or AXReader()
        app = qq_app()
        if app is None:
            return False, ERR_NO_APP
        if target is None or target.get("box") is None:
            return False, REASON_NO_INPUT
        fresh = locate_input(target["window"], ax=ax)
        editor = fresh["box"]
        if (editor is None or editor != target["box"]
                or not fill._same_rect(fresh["rect"], target["rect"])):
            return False, "输入目标已变化，请等检测框更新后重试"
        current = ax.value(editor)
        if fill._duplicate_blocked(text, current, _LAST_FILL, time.monotonic()):
            return False, fill.REASON_DUPLICATE
        base = current if current.strip() else ""     # 空编辑器读出 "\n"，不能当前缀
        if fill._ax_set_value(editor, base + text):
            landed = ax.value(editor)
            if _landed(landed, text):
                _LAST_FILL = (text, landed, time.monotonic())
                return True, "已填入"
        ok, reason = _type_text(text, editor, app, ax)
        if ok:
            _LAST_FILL = (text, ax.value(editor), time.monotonic())
        return ok, reason
    finally:
        _FILL_LOCK.release()


def _type_text(text: str, editor, app, ax) -> tuple[bool, str]:
    """键盘事件后备：AX 置焦编辑器、激活 QQ、按 20 字一段发 Unicode 键入事件，再读回校验。
    已有草稿时停止（不覆盖、不追加），永远不按回车。"""
    if ax.value(editor).strip():
        return False, REASON_DRAFT
    try:
        AS.AXUIElementSetAttributeValue(editor, AS.kAXFocusedAttribute, True)
    except Exception:
        pass
    app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    time.sleep(0.15)
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None or front.processIdentifier() != app.processIdentifier():
        return False, "QQ 没有获得焦点，请先点 QQ 输入区再重试"
    if not fill._ax_attr(editor, AS.kAXFocusedAttribute):
        return False, "输入框未获得焦点，请先点 QQ 输入区再重试"
    plain = text.replace("\n", " ").replace("\t", " ")
    for offset in range(0, len(plain), 20):
        chunk = plain[offset:offset + 20]
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventSetFlags(ev, 0)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)
    time.sleep(0.25)
    if _landed(ax.value(editor), plain):
        return True, "已填入（键盘输入，未发送）"
    return False, "已尝试输入，未能确认；请检查草稿，勿重复点击"


class QQApp:
    key = KEY
    display_name = DISPLAY_NAME
    bundle_ids = BUNDLE_IDS
    app_names = APP_NAMES
    needs_screen_capture = False

    def find_window(self, previous_wid=None):
        return find_window(previous_wid)

    def read_conversation(self, **kwargs):
        return read_conversation(**kwargs)

    def locate_input(self, win):
        return locate_input(win)

    def fill_text(self, text, target=None):
        return fill_text(text, target=target)

    def warm(self):
        return None      # AX 路径没有一次性加载


if __name__ == "__main__":
    res = read_conversation()
    if not res["ok"]:
        print("ERROR:", res["error"])
        raise SystemExit(1)
    w = res["window"]
    print(f"window wid={w['wid']} {w['w']:.0f}x{w['h']:.0f} title={res['chat_title']!r} "
          f"ax={res['timing_ms']['total']:.0f}ms n={res['n_blocks']}")
    print("--- messages (top to bottom) ---")
    for m in res["messages"]:
        print(f"  [{m.side:4s}] y={m.y:.3f} sender={m.sender!r} | {m.text}")
