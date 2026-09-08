"""离线测试用的确定性 LLM stub。"""
from __future__ import annotations

import json
import re

from prd2tc.llm import LLMResult


def stub_chat(cfg, user_text: str, **kwargs) -> LLMResult:
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
                  "priority":"P1","tags":["@regression"],"requirement":[requirement_id],
                  "steps":[{"action":"goto","target":"/","data":""}],"expected":["页面渲染"]}]
        content = "```json\n" + json.dumps(cases, ensure_ascii=False) + "\n```"
    return LLMResult(content=content, model="stub")
