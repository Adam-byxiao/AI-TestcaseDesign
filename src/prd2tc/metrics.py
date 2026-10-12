"""运行期实时指标：内存态、线程安全、限流快照。

设计约束（都来自本仓库的实测结论）：

1. **绝不落盘事件**。``WorkspaceStore.event()`` 每写一条事件都要把整个 JSONL 读出来
   重写，逐 token 落盘会退化成 O(n²) 写放大。因此实时指标只存内存，由 WebSocket
   推快照；需要崩溃恢复时只在批次边界覆盖写一个很小的 progress.json。
2. **热路径只做整数累加**。每个流式 delta 都会调用 ``delta()``，这里不做 I/O、
   不做正则、不做 JSON；token 估算推迟到 ``snapshot()`` 里算。
3. **多模块并发**（``jobs>1``）时全部可变状态用一把锁保护。
4. **估算必须可辨识**。中途按字符估算的 completion token 标 ``estimated``；
   拿到 Provider 真实 usage 后校准并把标记翻转为 False，UI 才能诚实地显示「≈」。

这个模块的存在意义是回答「我的活儿卡在哪了」，所以除了 token 计数，还记录：

- ``first_token_ms``：首 token 延迟 —— 区分「连不上/排队」与「正在生成」；
- ``silent_ms``：距上次收到内容的静默时长 —— 直接暴露「卡住」；
- ``preview``：最近若干字符的正文尾巴 —— 肉眼确认模型确实在产出。
"""
from __future__ import annotations

import threading
import time

from prd2tc.reasoning import ThinkScanner

# 正文预览保留的尾部字符数：够看出模型在写什么，又不至于把快照撑大
PREVIEW_TAIL_CHARS = 600

# 估算口径：CJK 约 1 token/字，西文约 4 字符/token
_CJK_TOKENS_PER_CHAR = 1.0
_ASCII_CHARS_PER_TOKEN = 4.0

MODULE_STATES = ("pending", "running", "done", "failed", "skipped")


def estimate_tokens(cjk_chars: int, ascii_chars: int) -> int:
    """按字符构成估算 token 数（Provider 未回显 usage 时的回退口径）。"""
    return int(cjk_chars * _CJK_TOKENS_PER_CHAR + ascii_chars / _ASCII_CHARS_PER_TOKEN)


def _split_counts(text: str) -> tuple[int, int]:
    """返回 (CJK 字符数, 其余字符数)。"""
    cjk = sum(1 for ch in text if ch >= "\u2e80")
    return cjk, len(text) - cjk


