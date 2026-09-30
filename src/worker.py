#!/usr/bin/env python3
# _*_ coding: utf-8 -*-
"""翻译推理 worker — 独立进程运行，避免与 GUI 主线程争抢 GIL。

协议（multiprocessing.Queue，消息为 (type, payload, epoch)）：
    - 收 job:  (text: str, src: str|None, tgt: str, epoch: int)
    - 收停止:  None
    - 发消息:  ("ready", None, 0) / ("fatal", msg, 0)
               / ("token", str, epoch) / ("done", None, epoch)
               / ("error", msg, epoch)

    epoch 是主进程单调递增的任务号，同时写入共享的 active_epoch。
    worker 在每个 token 处比对 active_epoch：一旦任务已过期（用户点了
    「停止/清空」或提交了新任务），立刻放弃当前任务、不再空烧算力。
    过期任务不补发 done，其残余 token 由主进程按 epoch 丢弃。

    fatal 表示进程级故障（模型加载失败，重试也没用）；
    error 表示单次任务失败（worker 仍可用，可继续接受新任务）。

长文本处理：
    原文超过 CHUNK_SRC_TOKENS 时，按「段落 → 换行 → 句末标点 → 硬切」分层切块，
    逐块流式翻译并顺序拼接；除首块外，每块携带前一块原文的尾部作为背景，
    以提升跨块术语与指代的一致性。

    原文在预算内（不触发分块）时，prompt 与改造前完全一致，行为零变更。
"""
from __future__ import annotations

import os
import re
import sys
from typing import Callable, List, Optional, Tuple

from .languages import ZH_PROMPT_TARGETS, zh_name

MODEL_REPO_ID = "tencent/Hy-MT2-1.8B-GGUF"
MODEL_FILENAME = "Hy-MT2-1.8B-Q4_K_M.gguf"

LANG_AUTO = "自动识别"

# Hy-MT 官方推荐的采样参数
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TOP_P = 0.6
DEFAULT_REPEAT_PENALTY = 1.05
DEFAULT_MAX_TOKENS = 2048

# 上下文窗口：模型结构为 hunyuan-dense（32 层 / 4 个 KV 头 / head_dim 128），
# KV cache 约 64 KiB/token，8192 对应约 512 MiB。
N_CTX = 8192

# ---------- 推理效率 ----------
# 线程数：6 核 6 线程 + 15W 的低压移动芯片，4 线程是甜点。
# 再往上会吃光功耗预算、CPU 降频，反而变慢，且会拖钝界面。
# 可用环境变量 FY_N_THREADS 覆盖，无需改代码。
DEFAULT_N_THREADS = 4
# 批大小。注意 context_params.n_ubatch = min(n_batch, n_ubatch)，
# 库默认 n_ubatch=512 —— 只把 n_batch 提到 1024 是无效的（会被 n_ubatch 卡住），
# 两个必须一起提，才能让 prefill 真的按 1024 一批算。
DEFAULT_N_BATCH = 1024
WARMUP_TOKENS = 8           # 预热生成量，够把全部权重翻进内存即可

# ---------- 分块预算 ----------
PROMPT_OVERHEAD = 32        # 模板特殊 token + 指令前缀的保守估计（实测 14~17）
SAFETY_MARGIN = 64          # 计数误差 / BOS-EOS 安全垫
BG_MAX_TOKENS = 256         # 每块携带的前文原文上限
OUTPUT_RATIO_MAX = 2.0      # 译文/原文 token 比硬上界（实测最坏 1.51）
# 单块原文预算由生成预算反推，保证每块译文都能被 DEFAULT_MAX_TOKENS 容纳
CHUNK_SRC_TOKENS = int(DEFAULT_MAX_TOKENS / OUTPUT_RATIO_MAX)   # = 1024
MAX_SPLIT_DEPTH = 4         # 兜底对半重切的最大递归深度
BG_RETRY_SHRINK = 2         # 重试时背景预算缩减倍数

