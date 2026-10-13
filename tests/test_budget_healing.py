"""截断处理与自愈测试。

两类实测故障：

1. **推理耗尽**：MiniMax-M3 遇到复杂 testData 会在 `<think>` 里反复自我纠错直到输出
   预算耗尽，正文一个字符都没写出来（整轮 26,504 字符产出为 0）。
2. **静默截断**（运行 9d97060e）：输出撞满 max_tokens 被截断，但 `extract_cases` 的
   「修补截断 JSON」回退靠 `rfind("]")` 找到用例内部 `tags`/`requirement` 的 `]`，
   修补出一个**合法但残缺**的数组 → 门禁零错误 → 模块被判 succeeded，
   AGENT 只留 1 条（6 条需求）、SCENE 只留 2 条（7 条需求），
   直到最后 traceability 才发现 3 条 P0 未覆盖。

两者共同的正确处置都是**降低单次输出量（拆批）**，且截断必须**无条件**报错。
"""
import json

import pytest

from prd2tc.config import Config
from prd2tc.llm import LLMResult
from prd2tc import gates, stages
from prd2tc.stages import MAX_BATCH_SPLIT_DEPTH, _split_batch_if_budget_exhausted


def _cfg(**kw) -> Config:
    base = Config(api_key="k", api_base="https://x", model="m", temperature=0.0,
                  timeout=10, max_retries=0)
    return base if not kw else Config(**{**base.__dict__, **kw})


def _truncated_salvageable(module: str = "SCENE", complete: int = 2) -> str:
    """构造「内层 ] 闭合后截断」的残缺 JSON —— 静默截断的典型形态。"""
    full = [{"id": f"TC-{module}-{i:03d}", "module": module, "title": f"场景用例{i}",
             "design_method": "场景法", "priority": "P0",
             "tags": ["@smoke", "@scene"], "requirement": [f"REQ-{module}-{i:03d}"],
             "steps": [{"action": "goto", "target": f"/s/{i}", "data": ""},
                        {"action": "expectVisible", "target": f"场景{i}结果区", "data": ""}],
             "expected": [f"页面{i}成功渲染并显示场景数据"]} for i in range(1, complete + 1)]
    return json.dumps(full, ensure_ascii=False)[:-1] + ',"design_method":"场景法","tags":["@smoke"]'


def test_salvage_can_parse_partial_json():
    """先确认缺陷前提：残缺输出确实能被修补成合法数组（所以门禁看不出问题）。"""
    salvaged = stages.extract_and_normalize(_truncated_salvageable("SCENE", 2))
    assert len(salvaged) == 2
    assert gates.check_cases_text(_truncated_salvageable("SCENE", 2), {"SCENE"}) == [], (
        "修补后的数组是合法的，这正是门禁会静默放过截断的原因"
    )


def test_truncation_is_always_an_error_even_when_gate_passes():
    """**核心回归**：只要 finish_reason=length，就必须报错，不能因修补成功而放过。"""
    def fake(cfg, user, **kwargs):
        return LLMResult(content=_truncated_salvageable("SCENE", 2), model="m",
                         finish_reason="length", prompt_tokens=100,
                         completion_tokens=cfg.max_tokens)

    _, result, errs = stages.generate_with_feedback(
        _cfg(), lambda fb: "u", lambda t: gates.check_cases_text(t, {"SCENE"}),
        llm=fake, max_retries=0, label="SCENE", system="s", temperature=0.0,
        reporter=lambda *_: None)
    assert result.truncated is True
    assert errs, "截断输出被静默放过 —— 会导致残缺用例被当作成功结果"
    assert any("不完整" in e for e in errs), errs


def test_truncation_error_explains_how_to_fix():
    def fake(cfg, user, **kwargs):
        return LLMResult(content=_truncated_salvageable(), model="m",
                         finish_reason="length")
    _, _, errs = stages.generate_with_feedback(
        _cfg(), lambda fb: "u", lambda t: [],
        llm=fake, max_retries=0, label="x", system="s", temperature=0.0,
        reporter=lambda *_: None)
    joined = " ".join(errs)
    for hint in ("长度截断", "精简输出", "完整闭合"):
        assert hint in joined, (hint, joined)


