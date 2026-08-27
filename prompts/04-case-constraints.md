> prompt v0.2.0 | 用例生成约束层 | 注入：阶段 3 输出要求区（extra_requirements）

# 用例生成约束（必须逐条遵守）

## A. 静态约束（测试原理）
1. **design_method**：每条用例标注采用的测试设计方法（等价类划分 / 边界值分析 / 判定表 / 状态迁移 / 场景法 / 错误推测），作为可选字段。
2. **步骤可定位**：`target` 用「页面元素中文名 (data-testid)」格式；动作取自动作枚举（goto/fill/click/selectOption/check/hover/press/reload/expectVisible/expectText/expectUrl/waitFor）。
3. **预期可断言**：URL / 文本 / 状态 / 金额；金额给精确值（¥ 千分位、2 位小数，如 ¥4,727.05）。
4. **反模式禁止**：
   - 模糊步骤（"随便点点"、"执行操作"）；
   - 不可验证预期（"表现正常"、"无异常"）；
   - 用例互相依赖 / 共享可变状态；
   - 重复用例（同步骤 + 同预期）；
   - 为凑数量生成的同质用例。
5. **标签口径**：P0 必带 `@smoke`；全量带 `@regression`；按模块带模块标签（@auth/@cart/@checkout…）。
6. **输出前自检**：对照校验规则自查（id 格式 TC-<MOD>-<SEQ>、module/priority 枚举、requirement 为 REQ-*、steps/expected 非空、id 全局唯一），再输出 JSON。

## B. 学习约束（来自参考示例，动态填充——须与示例库的设计逻辑保持一致）

{{learned_patterns}}
