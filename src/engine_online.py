# -*- coding: utf-8 -*-
"""在线翻译引擎 —— 搜狗翻译 Web 接口。

零 GUI 依赖的同步实现：调用即返回译文，失败抛异常。
逻辑与 main.py 中的 TranslateWorker 完全一致，只是把「线程 + 信号」
剥离出去，交给调用方决定用线程、协程还是别的什么去跑。
"""

import json
import urllib.parse
import urllib.request

from .languages import LANG_AUTO, LANGUAGES, lang_names, online_code, online_targets

# 语言表统一由 languages.py 维护（在线 21 种，实测校准）。这里保留旧名字，
# 方便别处继续按 `LANG_MAP[...]` / `LANG_NAMES` 引用。
LANG_MAP = {name: code for name, code, _ in LANGUAGES if code}
LANG_MAP[LANG_AUTO] = 'auto'

LANG_NAMES = lang_names('online')
TARGET_NAMES = online_targets()

_UA = ('Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) '
       'AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/120.0.0.0 Mobile Safari/537.36')


def lang_code(name):
    """界面语言名 -> 搜狗语言码。不支持的语种回落到自动识别。"""
    return online_code(name) or 'auto'


class OnlineEngine:
    """搜狗翻译（同步）。"""

    def __init__(self, timeout=15):
        self.timeout = timeout

    def translate(self, text, from_lang='auto', to_lang='en'):
        text = (text or '').strip()
        if not text:
            return ''

        encoded = urllib.parse.quote(text)
        url = (f'https://fanyi.sogou.com/text?keyword={encoded}'
               f'&transfrom={from_lang}&transto={to_lang}')
        req = urllib.request.Request(url, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            html = resp.read().decode('utf-8')

        marker = '__INITIAL_STATE__='
        start = html.find(marker)
        if start == -1:
            raise RuntimeError('未找到翻译数据')

        brace_start = html.find('{', start)
        if brace_start == -1:
            raise RuntimeError('未找到 JSON 起始')

        depth, i = 0, brace_start
        while i < len(html):
            c = html[i]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1

        data = json.loads(html[brace_start:i + 1])
        return data['textTranslate']['translateData']['translate']['dit']
