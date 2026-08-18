# DADC V1.0 验收矩阵

| 编号 | 冻结要求 | 自动测试 | 主要实现证据 |
|---:|---|---|---|
| 1 | 天线和滤波器使用同一核心结构 | `test_01_antenna_and_filter_use_same_core_structure` | 同一 Device/Observable Schema，器件特有字段位于 profile |
| 2 | 不需要大量 null 字段 | `test_02_optional_fields_do_not_create_sparse_null_rows` | 不适用字段省略；null 率阈值小于 1% |
| 3 | 新器件 Schema 不修改已有数据 | `test_03_new_device_profile_does_not_modify_existing_schema_or_data` | 动态注册声学器件 profile，并比较核心 Schema/JSON 的 SHA-256 |
| 4 | 指标可追溯到原始结果 | `test_04_metric_traces_to_original_complex_result` | Metric → Observable → Run → HDF5 Artifact → Provenance |
| 5 | 区分原始、计算和人工值 | `test_05_raw_calculated_and_manual_values_are_distinguishable` | `value_origin` 及条件必填的 derivation/manual context |
| 6 | 保存失败运行 | `test_06_failed_run_is_preserved_and_retry_links_to_parent` | failed Run、failure 结构、日志 Artifact、重试 `parent_run_id` |
| 7 | 表达多物理场耦合 | `test_07_multiphysics_coupling_is_explicit` | 三物理域、三 Run、两个有向 coupling edge、场元数据 |
| 8 | Schema 版本迁移 | `test_08_v09_run_migrates_to_v10_without_mutating_source` | 非破坏性 v0.9 → v1.0 Run 迁移和迁移历史 |
| 9 | 删除/篡改后校验失败 | `test_09_deleted_or_tampered_artifact_fails_integrity` | 对删除与篡改分别复算大小及 SHA-256 |
| 10 | 示例通过 Schema/Python 测试 | `test_10_all_generated_examples_pass_schema_python_and_storage_checks` | JSON Schema、引用、HDF5、Parquet、SHA-256 总验收 |

附加约束由总验收同时覆盖：复杂 S 参数与复数场必须指向包含 `real`/`imaginary` 的 HDF5 Group；所有场数据必须包含坐标、网格、分量、条件、位置和归一化；`validation.json` 中每项验证均包含方法、阈值、结果、证据和时间。

## 真实 HFSS 数据附加验收

`tests/test_touchstone_ingestion.py` 使用真实 AEDT Student 2025 R2.4 `.s2p`
文件验证：源文件字节及 SHA-256 不变；Touchstone `MA` 正确转换为复数
real/imaginary；S11/S21/S12/S22 端口顺序正确；指标能追溯到父仿真 Run、
原始文件、HDF5、适配脚本和 Provenance；篡改真实 `.s2p` 后总校验失败。

该测试不宣称完成网格独立性、求解器收敛、跨求解器或实验验证；这些证据
不在 Touchstone 文件中，仓库明确保留为后续验证任务。

## 厂商电感异构数据附加验收

`tests/test_inductor_ingestion.py` 验证同一 Case 中的 `experiment_run`、
`literature_record` 与 `data_processing` 不混用；厂商 S2P、数据手册、复数
S 参数、复数差分阻抗、L/Q 曲线与指标均可追溯；手册规格指向 PDF Artifact；
篡改 PDF 后完整性校验失败。`tests/test_touchstone_parser.py` 还覆盖厂商
Windows-1252 注释，并验证电感与滤波器追加时核心 Schema 字节不变。

## 电—热场异构数据附加验收

`tests/test_field_bundle_ingestion.py` 验证主 bundle 对九个相对路径伴随文件逐一钉住
SHA-256；网格坐标、四边形连接、电/热场被标准化到两个 HDF5；五个场
Observable 均含冻结格式要求的完整场元数据；温度指标追溯能够跨 coupling edge
到达上游焦耳损耗和电求解原始文件；收敛、功率守恒与网格差异验证均有证据；
篡改任一伴随场文件会在 Case 创建前被隔离；功率电阻 profile 追加到既有滤波器
warehouse 时核心 Schema 字节不变。当前全套为 39 个 Python 测试。
