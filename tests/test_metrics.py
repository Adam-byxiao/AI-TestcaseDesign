"""实时指标（LiveMetrics）单元测试：热路径、并发、限流、估算与校准、静默检测。"""
import threading
import time

from prd2tc.metrics import (LiveMetrics, current_module, estimate_tokens,
                            set_current_module)


def test_delta_and_snapshot_basic():
    m = LiveMetrics(push_interval_ms=0.0)
    m.set_stage("cases")
    m.begin_module("AUTH", requirements=2, batches=3)
    m.begin_request("AUTH")
    m.delta("AUTH", "正在生成测试用例：")
    m.delta("AUTH", "login flow ")
    m.set_batch("AUTH", 2, 3)
    snap = m.snapshot(force=True)
    auth = snap["modules"]["AUTH"]
    assert snap["type"] == "run_metrics" and snap["stage"] == "cases"
    assert auth["deltas"] == 2 and auth["batch"] == "2/3"
    assert auth["firstTokenMs"] >= 0
    assert "login flow" in auth["preview"] and "正在生成" in auth["preview"]
    assert auth["state"] == "running" and snap["activeRequests"] == 1


def test_estimated_then_exact_usage():
    """无 usage 时按字符估算并标 estimated；拿到 Provider usage 后转为精确。"""
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("CART", batches=1)
    m.begin_request("CART")
    m.delta("CART", "中文内容" * 10)
    snap = m.snapshot(force=True)
    cart = snap["modules"]["CART"]
    assert cart["estimated"] is True
    assert cart["completionTokens"] == estimate_tokens(40, 0) > 0

    m.end_request("CART", prompt_tokens=100, completion_tokens=17)
    snap = m.snapshot(force=True)
    cart = snap["modules"]["CART"]
    assert cart["completionTokens"] == 17, "精确 usage 必须覆盖估算值"
    assert cart["estimated"] is False
    assert snap["totals"]["promptTokens"] == 100
    assert snap["totals"]["totalTokens"] == 117


def test_tokens_per_sec_and_totals():
    m = LiveMetrics(push_interval_ms=0.0)
    for name, completion in (("AUTH", 100), ("CART", 50)):
        m.begin_module(name, batches=1)
        m.begin_request(name)
        m.delta(name, "x" * 10)
        m.end_request(name, prompt_tokens=10, completion_tokens=completion)
    m.finish_module("AUTH")
    snap = m.snapshot(force=True)
    assert snap["totals"]["completionTokens"] == 150
    assert snap["totals"]["promptTokens"] == 20
    assert snap["totals"]["totalTokens"] == 170
    assert snap["activeRequests"] == 1, "只有 CART 仍在运行"
    assert snap["modules"]["AUTH"]["state"] == "done"
    assert snap["modules"]["AUTH"]["tokensPerSec"] >= 0


def test_silent_ms_detects_stall():
    """静默时长是「卡在哪」的直接证据：有 delta 后故意不再产出。"""
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("SLOW", batches=1)
    m.begin_request("SLOW")
    m.delta("SLOW", "起点")
    assert m.snapshot(force=True)["modules"]["SLOW"]["silentMs"] == 0
    time.sleep(0.12)
    silent = m.snapshot(force=True)["modules"]["SLOW"]["silentMs"]
    assert silent >= 100, silent


def test_snapshot_is_throttled_but_force_bypasses():
    """热路径高频更新不得被逐次推送：限流生效，force 可绕过。"""
    m = LiveMetrics(push_interval_ms=10000.0)
    m.begin_module("A", batches=1)
    m.begin_request("A")
    assert m.snapshot() is not None, "首次快照应放行"
    for _ in range(500):
        m.delta("A", "x")
    assert m.snapshot() is None, "限流窗口内不应再产出快照"
    assert m.snapshot(force=True) is not None


def test_concurrent_deltas_do_not_lose_updates():
    """jobs>1 时多模块并发写入，计数必须精确、无丢失（此前 module_tokens 就是无锁的）。"""
    m = LiveMetrics(push_interval_ms=0.0)
    modules = [f"M{i}" for i in range(8)]
    per_thread = 200
    errors: list[BaseException] = []

    def worker(name: str):
        try:
            m.begin_module(name, batches=1)
            m.begin_request(name)
            for _ in range(per_thread):
                m.delta(name, "ab")
                m.delta(name, "中文")
        except BaseException as exc:  # pragma: no cover - 仅用于暴露竞态
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(name,)) for name in modules]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    snap = m.snapshot(force=True)
    for name in modules:
        item = snap["modules"][name]
        assert item["deltas"] == per_thread * 2, name
        assert item["chars"] == per_thread * 4, name
        assert item["completionTokens"] == estimate_tokens(per_thread * 2, per_thread * 2)