def test_non_truncated_output_still_passes():
    """对照：正常结束（finish_reason=stop）不受影响，不得误报。"""
    def fake(cfg, user, **kwargs):
        return LLMResult(content=_truncated_salvageable("SCENE", 2), model="m",
                         finish_reason="stop")
    _, result, errs = stages.generate_with_feedback(
        _cfg(), lambda fb: "u", lambda t: gates.check_cases_text(t, {"SCENE"}),
        llm=fake, max_retries=0, label="x", system="s", temperature=0.0,
        reporter=lambda *_: None)
    assert errs == [], errs


def test_module_marked_failed_so_module_retry_is_possible():
    """截断必须让模块状态为 failed，否则「仅重试失败模块」无法使用。

    实测 9d97060e 中 5 个模块全被标 succeeded，用户虽有重试入口却无模块可重试。
    """
    statuses: dict[str, str] = {}

    def fake(cfg, user, **kwargs):
        return LLMResult(content=_truncated_salvageable("A", 2), model="m",
                         finish_reason="length")

    reqs = [{"id": f"REQ-A-{i:03d}", "module": "A", "title": f"需求{i}", "priority": "P0"}
            for i in range(1, 7)]
    with pytest.raises(Exception):
        stages.run_stage_3_modules(
            _cfg(), "方案", reqs, "# A\n正文", llm=fake, jobs=1, use_gate=True,
            max_gate_retries=0, reporter=lambda *_: None,
            status_callback=lambda m, s: statuses.__setitem__(m, s),
            batch_callback=lambda *_: None)
    assert statuses.get("A") == "failed", statuses


def _batch(n: int) -> list[dict]:
    return [{"id": f"REQ-A-{i:03d}", "module": "A", "title": f"需求{i}", "priority": "P1"}
            for i in range(1, n + 1)]


def _exhausted_result() -> LLMResult:
    return LLMResult(content="<think>JSON 解析失败了，让我检查 testData 的转义",
                     model="m", finish_reason="length")


def test_split_enqueues_halves_when_budget_exhausted():
    pending: list = []
    batch = _batch(4)
    events = []
    split = _split_batch_if_budget_exhausted(
        "A", batch, 0, _exhausted_result().content, _exhausted_result(),
        ["用例 JSON 解析失败"], pending,
        emit=lambda *a: events.append(a))
    assert split is True
    assert [len(b) for b, _ in pending] == [2, 2], pending
    assert all(depth == 1 for _, depth in pending)
    assert events and "拆分" in events[0][-1]
    # 拆分后需求不丢不重
    assert sorted(r["id"] for b, _ in pending for r in b) == sorted(r["id"] for r in batch)


def test_no_split_when_already_single_requirement():
    pending: list = []
    assert _split_batch_if_budget_exhausted(
        "A", _batch(1), 0, _exhausted_result().content, _exhausted_result(), ["e"], pending) is False
    assert pending == []


def test_no_split_beyond_max_depth():
    pending: list = []
    assert _split_batch_if_budget_exhausted(
        "A", _batch(4), MAX_BATCH_SPLIT_DEPTH, _exhausted_result().content,
        _exhausted_result(), ["e"], pending) is False
    assert pending == []


def test_no_split_when_not_truncated():
    """普通门禁失败（非截断）不应触发拆分，否则会无谓放大调用次数。"""
    pending: list = []
    result = LLMResult(content="<think>思考</think>坏的 JSON", model="m", finish_reason="stop")
    assert _split_batch_if_budget_exhausted(
        "A", _batch(4), 0, result.content, result, ["e"], pending) is False
    assert pending == []


def test_split_on_plain_truncation_with_partial_json():
    """被截断且已写出部分 JSON → **仍要拆分**（v0.6.1 起放宽）。

    理由：截断的本质是「单次要输出的内容超过 max_tokens」，而「已有部分 JSON」
    恰恰说明内容确实写不完。实测运行 9d97060e 就是这种形态：AGENT/SCENE 一次请求
    撞满 8192，extract_cases 的修补逻辑抢救出合法但残缺的数组，模块被判成功，
    直到最后 traceability 才发现 P0 未覆盖。正确杠杆是降低单次输出量（拆批），
    而不是原地重试同样大的批次。
    """
    pending: list = []
    result = LLMResult(content='<think>x</think>[{"id":"TC-A-001"', model="m",
                        finish_reason="length")
    assert _split_batch_if_budget_exhausted(
        "A", _batch(4), 0, result.content, result, ["e"], pending) is True
    assert [len(b) for b, _ in pending] == [2, 2], pending


