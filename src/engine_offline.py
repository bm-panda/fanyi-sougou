# -*- coding: utf-8 -*-
"""离线翻译引擎 —— 本地 Hy-MT2 模型推理。

架构：
    主进程 ──in_q──> 推理子进程(worker.py) ──out_q──> 主进程
    推理全部跑在独立的 spawn 子进程中，主进程只做队列轮询，永不阻塞。
    每生成一个 token 就经队列回传，由调用方决定怎么渲染。

「取消」机制：
    主进程持有一个共享任务号 active_epoch，提交任务时 +1。
    子进程在每个 token 处比对任务号，发现过期立刻放弃本次生成，不再空烧 CPU。
    主进程同样丢弃所有任务号不匹配的消息，避免「清空后旧译文又长出来」。

本模块零 GUI 依赖，可独立使用：
    eng = OfflineEngine(model_path)
    eng.on_token = lambda s: print(s, end="", flush=True)
    eng.start()
    eng.wait_ready()
    eng.translate("Hello world.", None, "中文")
    while eng.busy:
        eng.poll()
        time.sleep(0.05)
    eng.stop()

注意：调用方必须周期性调用 poll()。GUI 里挂在 QTimer 上，脚本里就是循环。
"""

import multiprocessing as mp
import queue
import time


class OfflineEngine:
    """本地模型翻译引擎。"""

    def __init__(self, model_path):
        self.model_path = model_path

        self._ctx = mp.get_context("spawn")
        self._in_q = None
        self._out_q = None
        self._active_epoch = None
        self._proc = None
        self._epoch = 0

        self.ready = False       # 模型已加载，可以接活
        self.busy = False        # 有任务在跑
        self.fatal = None        # 引擎级故障（模型加载失败）—— 不可恢复，需重启进程
        self.last_error = None   # 单次任务失败 —— 可直接重试

        # 回调（全部可选）
        self.on_token = None     # (str)                每批 token
        self.on_done = None      # ()                  本次翻译结束
        self.on_error = None     # (str)               本次翻译失败
        self.on_ready = None     # ()                  模型就绪
        self.on_fatal = None     # (str)               引擎故障

    # ---------------- 生命周期 ----------------

    def start(self):
        """启动推理子进程。立即返回，模型加载是异步的。"""
        if self._proc is not None:
            return self._proc.pid

        from .worker import inference_worker  # 延迟导入：主进程不加载 llama_cpp

        self._in_q = self._ctx.Queue()
        self._out_q = self._ctx.Queue()
        self._active_epoch = self._ctx.Value("q", 0)
        self._proc = self._ctx.Process(
            target=inference_worker,
            args=(self._in_q, self._out_q, self.model_path, self._active_epoch),
            daemon=True,
        )
        self._proc.start()
        return self._proc.pid

    def stop(self, timeout=5.0):
        """优雅退出子进程。"""
        if self._in_q is not None:
            try:
                self._in_q.put(None)
            except Exception:
                pass
        if self._proc is not None:
            self._proc.join(timeout=timeout)
            if self._proc.is_alive():
                self._proc.terminate()
                self._proc.join(timeout=2.0)
            self._proc = None
        self.ready = False
        self.busy = False

    def wait_ready(self, timeout=300.0, interval=0.05):
        """阻塞等待模型就绪（脚本/测试用；GUI 请用 poll() 挂定时器）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if self.ready or self.fatal:
                return self.ready
            time.sleep(interval)
        return False

    # ---------------- 任务 ----------------

    def translate(self, text, src, tgt):
        """提交一次翻译。src 为 None 表示自动识别语言。"""
        if self.fatal:
            self._fire(self.on_error, f"引擎不可用: {self.fatal}")
            return False
        if not self.ready:
            self._fire(self.on_error, "模型尚未就绪")
            return False
        text = (text or "").strip()
        if not text:
            return False

        self._epoch += 1
        self._active_epoch.value = self._epoch
        self.busy = True
        self.last_error = None
        self._in_q.put((text, src, tgt, self._epoch))
        return True

    def cancel(self):
        """取消当前任务（同时清空后续到达的旧消息）。"""
        self._epoch += 1
        if self._active_epoch is not None:
            self._active_epoch.value = self._epoch
        self.busy = False

    # ---------------- 轮询 ----------------

    def poll(self):
        """排干队列并分发消息。返回本次是否有消息被处理。"""
        if self._out_q is None:
            return False
        hit = False
        try:
            while True:
                msg, payload, epoch = self._out_q.get_nowait()
                hit = True
                self._dispatch(msg, payload, epoch)
        except queue.Empty:
            pass
        except (EOFError, OSError):
            pass
        return hit

    def _dispatch(self, msg, payload, epoch):
        # 进程级消息：不校验任务号
        if msg == "ready":
            self.ready = True
            self._fire(self.on_ready)
            return
        if msg == "fatal":
            self.fatal = payload
            self.ready = False
            self.busy = False
            self._fire(self.on_fatal, payload)
            return

        # 任务级消息：任务号不匹配一律丢弃（旧任务的幽灵消息）
        if epoch != self._epoch:
            return

        if msg == "token":
            self._fire(self.on_token, payload)
        elif msg == "done":
            self.busy = False
            self._fire(self.on_done)
        elif msg == "error":
            self.busy = False
            self.last_error = payload
            self._fire(self.on_error, payload)

    @staticmethod
    def _fire(cb, *args):
        if cb is not None:
            try:
                cb(*args)
            except Exception:
                pass
