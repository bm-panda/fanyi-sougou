# -*- coding: utf-8 -*-
"""不忙翻译 v2 —— 在线 + 本地 AI 双引擎翻译。

界面沿用 v1 的右下角贴边小窗，只多了一行「引擎」选择：

    在线翻译（快速）  搜狗 Web 接口，秒级返回，需要联网
    本地 AI（离线）    Hy-MT2-1.8B 本地推理，无需联网，首次需加载模型

所有耗时操作都在后台：在线走 QThread，本地走独立子进程 + 队列轮询。
主线程只负责渲染，不参与任何计算 —— 这是 v1 与 v2 最大的结构差别。

v2.1 起多了一条无人值守路径：盒子以 `invoke_mode == "node"` 调用时
（其他脚本通过 /api/link 联动），不开窗口、直接用同步引擎翻译，
再把信封写进 `environment.output_json`。入口分流在 main()。
"""

import ctypes
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCursor, QTextCursor
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget, QHBoxLayout

from xsideui import (IconName, XButtonVariant, XCard, XColor, XComboBox,
                     XLabel, XPushButton, XSize, XTextEdit, XWidget)

from src.app_settings import OFFLINE, ONLINE, AppSettings
from src.engine_offline import OfflineEngine
from src.engine_online import OnlineEngine
from src.languages import LANG_AUTO, lang_names, offline_name, online_code
from src.worker import MODEL_FILENAME, MODEL_REPO_ID

APP_TITLE = '不忙翻译'
OUTPUT_HINT = '翻译结果'

ENGINE_LABELS = {
    ONLINE: '在线翻译（快速）',
    OFFLINE: '本地 AI（离线）',
}
LABEL_TO_ENGINE = {label: key for key, label in ENGINE_LABELS.items()}

# 输出框刷新节流：攒够 FLUSH_MIN_CHARS 或距上次 FLUSH_INTERVAL 秒，刷一次
FLUSH_INTERVAL = 0.06
FLUSH_MIN_CHARS = 40

# 输入停顿多久后自动翻译。本地推理慢得多，留长一点，少做无用功
DEBOUNCE_ONLINE = 0.8
DEBOUNCE_OFFLINE = 1.2

# 节点（无人值守）执行的超时。首次调用要现场加载约 1.13GB 模型，给足时间；
# 可用环境变量 FY_NODE_TIMEOUT 覆盖（单位秒）。
NODE_ENGINE_ONLINE = 'online'
NODE_ENGINE_OFFLINE = 'offline'


def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


NODE_LOAD_TIMEOUT = _env_int('FY_NODE_TIMEOUT', 300)
NODE_TALK_TIMEOUT = NODE_LOAD_TIMEOUT * 2


# --------------------------------------------------------------------------
# 模型定位
# --------------------------------------------------------------------------

