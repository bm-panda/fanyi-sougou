import ctypes
import re
import sys
import json
import urllib.request
import urllib.parse
from pathlib import Path

from PySide2.QtCore import Qt, QPoint, QTimer, QThread, Signal
from PySide2.QtGui import QCursor
from PySide2.QtWidgets import QWidget, QApplication, QVBoxLayout
from xsideui import XWidget, XTextEdit, XCard, XComboBox, XPushButton, XButtonVariant, IconName, XColor

LANG_MAP = {
    '自动识别': 'auto',
    '中文': 'zh-CHS',
    '英文': 'en',
    '日语': 'ja',
    '韩语': 'ko',
    '法语': 'fr',
    '德语': 'de',
    '俄语': 'ru',
}

LANG_NAMES = ['自动识别', '中文', '英文', '日语', '韩语', '法语', '德语', '俄语']
TARGET_NAMES = LANG_NAMES[1:]


class TranslateWorker(QThread):
    result_ready = Signal(str, str)  # result_text, error_text

    def __init__(self, text, from_lang, to_lang):
        super().__init__()
        self.text = text
        self.from_lang = from_lang
        self.to_lang = to_lang

    def run(self):
        try:
            encoded = urllib.parse.quote(self.text)
            url = (f'https://fanyi.sogou.com/text?keyword={encoded}'
                   f'&transfrom={self.from_lang}&transto={self.to_lang}')
            req = urllib.request.Request(url, headers={
                'User-Agent': ('Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) '
                               'AppleWebKit/537.36 (KHTML, like Gecko) '
                               'Chrome/120.0.0.0 Mobile Safari/537.36')
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode('utf-8')

            marker = '__INITIAL_STATE__='
            start = html.find(marker)
            if start == -1:
                raise Exception('未找到翻译数据')

            brace_start = html.find('{', start)
            if brace_start == -1:
                raise Exception('未找到 JSON 起始')

            depth = 0
            i = brace_start
            while i < len(html):
                c = html[i]
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1

            json_str = html[brace_start:i + 1]
            data = json.loads(json_str)
            translated = data['textTranslate']['translateData']['translate']['dit']
            self.result_ready.emit(translated, '')
        except Exception as e:
            self.result_ready.emit('', f'翻译失败: {e}')


class FanYi(XWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._request_seq = 0
        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self.translate)
        self._skip_lang_change = False
        self._worker = None
        self._init_ui()
        self._handle_startup_args()
        self.setWindowFlag(Qt.WindowStaysOnTopHint)

    def _get_centered_pos(self) -> QPoint:
        screen = QApplication.screenAt(QCursor.pos())
        if not screen:
            screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            return QPoint(geo.right() - self.width(), geo.bottom() - self.height())
        return super()._get_centered_pos()

    def _init_ui(self):
        self.setMinimumWidth(360)
        self.hide_theme_button()
        self.hide_maximize_button()
        self.hide_minimize_button()
        self.set_title('不忙翻译')
        self.set_logo(str(Path(__file__).parent / 'images' / 'fanyi.png'))

        content = QWidget()
        self.addWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(11, 11, 11, 11)
        layout.setSpacing(11)

        card = XCard(spacing=20, padding=(11, 2, 11, 2))

        self.input_combox = XComboBox(border_visible=False)
        self.input_combox.addItems(LANG_NAMES)
        self.input_combox.setCurrentText('自动识别')
        self.input_combox.currentTextChanged.connect(self._on_lang_changed)

        self.icon_btn = XPushButton(
            variant=XButtonVariant.LINK,
            icon=IconName.SWITCH,
            color=XColor.TERTIARY
        )
        self.icon_btn.clicked.connect(self._swap_languages)

        self.output_combox = XComboBox(border_visible=False)
        self.output_combox.addItems(TARGET_NAMES)
        self.output_combox.setCurrentText('英文')
        self.output_combox.currentTextChanged.connect(self._on_lang_changed)

        card.addWidget(self.input_combox)
        card.addWidget(self.icon_btn)
        card.addWidget(self.output_combox)

        self.input = XTextEdit(placeholder='请输入翻译内容')
        self.input.textChanged.connect(self._on_input_changed)

        self.output = XTextEdit(placeholder='翻译结果')
        self.output.setReadOnly(True)

        layout.addWidget(card)
        layout.addWidget(self.input)
        layout.addWidget(self.output)

    def _handle_startup_args(self):
        if len(sys.argv) > 1:
            json_path = sys.argv[1]
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                keyword = data['data']['translate_text'][0]
                if keyword:
                    self.input.setPlainText(keyword)
                    self.translate()
            except Exception:
                pass

    def _on_input_changed(self):
        text = self.input.toPlainText().strip()
        if text:
            self._auto_switch_output_lang(text)
            self._debounce_timer.start(800)
        else:
            self.output.clear()

    def _detect_language(self, text):
        chinese = len(re.findall(r'[一-鿿]', text))
        english = len(re.findall(r'[a-zA-Z]', text))
        total = chinese + english
        if total == 0:
            return None
        ratio = chinese / total
        if ratio > 0.6:
            return 'zh'
        elif ratio < 0.4:
            return 'en'
        else:
            return None

    def _auto_switch_output_lang(self, text):
        if self.input_combox.currentText() != '自动识别':
            return
        detected = self._detect_language(text)
        if not detected:
            return
        target = '中文' if detected == 'en' else '英文'
        if self.output_combox.currentText() != target:
            self._skip_lang_change = True
            self.output_combox.setCurrentText(target)
            self._skip_lang_change = False

    def _on_lang_changed(self):
        if self._skip_lang_change:
            return
        self._debounce_timer.stop()
        if self.input.toPlainText().strip():
            self._debounce_timer.start(300)

    def _swap_languages(self):
        src = self.input_combox.currentText()
        tgt = self.output_combox.currentText()

        self._skip_lang_change = True
        if src != '自动识别':
            self.input_combox.setCurrentText(tgt)
        self.output_combox.setCurrentText(src if src != '自动识别' else '英文')
        self._skip_lang_change = False

        if self.input.toPlainText().strip():
            self.translate()

    def translate(self):
        text = self.input.toPlainText().strip()
        if not text:
            return

        self.output.setPlainText('翻译中...')
        self.set_title('不忙翻译 - 翻译中...')

        from_lang = LANG_MAP.get(self.input_combox.currentText(), 'auto')
        to_lang = LANG_MAP.get(self.output_combox.currentText(), 'en')

        self._request_seq += 1
        seq = self._request_seq

        self._worker = TranslateWorker(text, from_lang, to_lang)
        self._worker.result_ready.connect(
            lambda r, e: self._on_result(r, e, seq)
        )
        self._worker.start()

    def _on_result(self, result, error, seq):
        if seq != self._request_seq:
            return
        if error:
            self.output.setPlainText(error)
        else:
            self.output.setPlainText(result)
        self.set_title('不忙翻译')


if __name__ == '__main__':
    app = QApplication(sys.argv)
    if sys.platform == 'win32':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            '瞎忙软件开发工作室.不忙翻译.不忙翻译.v0.0.1')
    demo = FanYi()
    demo.show()
    app.exec_()