# 单块最坏 token 占用 = prompt 侧 + 生成侧 + 安全垫。
# 必须留在 N_CTX 之内，否则 llama-cpp-python 会静默砍小 max_tokens，译文被截断。
WORST_CHUNK_TOKENS = (
    PROMPT_OVERHEAD + BG_MAX_TOKENS + CHUNK_SRC_TOKENS
    + DEFAULT_MAX_TOKENS + SAFETY_MARGIN
)

_PROMPT_WITH_SRC = "Translate the following {src} segment into {tgt}, without additional explanation.\n\n{text}"
_PROMPT_AUTO_SRC = "Translate the following segment into {tgt}, without additional explanation.\n\n{text}"

# 官方要求「中文 prompt 配中文语言名」。只有 ZH_PROMPT_TARGETS 里的语言走这套 ——
# 它们在英文模板下实测跑不出正确结果（见 languages.ZH_PROMPT_TARGETS 的注释）。
_PROMPT_ZH_WITH_SRC = "将以下{src}文本翻译为{tgt}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
_PROMPT_ZH_AUTO_SRC = "将以下文本翻译为{tgt}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"

_BG_BLOCK = (
    "Reference context — the tail of the previous source segment "
    "(for terminology and pronoun consistency only; do NOT translate it):\n"
    "{bg}\n\n"
)

_BG_BLOCK_ZH = (
    "参考上文（上一段原文的结尾，仅供术语与指代保持一致，不要翻译它）：\n"
    "{bg}\n\n"
)

_CJK_TARGETS = {"Chinese", "Traditional Chinese", "Cantonese", "Japanese", "Korean"}


def build_prompt(text: str, src: Optional[str], tgt: str) -> str:
    """组装 prompt。src/tgt 传的是**英文语言名**（自动识别用 None）。

    目标语属于 ZH_PROMPT_TARGETS 时切到中文模板，并把语言名一起换回中文 ——
    官方要求 prompt 语种与语言名语种一致，混搭在实测中会出坏结果。
    """
    src = (src or "").strip()
    tgt = (tgt or "").strip() or "Chinese"

    if tgt in ZH_PROMPT_TARGETS:
        tgt = zh_name(tgt)
        if not src or src == LANG_AUTO:
            return _PROMPT_ZH_AUTO_SRC.format(tgt=tgt, text=text)
        return _PROMPT_ZH_WITH_SRC.format(src=zh_name(src), tgt=tgt, text=text)

    if not src or src == LANG_AUTO:
        return _PROMPT_AUTO_SRC.format(tgt=tgt, text=text)
    return _PROMPT_WITH_SRC.format(src=src, tgt=tgt, text=text)


def build_chunk_prompt(text: str, src: Optional[str], tgt: str, bg: str) -> str:
    """分块路径专用：在原始 prompt 前追加一段前文背景。

    背景放在最前，让模型习得的「指令 + 正文」模式保持连续；
    bg 为空时退化为 build_prompt，即与单块路径逐字节相同。
    """
    base = build_prompt(text, src, tgt)
    bg = (bg or "").strip()
    if not bg:
        return base
    block = _BG_BLOCK_ZH if (tgt or "").strip() in ZH_PROMPT_TARGETS else _BG_BLOCK
    return block.format(bg=bg) + base


# --------------------------------------------------------------------------
# 分块算法
# --------------------------------------------------------------------------

_PARA_SEP = re.compile(r"\n[ \t]*\n+")     # 空行 = 段落分隔
_LINE_SEP = re.compile(r"\n")              # 单换行
_SENT_TRAIL = "」』”’\"')）】]"              # 句末标点后可跟随的收尾字符
_SENT_END = "。！？；…!?;：:"               # 无条件断句的标点
_SOFT_BREAK = " \t\r\n，。、；：,.;:!?！？)]}」』“”‘’" + _SENT_TRAIL


def _split_keep(text: str, pattern) -> List[str]:
    """按 pattern 切分，并把分隔符拼回【前一片的尾部】，保证 join 可还原原文。"""
    parts: List[str] = []
    last = 0
    for m in pattern.finditer(text):
        parts.append(text[last:m.end()])
        last = m.end()
    if last < len(text):
        parts.append(text[last:])
    return parts or [text]


