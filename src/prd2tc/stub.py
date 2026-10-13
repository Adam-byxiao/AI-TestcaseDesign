"""离线测试用的确定性 LLM stub。"""
from __future__ import annotations

import json
import re

from prd2tc.llm import LLMResult


def stub_chat(cfg, user_text: str, *, on_delta=None, should_cancel=None, **kwargs) -> LLMResult:
    """离线确定性响应；支持流式钩子，使整条流式链路无需真实 API 即可测试。"""
    if "任务指令" not in user_text:
        raise AssertionError("user 消息缺少任务指令")
    if "PRD 全文" in user_text:        content = """# 需求分析报告（stub）

## 1. 功能模块清单
| 模块代号 | 模块名 | 页面/入口 | 需求点编号 |
| AUTH | 账号 | /login | REQ-AUTH-001 |
## 2. 角色清单
| 角色 | 说明 | 关键权限/状态 |
| 用户 | 普通用户 | 登录 |
## 3. 需求点明细
| REQ 编号 | 优先级 | 需求描述 | 验收标准 | 异常分支/边界 |
| REQ-AUTH-001 | P1 | 页面访问 | 页面成功渲染 | 无 |
## 4. 业务规则
| 编号 | 规则 | 影响面 | 测试要点 |
## 5. 非功能需求
| 编号 | 类别 | 可验证指标 |
## 6. 风险与疑点
| 风险 | 影响 | 应对 |
"""
    elif "分段需求分析报告" in user_text:
        content = """## 2. 角色清单
| 角色 | 说明 | 关键权限/状态 |
| 用户 | 普通用户 | 登录 |
## 4. 业务规则
| 编号 | 规则 | 影响面 | 测试要点 |
| 未定义 | 待确认 | - | - |
## 5. 非功能需求
| 编号 | 类别 | 可验证指标 |
| 未定义 | 待确认 | - | - |
## 6. 风险与疑点
| 风险 | 影响 | 应对 |
| 未定义 | 待确认 | - | - |
"""
    elif "需求分析报告" in user_text and "测试方案排版模板" in user_text:
        content = """# 测试方案（stub）

## 1. 测试范围
- In Scope: AUTH
## 2. 测试策略
- 功能测试
## 3. 优先级口径
- P0-P3
## 4. 风险与应对
- 无
## 5. 测试环境与数据
- stub
## 6. 准入 / 准出标准
- 通过
## 7. 用例量目标
- 1
"""
    else:
        requirement_ids = re.findall(r'"id"\s*:\s*"(REQ-[A-Z][A-Z0-9_]*-\d{3})"', user_text)
        requirement_id = requirement_ids[0] if requirement_ids else "REQ-AUTH-001"
        module = requirement_id.split("-")[1]
        cases = [{"id":"TC-STUB-001","module":module,"subModule":"STUB","title":"stub 用例",
                  "design_method":"场景法",
                  "priority":"P1","tags":["@regression"],"requirement":[requirement_id],
                  # 必须含**断言**动作：门禁要求每条用例至少一个 expect*
                  # （stub 原先只有 goto，会让所有端到端测试触发该门禁）
                  "steps":[{"action":"goto","target":"/","data":""},
                           {"action":"click","target":"主要操作入口","data":""},
                           {"action":"expectVisible","target":"操作结果区域","data":""},
                           {"action":"expectText","target":"操作结果区域","data":"页面成功渲染并显示预期内容"}],
                  "expected":["页面成功渲染并显示预期内容"]}]
        content = "```json\n" + json.dumps(cases, ensure_ascii=False) + "\n```"
    _emit(content, on_delta, should_cancel)
    cjk = sum(1 for ch in content if ch >= "\u2e80")
    completion = int(cjk + max(0, len(content) - cjk) / 4)
    return LLMResult(content=content, model="stub", prompt_tokens=len(user_text) // 3,
                     completion_tokens=completion, streamed=on_delta is not None,
                     usage_estimated=True)


# 分块大小：够小才能让 UI 看出「正在流式产出」，又不至于产生过多回调
STUB_CHUNK_CHARS = 120


def _emit(content: str, on_delta, should_cancel) -> None:
    """按块回调，模拟流式；顺带验证取消能被逐块感知。"""
    if on_delta is None:
        if should_cancel is not None and should_cancel():
            from prd2tc.llm import LLMCancelled
            raise LLMCancelled("任务已取消")
        return
    from prd2tc.llm import LLMCancelled
    for start in range(0, len(content), STUB_CHUNK_CHARS):
        if should_cancel is not None and should_cancel():
            raise LLMCancelled("任务已取消，已停止接收流式响应")
        on_delta(content[start:start + STUB_CHUNK_CHARS])