def resolve_model_path(api_base, script_dir):
    """定位本地模型文件。

    优先问盒子要模型目录（走盒子运行时才有 api_base），再拼文件名；
    拿不到就依次回退到环境变量与若干常见位置。
    """
    if api_base:
        try:
            query = urllib.parse.urlencode({'repo_id': MODEL_REPO_ID})
            with urllib.request.urlopen(f'{api_base}/api/model/path?{query}', timeout=8) as resp:
                body = json.loads(resp.read().decode('utf-8'))
            if body.get('success'):
                return str(Path(body['data']['path']) / MODEL_FILENAME)
        except Exception:
            pass  # 盒子接口不可用不是致命问题，继续回退

    env_path = os.environ.get('FY_MODEL_PATH')
    if env_path:
        return env_path

    base = Path(script_dir) if script_dir else Path(__file__).parent
    candidates = [
        base / MODEL_FILENAME,
        base / 'models' / MODEL_FILENAME,
        base.parent / 'models' / MODEL_FILENAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(base / 'models' / MODEL_FILENAME)


# --------------------------------------------------------------------------
# 节点模式（被其他脚本通过 /api/link 调用）
#
# 契约：盒子把参数 JSON 的路径塞进 argv[1]，其中 environment.invoke_mode
# == "node" 表示本次是联动调用。此时脚本必须无人值守跑完，绝不能弹窗或
# 等待用户操作，并把结果写成信封写进 environment.output_json。
#
# 本模块的 GUI 部分（QApplication / FanYi）只在 manual 触发时才会被创建，
# 节点路径完全不碰 Qt 窗口 —— 这跟 offline 引擎的「零 GUI 依赖」是配套的。
# --------------------------------------------------------------------------

def read_payload():
    """读盒子塞在 argv[1] 的参数 JSON；拿不到就返回空字典。"""
    if len(sys.argv) <= 1:
        return {}
    path = Path(sys.argv[1])
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def write_envelope(output_json, body):
    """写节点信封，强制 code 是 int、msg 是 str（盒子按此校验）。"""
    if not isinstance(body.get('code'), int):
        body['code'] = -1
    if not isinstance(body.get('msg'), str):
        body['msg'] = str(body.get('msg') or '')
    try:
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def translate_offline_blocking(text, src, to_lang, model_path):
    """同步跑一次本地模型翻译，返回译文全文。

    GUI 里由 QTimer 驱动 poll()，这里没有事件循环，就自己转轮询。
    长文本分块已在 worker 内部完成，这里只管把 token 攒起来。
    """
    pieces = []
    state = {'done': False, 'error': None}

    engine = OfflineEngine(model_path)
    engine.on_token = pieces.append
    engine.on_done = lambda: state.__setitem__('done', True)
    engine.on_error = lambda msg: state.__setitem__('error', msg)

    deadline = time.monotonic() + NODE_TALK_TIMEOUT
    try:
        engine.start()
        if not engine.wait_ready(timeout=NODE_LOAD_TIMEOUT):
            raise RuntimeError(engine.fatal or '本地模型加载超时')
        if not engine.translate(text, src, to_lang):
            raise RuntimeError(engine.last_error or '提交离线翻译失败')

        while time.monotonic() < deadline:
            engine.poll()
            if state['error']:
                raise RuntimeError(state['error'])
            if state['done']:
                break
            if engine.fatal:
                raise RuntimeError(engine.fatal)
            if not engine.busy:
                break  # 没 done 也没 error 却已不在跑：让空结果检查兜底
            time.sleep(0.05)
        else:
            raise TimeoutError('离线翻译超时')
    finally:
        engine.stop(timeout=3.0)

    return ''.join(pieces)


def node_translate(text, src_name, to_name, engine_key, model_path):
    """返回 (实际用的引擎, 译文)。入参是**中文语言名**。

    两个引擎支持的语言集不重合，调用方指定的语言未必属于它请求的引擎，
    所以这里先按请求引擎试，不支持（或跑失败）就换另一个引擎；都不行才报错。
    信封里的 engine 始终如实反映实际用的那个。
    """
    order = ([NODE_ENGINE_OFFLINE, NODE_ENGINE_ONLINE]
             if engine_key == NODE_ENGINE_OFFLINE
             else [NODE_ENGINE_ONLINE, NODE_ENGINE_OFFLINE])

    last_error = None
    for key in order:
        if key == NODE_ENGINE_ONLINE:
            to_code = online_code(to_name)
            if not to_code:
                continue  # 在线不认这门外语，交给离线
            try:
                return key, OnlineEngine().translate(
                    text, online_code(src_name) or 'auto', to_code)
            except Exception as exc:
                last_error = exc
        else:
            to_en = offline_name(to_name)
            if not to_en or not model_path or not Path(model_path).exists():
                continue  # 模型不在 / 离线不认这门外语，交给在线
            try:
                return key, translate_offline_blocking(
                    text, offline_name(src_name), to_en, model_path)
            except Exception as exc:
                last_error = exc

    raise RuntimeError(last_error or f'没有引擎支持「{to_name}」')


def _emit(output_json, body):
    """写信封；写不进去就交非零退出码，让盒子按「格式错误」处理。"""
    return 0 if write_envelope(output_json, body) else 1


def run_node(payload):
    """无人值守入口：不开窗口，跑完把信封写进 output_json。

    业务失败也返回 0 —— 结果由信封的 code 表达。若这里以非零码退出，
    盒子会另合成一个错误信封，反而把我们写好的 msg 盖掉。
    """
    env = payload.get('environment') or {}
    output_json = env.get('output_json')
    if not output_json:
        return 1  # 契约规定节点调用必有 output_json，真缺了就只能靠退出码

    try:
        params = payload.get('params') or {}
        texts = (payload.get('data') or {}).get('translate_text') or []
        text = (texts[0] if texts else '').strip()
        if not text:
            return _emit(output_json, {'code': -1, 'msg': '没有待翻译的文本'})

        from_lang = params.get('from_lang') or LANG_AUTO
        to_lang = params.get('to_lang') or '中文'
        engine_key = str(params.get('engine') or NODE_ENGINE_ONLINE).lower()

        model_path = resolve_model_path(env.get('api_base'), env.get('script_dir'))
        used, result = node_translate(text, from_lang, to_lang, engine_key, model_path)

        if not (result or '').strip():
            return _emit(output_json, {'code': -1, 'msg': '翻译返回空结果'})

        return _emit(output_json, {
            'code': 0,
            'msg': 'ok',
            'translated_text': result,
            'engine': used,
            'target_lang': to_lang,
        })
    except Exception as exc:
        return _emit(output_json,
                     {'code': -1, 'msg': f'{type(exc).__name__}: {exc}'})


# --------------------------------------------------------------------------
# 在线引擎：QThread 包装（网络请求不能阻塞主线程）
# --------------------------------------------------------------------------

class OnlineWorker(QThread):
    """一次请求拿到全文，一次性发出去。"""

    chunk = Signal(str, int)
    done = Signal(int)
    failed = Signal(str, int)

    def __init__(self, text, from_lang, to_lang, seq):
        super().__init__()
        self._text = text
        self._from = from_lang
        self._to = to_lang
        self._seq = seq
        self._cancelled = False

    def cancel(self):
        """作废本次请求。urlopen 无法中断，但结果不会再发出去。"""
        self._cancelled = True

    def run(self):
        try:
            result = OnlineEngine().translate(self._text, self._from, self._to)
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(str(exc), self._seq)
            return
        if self._cancelled:
            return
        self.chunk.emit(result, self._seq)
        self.done.emit(self._seq)


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class FanYi(XWidget):
    def __init__(self, payload=None, parent=None):
        super().__init__(parent)

        self._payload = payload if payload is not None else read_payload()
        self._settings = AppSettings()
        self._engine_key = self._settings.engine
        self._model_path = ''

        self._job_seq = 0
        self._buffer = ''
        self._last_flush = 0.0
        self._wrote_output = False  # 译文框里是否有真译文（决定复制按钮是否可用）

        self._online_worker = None
        self._retired_workers = []  # 取消后仍在跑的线程，跑完再释放
        self._offline = None
        self._pending_text = None  # 离线模型未就绪时暂存的原文
        self._skip_lang_change = False

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self.translate)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._poll_offline)

        # 临时状态提示（「已复制 N 字」）用单发定时器复位，别 sleep 主线程
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._restore_idle_status)

        self._init_ui()
        self._load_startup_payload()
        self.setWindowFlag(Qt.WindowStaysOnTopHint)

        # 上次用的是本地引擎 → 提前把模型加载起来，省得第一次翻译干等
        if self._engine_key == OFFLINE:
            self._ensure_offline()
        self._set_status(self._idle_status())
        self._update_action_buttons()

    # ---------------- 界面 ----------------

    def _get_centered_pos(self) -> QPoint:
        screen = QApplication.screenAt(QCursor.pos())
        if not screen:
            screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            return QPoint(geo.right() - self.width(), geo.bottom() - self.height())
        return super()._get_centered_pos()

    def _init_ui(self):
        self.resize(420,580)
        self.hide_theme_button()
        self.hide_maximize_button()
        self.hide_minimize_button()
        self.set_title(APP_TITLE)
        self.set_logo(str(Path(__file__).parent / 'images' / 'fanyi.png'))

        content = QWidget()
        self.addWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(11, 11, 11, 11)
        layout.setSpacing(11)

        # 语言选择
        card = XCard(spacing=20, padding=(11, 2, 11, 2))

        self.input_combox = XComboBox(border_visible=False)
        self.input_combox.addItems(lang_names(self._engine_key))
        self.input_combox.setCurrentText(LANG_AUTO)
        self.input_combox.currentTextChanged.connect(self._on_lang_changed)

        self.icon_btn = XPushButton(
            variant=XButtonVariant.LINK,
            icon=IconName.SWITCH,
            color=XColor.TERTIARY
        )
        self.icon_btn.clicked.connect(self._swap_languages)

        self.output_combox = XComboBox(border_visible=False)
        self.output_combox.addItems(lang_names(self._engine_key))
        self.output_combox.setCurrentText('英文')
        self.output_combox.currentTextChanged.connect(self._on_lang_changed)

        card.addWidget(self.input_combox)
        card.addWidget(self.icon_btn)
        card.addWidget(self.output_combox)

        # 输入框
        self.input = XTextEdit(placeholder='请输入翻译内容')
        self.input.textChanged.connect(self._on_input_changed)
        # 输出框
        self.output = XTextEdit(placeholder=OUTPUT_HINT)
        self.output.setReadOnly(True)

        # 操作区
        operate_layout = QHBoxLayout()
        # 状态文本
        self.status_label = XLabel('', style=XLabel.Style.CAPTION, color=XColor.TERTIARY)
        self.engine_combo = XComboBox(size=XSize.SMALL)
        self.engine_combo.addItems([ENGINE_LABELS[ONLINE], ENGINE_LABELS[OFFLINE]])
        self.engine_combo.setCurrentText(ENGINE_LABELS[self._engine_key])
        self.engine_combo.currentTextChanged.connect(self._on_engine_changed)
        self.btn_clear = XPushButton(size=XSize.SMALL, color=XColor.DANGER, variant=XButtonVariant.TEXT, icon=IconName.CLEAR)
        self.btn_clear.setToolTip('清空输入与译文')
        self.btn_clear.clicked.connect(self._on_clear_clicked)

        self.btn_copy = XPushButton(size=XSize.SMALL,  variant=XButtonVariant.TEXT, icon=IconName.CLIPBOARD)
        self.btn_copy.setToolTip('复制译文')
        self.btn_copy.clicked.connect(self._on_copy_clicked)

        operate_layout.addWidget(self.status_label)
        operate_layout.addStretch()
        operate_layout.addWidget(self.btn_clear)
        operate_layout.addWidget(self.btn_copy)
        operate_layout.addWidget(self.engine_combo)

        layout.addWidget(card)
        layout.addWidget(self.input)

        layout.addWidget(self.output)
        layout.addLayout(operate_layout)

    # ---------------- 启动参数 ----------------

    def _load_startup_payload(self):
        """取出模型目录与待翻译文本。

        盒子调用约定见 json-contract：environment 段给运行环境信息，
        data 段给输入参数（inputs.name = translate_text）。
        payload 已在构造时读好，节点模式下根本不会走到这里。
        """
        payload = self._payload
        env = payload.get('environment') or {}
        data = payload.get('data') or {}

        self._model_path = resolve_model_path(env.get('api_base'), env.get('script_dir'))

        texts = data.get('translate_text') or []
        if texts and texts[0]:
            self.input.setPlainText(texts[0])
            self._debounce_timer.stop()
            QTimer.singleShot(0, self.translate)

    # ---------------- 输入 / 语言 ----------------

    def _on_input_changed(self):
        text = self.input.toPlainText().strip()
        if not text:
            # 输入框清空 = 停止在途任务（不给用户加停止按钮，这就是「停止」）
            self._cancel_inflight()
            self._reset_view()
            return
        self._auto_switch_output_lang(text)
        self._debounce_timer.start(
            int(self._current_debounce() * 1000)
        )

    def _current_debounce(self):
        return DEBOUNCE_OFFLINE if self._engine_key == OFFLINE else DEBOUNCE_ONLINE

    def _detect_language(self, text):
        chinese = len(re.findall(r'[一-鿿]', text))
        english = len(re.findall(r'[a-zA-Z]', text))
        total = chinese + english
        if total == 0:
            return None
        ratio = chinese / total
        if ratio > 0.6:
            return 'zh'
        if ratio < 0.4:
            return 'en'
        return None

    def _auto_switch_output_lang(self, text):
        if self.input_combox.currentText() != LANG_AUTO:
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
        if src != LANG_AUTO:
            self.input_combox.setCurrentText(tgt)
        self.output_combox.setCurrentText(src if src != LANG_AUTO else '英文')
        self._skip_lang_change = False

        if self.input.toPlainText().strip():
            self.translate()

    # ---------------- 清空 / 复制 ----------------

    def _reset_view(self):
        """回到初始态：丢掉在途缓冲、清空译文框、恢复标题与状态栏。"""
        self._buffer = ''
        self._pending_text = None
        self._wrote_output = False
        self.output.clear()
        self.output.setPlaceholderText(OUTPUT_HINT)
        self.set_title(APP_TITLE)
        self._set_status(self._idle_status())
        self._update_action_buttons()

    def _on_clear_clicked(self):
        """清空按钮：停掉在途翻译，输入与译文一起清掉，回到初始态。"""
        self._debounce_timer.stop()
        self._status_timer.stop()
        self._cancel_inflight()
        self.input.blockSignals(True)
        self.input.clear()
        self.input.blockSignals(False)
        self._reset_view()
        self.input.setFocus()

    def _on_copy_clicked(self):
        """复制按钮：把译文框全文写进剪贴板。"""
        text = self.output.toPlainText().strip()
        if not text:
            self._flash_status('没有可复制的译文')
            return
        QApplication.clipboard().setText(text)
        self._flash_status(f'已复制 {len(text)} 字')

    def _update_action_buttons(self):
        """复制按钮只在真有译文时可用。

        判断走 _wrote_output 布尔标志，不去 toPlainText() ——
        读大文本是 O(译文长度)，前几轮刚把渲染开销从主线程里压下去。
        """
        self.btn_copy.setEnabled(self._wrote_output)

    def _flash_status(self, msg, timeout=1500):
        """临时状态提示，超时自动回到常态文案。"""
        self._set_status(msg)
        self._status_timer.start(timeout)

    def _restore_idle_status(self):
        self._set_status(self._idle_status())

    # ---------------- 引擎切换 ----------------

    def _on_engine_changed(self, label):
        key = LABEL_TO_ENGINE.get(label)
        if key is None or key == self._engine_key:
            return
        self._switch_engine(key)
        if self.input.toPlainText().strip():
            self.translate()
        else:
            self.set_title(APP_TITLE)
            self._set_status(self._idle_status())

    def _switch_engine(self, key):
        """程序化切换引擎：作废在途任务、释放/加载模型、重排语言列表、落盘。"""
        self._engine_key = key
        self._settings.engine = key

        self._cancel_inflight()  # 内部要读 self._offline，必须排在 _unload_offline 之前
        self._buffer = ''
        self._pending_text = None

        if key == ONLINE:
            # 离开本地引擎就把模型从内存里放掉，别让 1GB+ 一直占着
            self._unload_offline()

        label = ENGINE_LABELS[key]
        if self.engine_combo.currentText() != label:
            self.engine_combo.blockSignals(True)
            self.engine_combo.setCurrentText(label)
            self.engine_combo.blockSignals(False)

        # 两个引擎支持的语言集不重合，下拉框得跟着换
        self._repopulate_langs()

        if key == OFFLINE:
            self._ensure_offline()

    def _repopulate_langs(self):
        """按当前引擎重填两个语言下拉框；原选中语言不被支持时回落。

        在线只认 21 种、离线能翻 38 种，两边不重合 —— 与其让用户选到翻不了的语言
        再在翻译时报错，不如根本不给选项。
        """
        names = lang_names(self._engine_key)
        src_old = self.input_combox.currentText()
        tgt_old = self.output_combox.currentText()

        src_new = src_old if src_old in names else LANG_AUTO
        if tgt_old in names:
            tgt_new = tgt_old
        else:
            tgt_new = '中文' if src_new != '中文' else '英文'

        self._skip_lang_change = True
        for combo, value in ((self.input_combox, src_new),
                             (self.output_combox, tgt_new)):
            combo.blockSignals(True)  # clear() 会发 currentTextChanged，必须挡住
            combo.clear()
            combo.addItems(names)
            combo.setCurrentText(value)
            combo.blockSignals(False)
        self._skip_lang_change = False

    # ---------------- 翻译调度 ----------------

    def translate(self):
        text = self.input.toPlainText().strip()
        if not text:
            return

        self._cancel_inflight()
        self._job_seq += 1
        seq = self._job_seq

        self._buffer = ''
        self._wrote_output = False
        self.output.clear()
        self.output.setPlaceholderText('翻译中…')
        self.set_title(f'{APP_TITLE} - 翻译中…')
        self._update_action_buttons()

        if self._engine_key == ONLINE:
            self._start_online(text, seq)
        else:
            self._start_offline(text)

    def _start_online(self, text, seq):
        to_code = online_code(self.output_combox.currentText())
        if not to_code:
            # 语言列表已按引擎过滤，正常选不到这里；防的是外部改动或历史配置
            self._fail(f'在线翻译不支持「{self.output_combox.currentText()}」'
                       f'，请切换到本地 AI')
            return
        worker = OnlineWorker(
            text,
            online_code(self.input_combox.currentText()) or 'auto',
            to_code,
            seq,
        )
        worker.chunk.connect(self._on_chunk)
        worker.done.connect(self._on_online_done)
        worker.failed.connect(self._on_online_failed)
        self._online_worker = worker
        worker.start()

    def _start_offline(self, text):
        engine = self._ensure_offline()
        if engine is None:
            self._fail('未找到本地模型文件')
            return
        if engine.fatal:
            self._fail(f'本地模型不可用：{engine.fatal}')
            return

        tgt_name = self.output_combox.currentText()
        to_en = offline_name(tgt_name)
        if to_en is None:
            self._fail(f'本地 AI 不支持「{tgt_name}」，请切换到在线翻译')
            return

        if not engine.ready:
            # 模型还在加载 —— 记下来，就绪后自动补翻
            self._pending_text = text
            self.output.setPlaceholderText('本地模型加载中…')
            self.set_title(f'{APP_TITLE} - 加载本地模型…')
            self._set_status('本地模型加载中，就绪后自动翻译…')
            return

        self._pending_text = None
        engine.translate(text, offline_name(self.input_combox.currentText()), to_en)

    def _cancel_inflight(self):
        """作废在途任务：在线线程打标记，离线子进程同步任务号。"""
        worker = self._online_worker
        if worker is not None:
            worker.cancel()
            self._retired_workers.append(worker)
            worker.finished.connect(self._reap_workers)
            self._online_worker = None
        if self._offline is not None:
            self._offline.cancel()

    def _reap_workers(self):
        self._retired_workers = [w for w in self._retired_workers if w.isRunning()]

    # ---------------- 离线引擎 ----------------

    def _ensure_offline(self):
        """拿到（必要时创建并启动）离线引擎。返回 None 表示模型文件不在。"""
        if self._offline is not None and not self._offline.fatal:
            return self._offline

        if not self._model_path or not Path(self._model_path).exists():
            self._offline = None
            return None

        engine = OfflineEngine(self._model_path)
        engine.on_token = self._on_offline_token
        engine.on_done = self._on_offline_done
        engine.on_error = self._on_offline_error
        engine.on_ready = self._on_offline_ready
        engine.on_fatal = self._on_offline_fatal
        engine.start()

        self._offline = engine
        if not self._poll_timer.isActive():
            self._poll_timer.start()
        return engine

    def _unload_offline(self):
        """停掉推理子进程，把模型占的内存（约 1GB+）交还系统。

        权重跑在 spawn 子进程里，杀掉它内存就回来了。
        stop() 先礼后兵：投毒丸 -> join(5s) -> 还在跑就 terminate()，
        所以推理中途切引擎也不会拖着那 1GB 不放。
        代价：切回离线要重新加载（本机约 3 秒）。
        """
        engine = self._offline
        self._offline = None
        if engine is not None:
            engine.stop(timeout=5.0)
        if self._poll_timer.isActive():
            self._poll_timer.stop()

    def _poll_offline(self):
        if self._offline is not None:
            self._offline.poll()

    def _on_offline_ready(self):
        if self._engine_key == OFFLINE:
            self._set_status('本地模型已就绪')
        if self._pending_text and self._engine_key == OFFLINE:
            self.translate()

    def _on_offline_fatal(self, msg):
        self._fail(msg)
        self._set_status('本地模型不可用')

    def _on_offline_token(self, piece):
        self._buffer += piece
        self._flush(force=False)

    def _on_offline_done(self):
        self._flush(force=True)
        self._finish_ok()

    def _on_offline_error(self, msg):
        self._flush(force=True)
        self._fail(msg)

    # ---------------- 在线回调 ----------------

    def _on_chunk(self, text, seq):
        if seq != self._job_seq:
            return
        self._buffer += text
        self._flush(force=False)

    def _on_online_done(self, seq):
        if seq != self._job_seq:
            return
        self._flush(force=True)
        self._finish_ok()

    def _on_online_failed(self, msg, seq):
        if seq != self._job_seq:
            return
        # 在线挂了 → 自动降级到本地模型。必须让用户看见，不静默切
        if self._engine_key == ONLINE and self._model_available():
            self._set_status(f'在线翻译失败，已切换本地模型（{msg}）')
            self._switch_engine(OFFLINE)
            self.translate()
            return
        self._fail(msg)

    def _model_available(self):
        return bool(self._model_path) and Path(self._model_path).exists()

    # ---------------- 输出 ----------------

    def _flush(self, force=False):
        if not self._buffer:
            return
        now = time.monotonic()
        if not force and (now - self._last_flush) < FLUSH_INTERVAL \
                and len(self._buffer) < FLUSH_MIN_CHARS:
            return
        self._last_flush = now
        piece, self._buffer = self._buffer, ''

        bar = self.output.verticalScrollBar()
        stick_to_bottom = bar.value() >= bar.maximum() - 2
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(piece)
        if stick_to_bottom:
            bar.setValue(bar.maximum())
        self._wrote_output = True

    def _finish_ok(self):
        self.output.setPlaceholderText(OUTPUT_HINT)
        self.set_title(APP_TITLE)
        self._set_status(self._idle_status())
        self._update_action_buttons()

    def _fail(self, msg):
        self.output.setPlaceholderText(OUTPUT_HINT)
        self.output.setPlainText(f'翻译失败：{msg}')
        self.set_title(APP_TITLE)
        # 失败文案不是译文，复制按钮保持禁用
        self._wrote_output = False
        self._update_action_buttons()

    def _set_status(self, msg):
        self.status_label.setText(msg)

    def _idle_status(self):
        if self._engine_key == ONLINE:
            return '在线翻译'
        if self._offline is None:
            return '本地模型未找到'
        if self._offline.fatal:
            return '本地模型不可用'
        return '本地模型已就绪' if self._offline.ready else '本地模型加载中…'

    # ---------------- 收尾 ----------------

    def closeEvent(self, event):
        self._debounce_timer.stop()
        self._poll_timer.stop()
        self._status_timer.stop()
        self._cancel_inflight()
        if self._offline is not None:
            self._offline.stop(timeout=3.0)
            self._offline = None
        super().closeEvent(event)


def main():
    payload = read_payload()

    # 被其他脚本联动调用 -> 无人值守跑完写信封，绝不创建窗口
    if (payload.get('environment') or {}).get('invoke_mode') == 'node':
        return run_node(payload)

    app = QApplication(sys.argv)
    if sys.platform == 'win32':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            '瞎忙软件开发工作室.不忙翻译.不忙翻译.v2.2.0')
    demo = FanYi(payload)
    demo.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
