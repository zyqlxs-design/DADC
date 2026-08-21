# DADC 1.8.0 数据沟通智能体阶段验收

- 总体状态：`passed`
- 评价方式：只使用客观通过/失败检查，不使用主观评分。
- 自动化测试：`73 passed, 0 failed`。
- 物理范围：本机未调用Windows AEDT；此前的PyAEDT物理后端证据保持由1.7.0物理验收报告覆盖。

## 已验收内容

| 检查 | 状态 | 客观证据 |
|---|---|---|
| 知识证据进入智能体上下文 | `passed` | 离线闭环向规划器提供3个带来源与哈希的知识块。 |
| DADC历史优化数据进入上下文 | `passed` | 自动化测试从有效warehouse提取器件匹配的优化Run、参数和Metric。 |
| LLM参数输出格式契约 | `passed` | `agent_parameter_selection v1.0` JSON Schema已加入发布包。 |
| 参数白名单和预算门禁 | `passed` | 越界值在生成优化计划和调用求解器之前被测试拒绝。 |
| 显式执行批准 | `passed` | 未提供 `--approve-execution` 时不创建执行目录。 |
| 测试知识与物理执行隔离 | `passed` | 物理后端引用 `test_only` / `test_fixture` 知识时在后端调用前拒绝。 |
| 自动搜索与失败保留 | `passed` | 示例保留4个搜索点，其中3个成功、1个失败。 |
| 最优点独立复算 | `passed` | `verify_001`独立执行，结果为0.25。 |
| 客观阈值判断 | `passed` | 程序根据 `<= 0.25` 规则计算状态，不接受LLM自报成功。 |
| 优化可读报告 | `passed` | Markdown报告列出全部搜索点、参数、状态、指标和证据文件数。 |
| AI决策来源追溯 | `passed` | 生成计划记录provider、model、选择哈希、知识chunk及DADC历史Run ID。 |
| Schema跨平台一致性 | `passed` | JSON Schema按规范化后的语义比较；Windows换行、缩进和UTF-8 BOM不会产生伪冲突，字段真实变化仍被拒绝。 |

## 本阶段能力边界

- DeepSeek连接器使用JSON输出，只负责从人工声明的允许值中选择参数。
- 真实DeepSeek API质量和真实AEDT执行需要在用户Windows环境验收。
- 当前是单轮AI辅助候选网格生成，不是多轮主动学习。
- 尚未实现代理模型、贝叶斯优化、网格独立性和实验符合性验证。
- 有限预算的LLM辅助调优不能保证一定达到工程目标；未达阈值时输出 `target_not_met` 并保留证据。
