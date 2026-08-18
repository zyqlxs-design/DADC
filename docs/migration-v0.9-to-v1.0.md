# v0.9 → v1.0 迁移说明

`dadc migrate` 对输入 JSON 做深拷贝，不覆盖原文件。当前冻结迁移路径支持 Run：

- `run_type=simulation|experiment|literature|processing|optimization` 映射到五个冻结的 `activity_type`；
- `status=completed|error` 映射到 `succeeded|failed`；
- `parent_id` 映射到 `parent_run_id`；
- 追加 `migration_history`，记录来源版本、目标版本、方法和执行时间。

迁移输出若已存在，命令行会拒绝覆盖。迁移后的记录仍需通过 V1.0 JSON Schema 和仓库引用校验；迁移不会替代数据文件完整性验证。

