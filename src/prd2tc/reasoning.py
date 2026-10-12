"""推理内容（thinking）与正文的分离。

背景（实测自 MiniMax-M3 的真实原始输出）：M3 会把思考过程直接写在 `content` 里，
用 `<think>…</think>` 包裹。这带来三个问题：

1. **指标失真**：`chars`/`token/s` 把思考算成产出，看板显示「正在产出 1.2 万 token」，
   其实是在思考，人无法判断进度是否正常；
2. **提取风险**：`extract_cases` 取「第一个 `[` 到最后一个 `]`」，思考里若出现示例 JSON
   就可能被当成真用例；
3. **预算耗尽**：思考吃光 `max_tokens` 后正文一个字符都没写，`finish_reason=length`，
   整轮输出归零（实测有一次 26,504 字符里 100% 是思考，可用产出 0）。

本模块只做纯文本分离，不依赖 HTTP/配置，便于离线测试。
"""
from __future__ import annotations

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
# carry 需要能兜住被切开的标记：取最长标记长度 - 1
_MAX_MARKER = max(len(THINK_OPEN), len(THINK_CLOSE))


def _find(lowered: str, marker: str, start: int) -> int:
    return lowered.find(marker, start)


class ThinkScanner:
    """增量分离思考与正文。

    流式场景下标记可能被切成两块（`"<thi"` + `"nk>"`），故保留 carry 缓冲；
    未分类的尾部字符不计入任何一边，等下一块到达后再判定。
    """

    def __init__(self) -> None:
        self.in_think = False
        self.carry = ""
        self.content_chars = 0
        self.reasoning_chars = 0

    def feed(self, text: str) -> tuple[str, str]:
        """喂入一个流式分片，返回 (正文片段, 思考片段)。"""
        if not text:
            return "", ""
        buf = self.carry + text
        self.carry = ""
        lowered = buf.lower()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        i, n = 0, len(buf)
        while i < n:
            if not self.in_think:
                j = _find(lowered, THINK_OPEN, i)
                if j == -1:
                    keep = n - i
                    tail = min(keep, _MAX_MARKER - 1)
                    if keep - tail:
                        content_parts.append(buf[i:n - tail])
                        self.content_chars += keep - tail
                    self.carry = buf[n - tail:] if tail else ""
                    break
                if j > i:
                    content_parts.append(buf[i:j])
                    self.content_chars += j - i
                self.in_think = True
                i = j + len(THINK_OPEN)
            else:
                j = _find(lowered, THINK_CLOSE, i)
                if j == -1:
                    keep = n - i
                    tail = min(keep, _MAX_MARKER - 1)
                    if keep - tail:
                        reasoning_parts.append(buf[i:n - tail])
                        self.reasoning_chars += keep - tail
                    self.carry = buf[n - tail:] if tail else ""
                    break
                if j > i:
                    reasoning_parts.append(buf[i:j])
                    self.reasoning_chars += j - i
                self.in_think = False
                i = j + len(THINK_CLOSE)
        return "".join(content_parts), "".join(reasoning_parts)

    def flush(self) -> tuple[str, str]:
        """结束时的收尾：把未分类的 carry 按当前状态归位。

        未闭合的 `<think>`（被截断）视为思考内容——这正是「预算被思考吃光」的特征。
        """
        if not self.carry:
            return "", ""
        leftover, self.carry = self.carry, ""
        if self.in_think:
            self.reasoning_chars += len(leftover)
            return "", leftover
        self.content_chars += len(leftover)
        return leftover, ""

    @property
    def saw_reasoning(self) -> bool:
        return self.reasoning_chars > 0 or self.in_think


def split_think(text: str) -> tuple[str, str]:
    """把一段完整文本拆成 (正文, 思考)。供非流式路径与测试使用。"""
    scanner = ThinkScanner()
    content, reasoning = scanner.feed(text or "")
    tail_content, tail_reasoning = scanner.flush()
    return content + tail_content, reasoning + tail_reasoning


def strip_think(text: str) -> str:
    """只取正文。"""
    return split_think(text)[0]


def looks_reasoning_only(text: str) -> bool:
    """判断输出是否「几乎全是思考、没有可用正文」。

    用于把「整轮输出归零」从泛化的门禁失败里识别出来，给出可行动的诊断。
    """
    content, reasoning = split_think(text or "")
    if not reasoning:
        return False
    return len(content.strip()) <= max(2, len(reasoning) * 0.01)


def looks_like_reasoning_exhausted(text: str, *, finish_reason: str = "") -> bool:
    """输出预算被思考耗尽的特征：被截断 + 未闭合的 think + 无正文。

    注意「无正文」用原始文本判断：模型可能写了半截 JSON 也算有产出，
    那种情况应走普通的语法错误反馈，而不是「预算耗尽」诊断。
    """
    if finish_reason not in {"length", "max_tokens"}:
        return False
    scanner = ThinkScanner()
    scanner.feed(text or "")
    content, _ = split_think(text or "")
    unfinished_think = scanner.in_think
    has_json_start = "[" in content or "{" in content
    return unfinished_think and not has_json_start
