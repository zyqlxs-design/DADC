# DADC 自动调优结果示例

- 优化任务：`agent_fixture_tuning_001`
- 器件：`generic_component` / `analytic_contract_fixture`
- 后端：`analytic_fixture`（物理求解器：`false`）
- 目标：`analytic_mismatch_score`，策略：`minimize`
- 最优搜索点：`search_001`
- 最优搜索指标：`0.25 score`

## 参数点与结果

| 试算 | 类型 | 状态 | 参数 | 指标 | 证据文件数 |
|---|---|---|---|---:|---:|
| `search_001` | `search` | `succeeded` | patch_length_mm=10.0 mm, patch_width_mm=5.0 mm | 0.25 score | 2 |
| `search_002` | `search` | `succeeded` | patch_length_mm=10.0 mm, patch_width_mm=4.0 mm | 2.25 score | 2 |
| `search_003` | `search` | `succeeded` | patch_length_mm=9.0 mm, patch_width_mm=5.0 mm | 1.25 score | 2 |
| `search_004` | `search` | `failed` | patch_length_mm=9.0 mm, patch_width_mm=4.0 mm | — | 2 |
| `verify_001` | `independent_verification` | `succeeded` | patch_length_mm=10.0 mm, patch_width_mm=5.0 mm | 0.25 score | 2 |

## 结果边界

这是离线解析夹具生成的可查看样例，只证明搜索、失败保留、最优点选择、独立复算和报告链路，不是LLM质量或HFSS物理结果。真实PyAEDT证据包可以使用同一 `dadc optimization-report` 命令生成对应报告。