class LiveMetrics:
    """一次运行的实时指标。线程安全，可被 WebSocket 循环反复读取快照。"""

    def __init__(self, *, push_interval_ms: int = 500):
        self._lock = threading.RLock()
        self._push_interval = max(0.0, push_interval_ms / 1000.0)
        self._created = time.perf_counter()
        self._last_emit = 0.0
        self._stage = ""
        self._modules: dict[str, dict] = {}
        self._totals = {"promptTokens": 0, "completionTokens": 0,
                        "cacheReadTokens": 0, "cacheCreationTokens": 0}
        self._estimated = False
        self._cancelled = False
        self._finished = False
        self._final: dict | None = None

    # ---- 写入侧（流水线/LLM 回调调用）----

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self._stage = stage

    def begin_module(self, module: str, *, requirements: int = 0, batches: int = 1) -> None:
        with self._lock:
            now = time.perf_counter()
            item = self._modules.setdefault(module, self._blank(module))
            item.update({"state": "running", "requirements": requirements,
                         "batchIndex": 0, "batchTotal": max(1, batches),
                         "startedAt": now, "finishedAt": 0.0, "error": ""})

    def set_batch(self, module: str, index: int, total: int) -> None:
        with self._lock:
            item = self._modules.get(module)
            if item is not None:
                item["batchIndex"] = index
                item["batchTotal"] = max(1, total)

    def begin_request(self, module: str) -> None:
        """一次 LLM 请求开始（含门禁重试），用于统计首 token 延迟。"""
        with self._lock:
            item = self._modules.setdefault(module, self._blank(module))
            item["requests"] += 1
            item["_requestStart"] = time.perf_counter()
            item["_firstTokenSeen"] = False

    def delta(self, module: str, text: str, *, reasoning: bool = False) -> None:
        """流式热路径：只做整数累加与有界字符串拼接。

        reasoning=True 表示来自独立推理通道（reasoning_content / thinking_delta）；
        否则按 `<think>` 标签在线分离——M3 会把思考写进 content。
        """
        if not text:
            return
        with self._lock:
            item = self._modules.setdefault(module, self._blank(module))
            now = time.perf_counter()
            # 收到任何字节都算「流还活着」：静默时长据此判断，必须先于分类更新，
            # 否则尾部字符滞留在 carry 里会让静默被误报为 0 或漏报
            item["deltas"] += 1
            item["lastDeltaAt"] = now
            if not item.get("_firstTokenSeen"):
                item["_firstTokenSeen"] = True
                item["firstTokenMs"] = int((now - item.get("_requestStart", now)) * 1000)
            if reasoning:
                content_text, think_text = "", text
            else:
                content_text, think_text = item["scanner"].feed(text)
            if content_text:
                cjk, other = _split_counts(content_text)
                item["cjkChars"] += cjk
                item["asciiChars"] += other
                item["preview"] = (item["preview"] + content_text)[-PREVIEW_TAIL_CHARS:]
            if think_text:
                cjk, other = _split_counts(think_text)
                item["rCjkChars"] += cjk
                item["rAsciiChars"] += other
                item["reasoningChars"] += len(think_text)
                item["reasoningPreview"] = (item["reasoningPreview"] + think_text)[-PREVIEW_TAIL_CHARS:]

    def end_request(self, module: str, *, prompt_tokens: int = 0, completion_tokens: int = 0,
                    cache_read: int = 0, cache_creation: int = 0, estimated: bool = False,
                    reasoning_chars: int = 0) -> None:
        """一次请求结束：累计 usage；拿到精确 usage 时撤销本模块的估算口径。

        语义：
        - completion_tokens 有值且 estimated=False → 采用 Provider 精确值；
        - completion_tokens 有值但 estimated=True → 累加，但仍标估算；
        - 无 completion_tokens → 保持 0，由 snapshot() 按字符估算并标估算。
        """
        with self._lock:
            item = self._modules.get(module)
            if item is None:
                return
            item["promptTokens"] += int(prompt_tokens or 0)
            item["cacheReadTokens"] += int(cache_read or 0)
            item["cacheCreationTokens"] += int(cache_creation or 0)
            if reasoning_chars:
                item["reasoningChars"] += int(reasoning_chars)
            if completion_tokens:
                item["completionTokens"] += int(completion_tokens)
            if estimated or not completion_tokens:
                item["estimated"] = True
                self._estimated = True

    def finish_module(self, module: str, state: str = "done", *, error: str = "") -> None:
        with self._lock:
            item = self._modules.get(module)
            if item is None:
                return
            item["state"] = state if state in MODULE_STATES else "done"
            item["finishedAt"] = time.perf_counter()
            item["error"] = error
            item.pop("_requestStart", None)

    def mark_cancelled(self) -> None:
        with self._lock:
            self._cancelled = True

    def fail_active(self, message: str) -> None:
        """把仍在运行的模块标记为失败并附上原因（门禁错误）。

        否则 UI 只看到 state=failed 却不知道原因，仍要翻日志才能定位。
        """
        if not message:
            return
        with self._lock:
            now = time.perf_counter()
            for item in self._modules.values():
                if item.get("state") == "running":
                    item["state"] = "failed"
                    item["error"] = message
                    item["finishedAt"] = now
                    item.pop("_requestStart", None)

    # ---- 读取侧（WebSocket / HTTP 快照）----

    def snapshot(self, *, force: bool = False) -> dict | None:
        """返回限流后的快照；未到推送间隔且非 force 时返回 None。"""
        with self._lock:
            now = time.perf_counter()
            if not force and (now - self._last_emit) < self._push_interval:
                return None
            self._last_emit = now
            modules = {}
            totals = dict(self._totals)
            for name, item in self._modules.items():
                # flush 让滞留在 carry 的尾部字符归位（未闭合的 <think> 归入推理，
                # 避免「思考被算成产出」）
                tail_c, tail_r = item["scanner"].flush()
                if tail_c:
                    cjk, other = _split_counts(tail_c)
                    item["cjkChars"] += cjk
                    item["asciiChars"] += other
                    item["preview"] = (item["preview"] + tail_c)[-PREVIEW_TAIL_CHARS:]
                if tail_r:
                    cjk, other = _split_counts(tail_r)
                    item["rCjkChars"] += cjk
                    item["rAsciiChars"] += other
                    item["reasoningChars"] += len(tail_r)
                    item["reasoningPreview"] = (item["reasoningPreview"] + tail_r)[-PREVIEW_TAIL_CHARS:]
                est = estimate_tokens(item["cjkChars"], item["asciiChars"])
                completion = item["completionTokens"] or est
                # usage 缺失时数字来自字符估算，必须标记为估算，不能谎报精确
                estimated = bool(item["estimated"] or not item["completionTokens"])
                reasoning_est = estimate_tokens(item["rCjkChars"], item["rAsciiChars"])
                silent = 0
                if item["state"] == "running" and item["lastDeltaAt"]:
                    silent = int((now - item["lastDeltaAt"]) * 1000)
                elapsed = (item["finishedAt"] or now) - item["startedAt"]
                modules[name] = {
                    "state": item["state"],
                    "requirements": item["requirements"],
                    "batch": f'{item["batchIndex"]}/{item["batchTotal"]}',
                    "batchIndex": item["batchIndex"], "batchTotal": item["batchTotal"],
                    "promptTokens": item["promptTokens"],
                    "completionTokens": completion,
                    "estimated": estimated,
                    "chars": item["cjkChars"] + item["asciiChars"],
                    # 思考与产出分开：否则看板会把「正在思考」误报成「正在产出」
                    "reasoningChars": item["reasoningChars"],
                    "reasoningTokens": reasoning_est,
                    "contentTokens": max(0, completion - reasoning_est) if item["completionTokens"] else est,
                    # 产出为 0 但思考很多 => 「预算被思考吃光」的典型信号
                    "reasoningOnly": bool(item["reasoningChars"] > 200 and (item["chars"] + item["asciiChars"]) < 40),
                    "reasoningPreview": item["reasoningPreview"],
                    "tokensPerSec": round(completion / elapsed, 1) if elapsed > 0.05 and completion else 0,
                    # 未被观察到首 token 时为 None：0 无法区分「瞬时响应」与「还没开始产出」
                    "firstTokenMs": item["firstTokenMs"] if item.get("_firstTokenSeen") else None,
                    "silentMs": silent,
                    "deltas": item["deltas"],
                    "requests": item["requests"],
                    "preview": item["preview"],
                    "error": item["error"],
                }
                totals["promptTokens"] += item["promptTokens"]
                totals["completionTokens"] += completion
                totals["cacheReadTokens"] += item["cacheReadTokens"]
                totals["cacheCreationTokens"] += item["cacheCreationTokens"]
            totals["totalTokens"] = totals["promptTokens"] + totals["completionTokens"]
            totals["estimated"] = self._estimated or any(m["estimated"] for m in modules.values())
            silent_max = max([m["silentMs"] for m in modules.values()] or [0])
            active = sum(1 for m in modules.values() if m["state"] == "running")
            snapshot = {
                "type": "run_metrics",
                "stage": self._stage,
                "elapsedMs": int((now - self._created) * 1000),
                "activeRequests": active,
                "silentMs": silent_max,
                "cancelled": self._cancelled,
                "finished": self._finished,
                "totals": totals,
                "modules": modules,
            }
            return snapshot

    def final_snapshot(self) -> dict:
        with self._lock:
            self._finished = True
        return self.snapshot(force=True) or {"type": "run_metrics", "modules": {}, "totals": {}}

    # ---- 内部 ----

    @staticmethod
    def _blank(module: str) -> dict:
        return {"state": "running", "requirements": 0, "batchIndex": 0, "batchTotal": 1,
                "chars": 0, "cjkChars": 0, "asciiChars": 0, "deltas": 0,
                "reasoningChars": 0, "rCjkChars": 0, "rAsciiChars": 0,
                "promptTokens": 0, "completionTokens": 0, "cacheReadTokens": 0,
                "cacheCreationTokens": 0, "estimated": False, "firstTokenMs": 0,
                "startedAt": time.perf_counter(), "lastDeltaAt": 0.0, "finishedAt": 0.0,
                "preview": "", "reasoningPreview": "", "scanner": ThinkScanner(),
                "requests": 0, "error": ""}


# 线程本地「当前模块」：线程池里每个 worker 负责一个模块，用线程本地把流式
# 回调归因到正确的模块，避免把 on_delta 一路穿过 6 层函数签名。
_local = threading.local()


def set_current_module(module: str) -> None:
    _local.module = module


def current_module() -> str:
    return getattr(_local, "module", "")


def bind(metrics: "LiveMetrics | None", module: str = "") -> None:
    """在当前线程绑定指标实例与模块名，供 stages 无侵入地取用。"""
    _local.metrics = metrics
    _local.module = module


def active() -> tuple["LiveMetrics | None", str]:
    return getattr(_local, "metrics", None), getattr(_local, "module", "")


class CancelFlag:
    """运行级取消标志。流式回调逐块检查它，命中即停止接收响应以真正止损。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def cancelled(self) -> bool:
        return self._event.is_set()


_cancel_local = threading.local()


def bind_cancel(flag: "CancelFlag | None") -> None:
    _cancel_local.flag = flag


def cancel_flag() -> "CancelFlag | None":
    return getattr(_cancel_local, "flag", None)