def test_concurrent_snapshot_while_writing():
    """读取快照与写入并发时不得抛 RuntimeError（字典变更/迭代冲突）。"""
    m = LiveMetrics(push_interval_ms=0.0)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        i = 0
        while not stop.is_set():
            m.delta(f"M{i % 5}", "x")
            m.begin_module(f"M{i % 5}", batches=1)
            i += 1

    def reader():
        try:
            while not stop.is_set():
                m.snapshot(force=True)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(3)] + \
              [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.25)
    stop.set()
    for t in threads:
        t.join()
    assert not errors, errors


def test_module_states_and_error_recorded():
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("BAD", batches=1)
    m.finish_module("BAD", "failed", error="HTTP 404")
    snap = m.snapshot(force=True)
    assert snap["modules"]["BAD"]["state"] == "failed"
    assert snap["modules"]["BAD"]["error"] == "HTTP 404"
    assert snap["activeRequests"] == 0


def test_preview_is_bounded():
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("LONG", batches=1)
    m.begin_request("LONG")
    for _ in range(50):
        m.delta("LONG", "0123456789" * 5)
    preview = m.snapshot(force=True)["modules"]["LONG"]["preview"]
    assert len(preview) <= 600, len(preview)


def test_thread_local_current_module():
    set_current_module("AUTH")
    assert current_module() == "AUTH"
    seen: list[str] = []

    def worker():
        seen.append(current_module())
        set_current_module("CART")
        seen.append(current_module())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == ["", "CART"], "线程本地不得串味"
    assert current_module() == "AUTH"


def test_final_snapshot_marks_finished():
    m = LiveMetrics(push_interval_ms=10000.0)
    m.begin_module("A", batches=1)
    final = m.final_snapshot()
    assert final["finished"] is True and final["type"] == "run_metrics"
    assert m.snapshot(force=True)["finished"] is True


def test_think_content_is_not_counted_as_output():
    """M3 把思考写在 content 的 <think> 里；不得算成产出，否则看板会误报进度。"""
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("AUTH", batches=1)
    m.begin_request("AUTH")
    m.delta("AUTH", "<think>我在分析 testData 的嵌套结构，思考很久很久</think>")
    m.delta("AUTH", '[{"id":"TC-AUTH-001"}]')
    item = m.snapshot(force=True)["modules"]["AUTH"]
    assert item["reasoningChars"] > 0
    assert "我在分析" in item["reasoningPreview"]
    assert "我在分析" not in item["preview"], "思考不得进入产出预览"
    assert '[{"id"' in item["preview"], "产出预览应只含正文"
    assert item["chars"] == len('[{"id":"TC-AUTH-001"}]'), item["chars"]


def test_reasoning_only_output_is_flagged():
    """「思考吃光预算、正文为零」必须被显式标记，供 UI 与门禁识别。"""
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("TOOL", batches=1)
    m.begin_request("TOOL")
    m.delta("TOOL", "<think>" + "让我检查一下 JSON 转义问题。" * 40)
    item = m.snapshot(force=True)["modules"]["TOOL"]
    assert item["reasoningOnly"] is True, item
    assert item["chars"] == 0
    assert item["contentTokens"] == 0

    ok = LiveMetrics(push_interval_ms=0.0)
    ok.begin_module("TOOL", batches=1)
    ok.begin_request("TOOL")
    ok.delta("TOOL", "<think>简短思考</think>" + "正文内容" * 50)
    assert ok.snapshot(force=True)["modules"]["TOOL"]["reasoningOnly"] is False


def test_explicit_reasoning_channel_is_separate():
    """reasoning_content 独立通道：计入思考，不计入正文。"""
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("M", batches=1)
    m.begin_request("M")
    m.delta("M", "思考内容", reasoning=True)
    m.delta("M", "正文")
    item = m.snapshot(force=True)["modules"]["M"]
    assert item["reasoningChars"] == 4
    assert item["preview"] == "正文"
    assert item["reasoningPreview"] == "思考内容"


def test_end_request_counts_reasoning_chars_from_separate_channel():
    m = LiveMetrics(push_interval_ms=0.0)
    m.begin_module("M", batches=1)
    m.begin_request("M")
    m.end_request("M", prompt_tokens=10, completion_tokens=50, reasoning_chars=400)
    item = m.snapshot(force=True)["modules"]["M"]
    assert item["reasoningChars"] == 400
    assert item["estimated"] is False
