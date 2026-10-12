"""think 分离测试：重点是跨块标记、未闭合思考、以及「预算被思考耗尽」的识别。"""
from prd2tc.reasoning import (ThinkScanner, looks_like_reasoning_exhausted,
                              looks_reasoning_only, split_think, strip_think)


def test_plain_content_has_no_reasoning():
    content, reasoning = split_think("普通正文，没有思考标签")
    assert content == "普通正文，没有思考标签"
    assert reasoning == ""


def test_complete_think_block_is_separated():
    text = "<think>我先分析一下需求</think>[{\"id\":\"TC-A-001\"}]"
    content, reasoning = split_think(text)
    assert reasoning == "我先分析一下需求"
    assert content == "[{\"id\":\"TC-A-001\"}]"
    assert strip_think(text) == "[{\"id\":\"TC-A-001\"}]"


def test_multiple_think_blocks():
    text = "<think>一</think>正文A<think>二</think>正文B"
    content, reasoning = split_think(text)
    assert content == "正文A正文B"
    assert reasoning == "一二"


def test_marker_split_across_stream_chunks():
    """流式下标记必然会被切开，必须靠 carry 兜住。"""
    scanner = ThinkScanner()
    parts = ["<thi", "nk>思考", "内容</thi", "nk>", "正文", "内容"]
    content, reasoning = "", ""
    for part in parts:
        c, r = scanner.feed(part)
        content += c
        reasoning += r
    tail_c, tail_r = scanner.flush()
    content += tail_c
    reasoning += tail_r
    assert content == "正文内容", content
    assert reasoning == "思考内容", reasoning


def test_every_possible_split_point_is_handled():
    """穷举所有切分点，确保没有一处会漏判。"""
    text = "<think>REASON</think>CONTENT"
    for cut in range(len(text) + 1):
        scanner = ThinkScanner()
        content, reasoning = "", ""
        for part in (text[:cut], text[cut:]):
            c, r = scanner.feed(part)
            content += c
            reasoning += r
        tail_c, tail_r = scanner.flush()
        content += tail_c
        reasoning += tail_r
        assert content == "CONTENT", f"cut={cut} content={content!r}"
        assert reasoning == "REASON", f"cut={cut} reasoning={reasoning!r}"


def test_unclosed_think_treated_as_reasoning():
    """被截断的 thinking 应归入思考，而不是当成正文。"""
    text = "<think>分析到一半就被截断了，正文还没开始"
    content, reasoning = split_think(text)
    assert content == ""
    assert reasoning.startswith("分析到一半")
    assert ThinkScanner().feed(text)[0] == ""


def test_case_insensitive_markers():
    content, reasoning = split_think("<THINK>大写</Think>正文")
    assert content == "正文", content
    assert reasoning == "大写", reasoning


def test_char_counters_match_split():
    text = "<think>思考部分</think>正文部分"
    scanner = ThinkScanner()
    content, reasoning = scanner.feed(text)
    tail_c, tail_r = scanner.flush()
    content += tail_c
    reasoning += tail_r
    assert content == "正文部分"
    assert reasoning == "思考部分"
    assert scanner.content_chars == len(content)
    assert scanner.reasoning_chars == len(reasoning)
    assert scanner.saw_reasoning is True


def test_looks_reasoning_only():
    assert looks_reasoning_only("<think>全是思考，没有任何正文产出</think>") is True
    assert looks_reasoning_only("<think>一点思考</think>然后有大量正文内容在这里") is False
    assert looks_reasoning_only("没有思考标签的正文") is False


def test_looks_like_reasoning_exhausted_requires_truncation_and_unclosed_think():
    # 真实故障形态：截断 + 未闭合 think + 正文没有 JSON
    broken = "<think>The JSON parsing failed at line 11. Let me check testData for unescaped quotes"
    assert looks_like_reasoning_exhausted(broken, finish_reason="length") is True
    # 没有截断标志 → 不是预算耗尽
    assert looks_like_reasoning_exhausted(broken, finish_reason="stop") is False
    # 正常结束且思考闭合 → 不是预算耗尽
    closed = "<think>思考完了</think>[{\"id\":\"TC-A-001\"}]"
    assert looks_like_reasoning_exhausted(closed, finish_reason="length") is False
    # 未闭合且已写了半截 JSON → 属普通语法错误，不是预算耗尽
    partial = "<think>思考中…</think>[{\"id\":\"TC-A-001\""
    assert looks_like_reasoning_exhausted(partial, finish_reason="length") is False
    # JSON 写在 think 内部（正文一无所获）→ 仍属预算耗尽，因为可用正文为空
    assert looks_like_reasoning_exhausted("<think>x[{\"id\":\"A\"}]", finish_reason="length") is True