def test_split_message_distinguishes_reasoning_exhaustion():
    """两种截断给出不同说明，便于定位是「推理吃光预算」还是「纯输出过长」。"""
    events: list = []
    plain = LLMResult(content='[{"id":"TC-A-001"', model="m", finish_reason="length")
    _split_batch_if_budget_exhausted("A", _batch(4), 0, plain.content, plain, ["e"], [],
                                     emit=lambda *a: events.append(a))
    assert "长度截断" in events[0][-1], events

    events.clear()
    think_only = LLMResult(content="<think>" + "反复纠错。" * 40, model="m", finish_reason="length")
    _split_batch_if_budget_exhausted("A", _batch(4), 0, think_only.content, think_only, ["e"], [],
                                     emit=lambda *a: events.append(a))
    assert "推理耗尽" in events[0][-1], events


def test_generate_with_feedback_gives_reasoning_specific_hint():
    """门禁反馈应指明「预算被思考耗尽」，而不是泛化的截断提示。"""
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.0,
                 timeout=10, max_retries=0)
    calls: list[str] = []

    def fake(cfg, user, **kwargs):
        calls.append(user)
        return LLMResult(content="<think>让我先分析一下这个 JSON 为什么坏了" * 5,
                         model="m", finish_reason="length")

    content, result, errors = stages.generate_with_feedback(
        cfg, lambda fb: "u", lambda text: ["用例 JSON 解析失败: 未找到数组"],
        llm=fake, max_retries=0, label="t", system="s", temperature=0.0,
        reporter=lambda *_: None)
    assert errors, errors
    joined = " ".join(errors)
    assert "思考" in joined, joined
    assert "<think>" in joined, joined


def test_split_heals_a_module_that_would_otherwise_fail(monkeypatch):
    """端到端：首轮因思考耗尽而失败，拆批后应成功产出用例。

    构造：需求 4 条 → 首次调用返回「截断 + 全思考」；拆成 2 批后每次都返回合法 JSON。
    """
    cfg = Config(api_key="k", api_base="https://x", model="m", temperature=0.0,
                 timeout=10, max_retries=0)
    state = {"calls": 0}

    def fake(cfg, user, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return LLMResult(content="<think>" + "JSON 转义问题排查。" * 60,
                             model="m", finish_reason="length")
        # 从 hint 里取本批需求编号，生成引用它们的合法用例
        import re
        ids = sorted(set(re.findall(r"REQ-A-\d{3}", user)))
        cases = [{"id": f"TC-A-{i:03d}", "module": "A", "title": f"场景{i}", "design_method": "场景法", "priority": "P1",
                  "tags": ["@regression"], "requirement": [rid],
                  # 步骤/预期必须真正不同：C-007 按「同模块+同步骤+同预期」判重复，
                  # 只改 id/requirement 的用例会被门禁拦下（那正是「同质用例」的定义）。
                  "steps": [{"action": "goto", "target": f"/p{i}", "data": ""},
                            {"action": "expectVisible", "target": f"页面{i}主体", "data": ""}],
                  "expected": [f"页面{i}成功渲染"]} for i, rid in enumerate(ids, 1)]
        import json
        return LLMResult(content="```json\n" + json.dumps(cases, ensure_ascii=False) + "\n```",
                         model="m", finish_reason="stop")

    from prd2tc import gates
    seen_batches: list[int] = []

    cases, result = stages.run_stage_3_modules(
        cfg, "方案", _batch(4), "正文", llm=fake, jobs=1, use_gate=True, max_gate_retries=0,
        reporter=lambda *_: None, status_callback=lambda *_: None,
        batch_callback=lambda m, i, t, p: None)
    assert state["calls"] >= 2, state
    assert len(cases) == 4, cases
    assert {r for c in cases for r in c["requirement"]} == {f"REQ-A-{i:03d}" for i in range(1, 5)}
