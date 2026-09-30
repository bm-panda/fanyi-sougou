# -*- coding: utf-8 -*-
"""脚本设置持久化。

用 QSettings 写进注册表（HKCU\\Software\\<组织>\\<应用>），
而不是在脚本目录写文件 —— 脚本重装时目录会被覆盖，配置会跟着丢。
"""

from PySide6.QtCore import QSettings

ORG = "瞎忙软件开发工作室"
APP = "不忙翻译"

ONLINE = "online"
OFFLINE = "offline"
_VALID = (ONLINE, OFFLINE)

_KEY_ENGINE = "engine"


class AppSettings:
    """极简设置读写；目前只存「上次用的引擎」。"""

    def __init__(self):
        self._s = QSettings(ORG, APP)

    @property
    def engine(self) -> str:
        """上次使用的引擎；缺失或非法时回落到在线。"""
        value = self._s.value(_KEY_ENGINE, ONLINE)
        return value if value in _VALID else ONLINE

    @engine.setter
    def engine(self, value: str) -> None:
        if value in _VALID:
            self._s.setValue(_KEY_ENGINE, value)
            self._s.sync()

    def reset(self) -> None:
        self._s.clear()
        self._s.sync()