def _split_sentences(text: str) -> List[str]:
    """句末标点切分，保留标点与其后的收尾字符 / 空白。

    英文的 . ! ? 只在「后接空白或串尾」时才断句，
    借此天然避开 3.14 / example.com / U.S. 这类内部标点。
    """
    out: List[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        hit = False
        if ch in _SENT_END:
            hit = True
        elif ch in ".!?":
            nxt = text[i + 1] if i + 1 < n else ""
            hit = nxt == "" or nxt.isspace()
        if hit:
            j = i + 1
            while j < n and text[j] in _SENT_TRAIL:
                j += 1
            while j < n and text[j] in " \t":
                j += 1
            out.append(text[start:j])
            start = j
            i = j
            continue
        i += 1
    if start < n:
        out.append(text[start:])
    return out or [text]


def _max_prefix(text: str, budget: int, count: Callable[[str], int]) -> int:
    """最大的 k，使 count(text[:k]) <= budget。

    二分合法性来自单调性：追加字符只会保持或新增 token，不会减少。
    返回值保证 >= 1（除非 text 为空），以免调用方死循环。
    """
    n = len(text)
    if n == 0:
        return 0
    if count(text) <= budget:
        return n
    lo, hi = 1, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return max(1, lo)


def _soft_backoff(text: str, start: int, cut: int) -> int:
    """把切点向前退到最近的软边界，并保护 CRLF 与 UTF-16 代理对不被拆开。"""
    n = len(text)
    if start < cut < n:
        if text[cut - 1] == "\r" and text[cut] == "\n":
            cut -= 1
        elif 0xD800 <= ord(text[cut - 1]) <= 0xDBFF and 0xDC00 <= ord(text[cut]) <= 0xDFFF:
            cut -= 1
    floor = start + max(1, (cut - start) * 3 // 4)   # 最多回退 25% 窗口
    for j in range(cut - 1, floor - 1, -1):
        if text[j] in _SOFT_BREAK or text[j].isspace():
            return j + 1
    return cut


def _hard_cut(text: str, budget: int, count: Callable[[str], int]) -> List[str]:
    """无空白无标点的超长串（URL / base64 / 连续 CJK）兜底切分。"""
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        rest = text[i:]
        k = len(rest) if count(rest) <= budget else _max_prefix(rest, budget, count)
        cut = _soft_backoff(text, i, i + k)
        if cut <= i:                    # 极端情况：保底前进一个码点
            cut = i + 1
        out.append(text[i:cut])
        i = cut
    return out


_LEVELS = (
    lambda t: _split_keep(t, _PARA_SEP),    # 0 段落
    lambda t: _split_keep(t, _LINE_SEP),    # 1 换行
    _split_sentences,                       # 2 句末标点
)


def _chunk_recurse(text: str, budget: int,
                   count: Callable[[str], int], level: int) -> List[str]:
    """递归分层 + 贪心打包：每片 count() <= budget，且尽量在自然边界断开。"""
    if not text:
        return []
    if count(text) <= budget:
        return [text]
    if level >= 3:                      # 已无可切层级 -> 硬切
        return _hard_cut(text, budget, count)

    pieces = _LEVELS[level](text)
    if len(pieces) <= 1:                # 该层切不动 -> 下降一层
        return _chunk_recurse(text, budget, count, level + 1)

    out: List[str] = []
    buf = ""
    for p in pieces:
        if count(p) > budget:           # 单片段仍超预算 -> 递归下降
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_chunk_recurse(p, budget, count, level + 1))
            continue
        if buf and count(buf + p) > budget:   # 装不下 -> 结算
            out.append(buf)
            buf = p
        else:
            buf += p
    if buf:
        out.append(buf)
    return out


def split_text(text: str, budget: int,
               count: Callable[[str], int]) -> List[Tuple[str, str]]:
    """切成 [(块正文, 块后分隔符), ...]。

    分隔符附着在【前一块尾部】，保证块间空白不丢失，译文结构与原文一一对应。
    """
    raws = _chunk_recurse(text, budget, count, level=0)
    result: List[Tuple[str, str]] = []
    for raw in raws:
        if not raw.strip():             # 纯空白片段 -> 并入上一块的分隔符
            if result:
                pb, ps = result[-1]
                result[-1] = (pb, ps + raw)
            continue
        body = raw.strip()
        lead = raw[:len(raw) - len(raw.lstrip())]
        sep = raw[len(raw.rstrip()):]
        if result:
            pb, ps = result[-1]
            result[-1] = (pb, ps + lead)
        result.append((body, sep))
    return result


def _boundary_joiner(sep: str, tgt: str) -> str:
    """块间分隔符：原文有就沿用原文；硬切边界无空白时按目标语言补。"""
    if sep:
        return sep
    return "" if (tgt or "").strip() in _CJK_TARGETS else " "


# --------------------------------------------------------------------------
# 翻译器
# --------------------------------------------------------------------------

def _resolve_n_threads(explicit: Optional[int] = None) -> int:
    """线程数优先级：显式参数 > 环境变量 FY_N_THREADS > 默认值。"""
    if explicit:
        return max(1, int(explicit))
    env = os.environ.get("FY_N_THREADS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return DEFAULT_N_THREADS


class Translator:
    """懒加载的翻译器，模型只在首次使用时载入。

    译文/原文 token 比的上界（OUTPUT_RATIO_MAX）保证
    CHUNK_SRC_TOKENS * OUTPUT_RATIO_MAX == DEFAULT_MAX_TOKENS，
    即每块的译文都能被生成预算容纳。
    """

    def __init__(self, model_path: str, n_threads: Optional[int] = None) -> None:
        self.model_path = model_path
        self.n_threads = _resolve_n_threads(n_threads)
        # prefill 用满全部逻辑核，只把 decode 压到 n_threads
        self.n_threads_batch = max(self.n_threads, os.cpu_count() or self.n_threads)
        self._llm = None
        self._tok = None

    @property
    def llm(self):
        if self._llm is None:
            from llama_cpp import Llama  # 延迟导入

            if not os.path.exists(self.model_path):
                raise FileNotFoundError(f"模型不存在: {self.model_path}")
            options = dict(
                n_ctx=N_CTX,
                n_threads=self.n_threads,
                n_threads_batch=self.n_threads_batch,
                n_batch=DEFAULT_N_BATCH,
                n_ubatch=DEFAULT_N_BATCH,
                no_perf=True,           # 关掉逐步计时，省一点每 token 开销
                verbose=False,
            )
            try:
                # mlock：把权重钉在物理内存里，避免被换出后再从磁盘翻回来。
                # 可能因权限或配额失败，失败就退回默认的按需分页。
                self._llm = Llama(self.model_path, use_mlock=True, **options)
            except Exception as exc:
                print(f"[warn] mlock 不可用（{exc}），回退为按需分页")
                self._llm = Llama(self.model_path, **options)
            self._check_context_budget(self._llm.n_ctx())
        return self._llm

    def warmup(self) -> None:
        """预热：跑一次极短推理，把权重从磁盘真正读进来。

        use_mmap 默认开启 —— Llama() 返回时权重还没读进内存，首次推理
        要边跑边 page fault，所以「就绪」后的第一句会额外卡一下。
        这里提前把它跑掉，把这段延迟从用户的第一句话里挪到启动阶段。
        """
        llm = self.llm
        for _ in llm.create_chat_completion(
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=WARMUP_TOKENS,
            temperature=0.0,
            stream=True,
        ):
            pass
        llm.reset()

    @staticmethod
    def _check_context_budget(n_ctx: int) -> None:
        """启动自检：单块最坏占用必须留在【实际】上下文窗口之内。

        若未来调整 N_CTX / DEFAULT_MAX_TOKENS 等常量导致越界，
        这里会立刻报出来，而不是等到译文被静默截断才发现。
        """
        if WORST_CHUNK_TOKENS > n_ctx:
            print(
                f"[warn] 上下文预算不足：单块最坏需 {WORST_CHUNK_TOKENS} token，"
                f"实际 n_ctx={n_ctx}，长文本可能被截断",
                file=sys.stderr,
            )

    # ---------- token 计数 ----------

    def _tokenizer(self):
        if self._tok is None:
            self._tok = self.llm.tokenizer()
        return self._tok

    def _count_tokens(self, text: str) -> int:
        """近似计数：只统计文本本身的 token，不含模板开销。"""
        if not text:
            return 0
        tok = self._tokenizer()
        try:
            return len(tok.encode(text, add_bos=False, special=True))
        except Exception:
            # 兜底：按字节数估，只会高估（更保守）
            return len(text.encode("utf-8"))

    def _tail_tokens(self, text: str, budget: int) -> str:
        """取 count() <= budget 的最大后缀。count(text[i:]) 关于 i 单调不增，可二分。"""
        if self._count_tokens(text) <= budget:
            return text
        lo, hi, n = 1, len(text), len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._count_tokens(text[mid:]) <= budget:
                hi = mid
            else:
                lo = mid + 1
        out = text[lo:]
        sp = out.find(" ")              # 避免以半个单词开头
        if 0 <= sp < 8:
            out = out[sp + 1:]
        return out

    def _make_background(self, prev_body: Optional[str],
                         budget: int = BG_MAX_TOKENS) -> str:
        """取前一块原文的尾部作为背景，按句从后往前累加。"""
        if not prev_body:
            return ""
        picked: List[str] = []
        total = 0
        for p in reversed(_split_sentences(prev_body)):
            t = self._count_tokens(p)
            if not picked and t > budget:
                p = self._tail_tokens(p, budget)
                t = self._count_tokens(p)
            if picked and total + t > budget:
                break
            picked.append(p)
            total += t
            if total >= budget:
                break
        return "".join(reversed(picked)).strip()

    # ---------- 单次请求 ----------

    def _stream_once(self, text: str, src: Optional[str], tgt: str,
                     bg: Optional[str] = None,
                     should_abort: Optional[Callable[[], bool]] = None):
        prompt = build_chunk_prompt(text, src, tgt, bg) if bg else build_prompt(text, src, tgt)
        messages = [{"role": "user", "content": prompt}]
        finish = None
        for chunk in self.llm.create_chat_completion(
            messages=messages,
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=DEFAULT_TEMPERATURE,
            top_p=DEFAULT_TOP_P,
            repeat_penalty=DEFAULT_REPEAT_PENALTY,
            stream=True,
        ):
            if should_abort is not None and should_abort():
                return              # 任务已过期，立刻放弃生成
            choice = chunk["choices"][0]
            delta = choice["delta"].get("content")
            if delta:
                yield delta
            fr = choice.get("finish_reason")
            if fr is not None:
                finish = fr
        if finish == "length":
            # 预算理论上已排除该路径；若仍发生，只做诊断，不污染译文
            print(
                f"[warn] 单块输出触顶被截断 (finish_reason=length, "
                f"max_tokens={DEFAULT_MAX_TOKENS})",
                file=sys.stderr,
            )

    # ---------- 兜底重切 ----------

    def _bisect(self, body: str) -> List[str]:
        """把块对半切：优先句边界，其次字符中点（带软边界回退）。"""
        total = self._count_tokens(body)
        if total <= 1:
            return [body]
        pieces = _split_sentences(body)
        if len(pieces) < 2:
            mid = len(body) // 2
            cut = _soft_backoff(body, 0, mid)
            if cut <= 0 or cut >= len(body):
                cut = max(1, mid)
            return [body[:cut], body[cut:]]
        acc = 0
        cut_at = None
        for i, p in enumerate(pieces):
            acc += self._count_tokens(p)
            if acc >= total // 2:
                cut_at = i
                break
        if cut_at is None:
            cut_at = max(0, len(pieces) // 2 - 1)
        cut_at = min(cut_at, len(pieces) - 2)
        left = "".join(pieces[:cut_at + 1])
        right = "".join(pieces[cut_at + 1:])
        return [left, right] if left and right else [body]

    def _stream_chunk(self, body: str, src: Optional[str], tgt: str,
                      bg: Optional[str], depth: int,
                      bg_budget: int = BG_MAX_TOKENS,
                      should_abort: Optional[Callable[[], bool]] = None):
        """带兜底的单块流式翻译。

        ValueError 由 llama-cpp-python 在 sampling 循环【之前】抛出
        （llama.py:1337），此刻尚未 yield 任何 token，因此对半重切重试
        不会造成译文重复。

        深度耗尽时直接把异常抛出，绝不静默丢弃内容：
        宁可让上层报错，也不能悄悄少译一段。
        """
        if not body:
            return
        if should_abort is not None and should_abort():
            return
        try:
            yield from self._stream_once(body, src, tgt, bg, should_abort)
        except ValueError:
            if depth >= MAX_SPLIT_DEPTH:
                raise
            halves = self._bisect(body)
            if len(halves) < 2:
                raise
            for i, h in enumerate(halves):
                sub_bg = bg if i == 0 else self._make_background(halves[i - 1], bg_budget)
                yield from self._stream_chunk(
                    h, src, tgt, sub_bg, depth + 1,
                    max(64, bg_budget // BG_RETRY_SHRINK),
                    should_abort,
                )

    # ---------- 对外入口 ----------

    def translate_stream(self, text: str, src: Optional[str], tgt: str,
                         should_abort: Optional[Callable[[], bool]] = None):
        """逐 token 产出译文；超长原文自动分块并顺序拼接。

        原文在预算内时走单块路径，prompt 与改造前逐字节一致。
        should_abort 返回 True 时立即停止（任务已过期，算力不再浪费）。
        """
        text = text or ""
        if not text.strip():
            return

        def _abort() -> bool:
            return should_abort is not None and should_abort()

        chunks = split_text(text, CHUNK_SRC_TOKENS, self._count_tokens)

        if len(chunks) <= 1:
            yield from self._stream_chunk(text, src, tgt, None, 0, should_abort=should_abort)
            return

        prev_body: Optional[str] = None
        for idx, (body, _sep) in enumerate(chunks):
            if _abort():
                return
            if idx > 0:
                joiner = _boundary_joiner(chunks[idx - 1][1], tgt)
                if joiner:
                    yield joiner
            bg = self._make_background(prev_body) if idx > 0 else None
            yield from self._stream_chunk(body, src, tgt, bg, 0, should_abort=should_abort)
            prev_body = body


def inference_worker(in_q, out_q, model_path: str, active_epoch=None) -> None:
    """常驻子进程主循环：启动即加载模型，之后循环接翻译任务。

    active_epoch 是主进程共享的任务号。每个 token 处比对一次，
    一旦不是当前任务就立即收手 —— 用户点「清空」后不再空烧 CPU。
    """
    translator = None
    try:
        translator = Translator(model_path)
        translator.warmup()   # 加载 + 预热，把权重真正读进内存后再报就绪
        out_q.put(("ready", None, 0))
    except Exception as e:
        out_q.put(("fatal", f"模型加载失败: {e}", 0))
        translator = None

    while True:
        job = in_q.get()
        if job is None:
            break
        if translator is None:
            out_q.put(("fatal", "模型未就绪，无法翻译", 0))
            continue

        text, src, tgt, epoch = job

        def _aborted(_epoch=epoch) -> bool:
            return active_epoch is not None and active_epoch.value != _epoch

        try:
            for piece in translator.translate_stream(text, src, tgt, should_abort=_aborted):
                out_q.put(("token", piece, epoch))
            if _aborted():
                continue            # 已被取消：不补发 done，残余输出由主进程丢弃
            out_q.put(("done", None, epoch))
        except Exception as e:
            if _aborted():
                continue
            out_q.put(("error", f"翻译失败: {e}", epoch))
