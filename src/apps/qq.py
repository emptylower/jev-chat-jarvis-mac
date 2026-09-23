"""QQ 适配器骨架：Task 4 填充实现。"""
from __future__ import annotations

KEY = "qq"
DISPLAY_NAME = "QQ"
BUNDLE_IDS = ("com.tencent.qq",)
APP_NAMES = ("QQ",)


class QQApp:
    key = KEY
    display_name = DISPLAY_NAME
    bundle_ids = BUNDLE_IDS
    app_names = APP_NAMES
    needs_screen_capture = False
