# DADC当前阶段实验与证明结论报告

## 1. 报告目的

本报告固定当前版本实际证明的能力、证据和尚未证明的边界，是受限只读审计，不是产品宣传材料。只有明确自动测试及关键断言、成功命令、可核查案例/manifest/结果文件，或 Schema 约束及对应验证测试，才写“通过”；只有实现写“已实现，尚未充分验证”；只有文档写“设计目标，尚未验证”。本次完整测试未成功启动，故静态测试证据与本次运行结果分开记录。

## 2. 版本与测试环境

| 项目 | 证据 |
|---|---|
| 当前分支 | `main` |
| Git提交号 | `49b0ef3f82c6bdfc5744fd86a05673b153d874ea` |
| 最近版本标签 | `v1.5.0-rc1` |
| 项目版本 | `1.5.0`（`pyproject.toml`）；`README.md` 仍有工具 1.4.0 字样，属口径滞后 |
| Schema版本 | `1.0`（`schemas/v1.0/*.schema.json`） |
| Python版本 | 可用解释器 Python `3.12.10`；项目 `.venv` 失效，`py -3.12` 注册项无法创建进程 |
| 测试时间 | `2026-08-20T10:53:08+08:00` |
| 测试命令 | `py -3.12 -m unittest discover -s tests -v`；未进入 unittest |
| 单元测试总数/通过/失败 | 本次实际 `0/0/0`，即“本次未重新执行测试”，不是通过；静态检索到 56 个 `def test_...` 方法 |
| Warehouse校验 | `..\DADC_DATA\warehouse` 存在；尝试校验时在 CLI 导入阶段因缺少 `h5py` 中止，未产生校验结果，记“未验证” |
| 工作区 | 审计开始时 `git status --short` 为空；交付后仅新增本报告 |

未安装依赖，未联网，未启动 AEDT/HFSS/PyAEDT/其他求解器，未迁移、入库或生成示例。

## 3. 当前数据模型

九类核心实体是九种核心**数据实体**，不是九种器件类别：

| 实体 | 简要语义 |
|---|---|
| Device | 器件身份、类别、物理域、profile 扩展入口 |
| DesignRevision | 设计修订、几何/拓扑及 Artifact 引用 |
| Study | 研究范围、Run 集合和 coupling edge |
| Run | 仿真、实验、文献、处理或优化活动及状态 |
| Observable | 曲线、响应、表、标量或场及单位/数据引用 |
| Metric | 指标、来源 Observable、算法和 Run 引用 |
| Artifact | 原生/原始/HDF5/Parquet/日志/脚本/证据文件登记 |
| Validation | 对象、方法、阈值、结果和证据 |
| Provenance | 来源、主体、脚本和时间链 |

JSON 负责元数据和关系；HDF5 负责曲线、复数数组和场数据，复数采用 `real`/`imaginary`；Parquet 负责查询索引；原生文件负责复现证据；SHA-256 负责完整性检查。不能表述为“所有数据都存储在 JSON 中”。证据：`README.md`、`schemas/v1.0/`、`src/dadc/integrity.py`、`src/dadc/repository.py`。

## 4. 已有数据案例清单

| 案例名称 | 器件类别 | 物理域 | 数据来源类型 | Run activity_type | 原始格式 | 是否真实数据 | 受控测试夹具 | 适配器 | 主要Observable | 主要Metric | 测试/证据 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| HFSS带通滤波器 | `rf_filter` | 电磁 | 仿真生成；仓库声明为 AEDT Student 2025 R2.4 导出 | `simulation_run`、`data_processing` | `.s2p` MA | 是仿真结果，非实验 | 否 | `touchstone_rf_filter` | 复数S参数、派生dB | `metric_hfss_bandpass_real_001_bandwidth_3db` 等 | `tests/fixtures/bandpass_filter_run_001_HFSSDesign1.s2p`；`examples/intake/hfss_bandpass_real_001.dadc.json`；`tests/test_touchstone_ingestion.py` |
| 合成天线验收案例（非PyAEDT实算） | `antenna` | 电磁 | 受控生成 | `simulation_run` | 测试时生成的原始/HDF5 | 否 | 是 | `src/dadc/demo.py` | `obs_ant_s_complex`、`obs_ant_s11_db` | `metric_ant_resonance` | `tests/test_acceptance.py` 的 `test_01...`、`test_04...`、`test_05...` |
| 厂商RF电感形态测试 | `inductor` | 电磁 | **模拟的厂商测量和数据表声明**，非真实厂商文件 | `experiment_run`、`literature_record`、`data_processing` | 临时 `.s2p`、最小伪PDF | 否 | 是 | `touchstone_inductor` | S参数、差分阻抗、L/Q | 参考频率有效电感等 | `tests/test_inductor_ingestion.py`；下载测试仅为 `tests/test_inductor_downloader.py` mock |
| 电热功率电阻参考求解 | `power_resistor` | 电磁、热 | DADC参考有限差分仿真 | 仿真×2、处理×2 | bundle JSON、网格/场CSV、日志→HDF5 | 确定性数值解，非实验/跨求解器 | 是 | `joule_thermal_field_bundle` | 电势、电场、焦耳损耗、温度、热流 | 最大温度、总电功率、热阻 | `scripts/generate_joule_thermal_resistor.py`；`tests/test_field_bundle_ingestion.py` |
| 热敏电阻CSV实验夹具 | `thermistor` | 电磁、热 | 受控测试夹具 | `experiment_run`、`data_processing` | CSV | 否 | 是 | `tabular_experiment_csv` | 电压、电流、复阻抗 | 最大电流、最小阻抗幅值 | `examples/intake/generic_experiment.csv`、对应manifest、`tests/test_tabular_ingestion.py` |
| 光电二极管光谱CSV夹具 | `photodiode` | 光学、电磁 | 受控测试夹具 | `experiment_run`、`data_processing` | 分号CSV | 否 | 是 | `tabular_experiment_csv` | 响应度、光电流 | `metric_photodiode_spectral_exp_001_maximum_responsivity` | `examples/intake/photodiode_spectral_experiment.csv`、对应manifest、`tests/test_tabular_cross_device.py` |

`scripts/generate_patch_antenna.py` 及 `tests/test_pyaedt_generator_contract.py` 仅证明生成器契约；仓库无生成后的 `.aedt`/`.s1p`，不能称 PyAEDT 贴片已求解入库。电感下载器有固定哈希设计，但本次未联网且仓库无真实厂商 S2P/PDF，不能把夹具称为厂商测量数据。

## 5. 自动测试结果

下列“通过”指存在明确测试和关键断言，不代表本次重新跑通。

| 类别 | 相对路径和具体方法 | 结论 |
|---|---|---|
| Schema验证 | `tests/test_acceptance.py::DADCV10AcceptanceTests.test_10_all_generated_examples_pass_schema_python_and_storage_checks`；`tests/test_touchstone_ingestion.py::RealTouchstoneRepositoryTests.test_ingested_repository_is_valid_and_preserves_raw_bytes` | 通过（静态证据）；本次未运行 |
| 共用核心结构 | `tests/test_acceptance.py::...test_01_antenna_and_filter_use_same_core_structure`；`tests/test_warehouse_ingestion.py::...test_filter_and_antenna_share_core_and_indexes_are_rebuilt` | 通过 |
| Touchstone | `tests/test_touchstone_parser.py::...test_real_hfss_ma_file_is_parsed_without_losing_complex_values`；`tests/test_touchstone_ingestion.py::...test_canonical_hdf5_contains_real_and_imaginary_arrays` | 通过 |
| 通用CSV | `tests/test_tabular_ingestion.py::...test_real_and_complex_curves_ingest_without_core_schema_changes`；`tests/test_tabular_cross_device.py::...test_semicolon_spectrum_is_normalized_and_traceable` | 通过（受控夹具） |
| 判重 | `tests/test_warehouse_ingestion.py::...test_renamed_identical_source_is_deduplicated_by_bytes`；`tests/test_tabular_ingestion.py::...test_identical_csv_bytes_are_deduplicated` | 通过 |
| quarantine | `tests/test_warehouse_ingestion.py::...test_id_conflict_and_unknown_format_are_quarantined_without_mutation`；`tests/test_tabular_ingestion.py::...test_nonmonotonic_axis_is_quarantined_without_creating_warehouse`；`tests/test_field_bundle_ingestion.py::...test_companion_tampering_is_quarantined_before_case_creation` | 通过 |
| 失败运行 | `tests/test_acceptance.py::...test_06_failed_run_is_preserved_and_retry_links_to_parent` | 通过（受控） |
| 多物理场 | `tests/test_acceptance.py::...test_07_multiphysics_coupling_is_explicit`；`tests/test_field_bundle_ingestion.py::...test_repository_and_multiphysics_activity_chain_are_valid`、`test_every_field_has_complete_coordinate_mesh_and_condition_metadata` | 通过（表达/参考案例） |
| Schema迁移 | `tests/test_acceptance.py::...test_08_v09_run_migrates_to_v10_without_mutating_source` | 通过（仅Run v0.9→v1.0） |
| SHA-256篡改 | `tests/test_acceptance.py::...test_09_deleted_or_tampered_artifact_fails_integrity`；Touchstone/CSV对应tamper测试 | 通过 |
| Metric追溯 | `tests/test_touchstone_ingestion.py::...test_metric_trace_reaches_source_run_raw_file_and_adapter`；CSV和field bundle对应trace测试 | 通过（案例范围） |
| Adapter目录 | `tests/test_adapter_catalog.py::AdapterCatalogTests.test_every_installed_adapter_has_one_stable_capability_record`、`test_cli_prints_machine_readable_adapter_catalog`（断言5个adapter） | 通过 |
| Preflight | `tests/test_ingestion_preflight.py` 的4个测试覆盖缺元数据、ready、unsupported、ambiguous且不修改warehouse | 通过 |
| 新器件不改核心Schema | `tests/test_acceptance.py::...test_03_new_device_profile_does_not_modify_existing_schema_or_data`；field bundle及cross-device对应测试 | 通过（样例范围） |
| 其他 | `tests/test_touchstone_ingestion.py::...test_metrics_and_physical_screens_are_reproducible`；`tests/test_field_bundle_ingestion.py::...test_declared_numerical_validations_are_evidence_backed`；下载器mock和PyAEDT契约测试 | 物理证据部分验证；真实下载/求解未验证 |

## 6. 验收证明矩阵

| ID | 声明 | 输入案例 | 自动测试/命令 | 证据路径 | 结果 | 严格边界 |
|---|---|---|---|---|---|---|
| 1 | 天线/滤波器同核心结构 | 合成天线/滤波器、共享warehouse测试 | `test_01_...`、`test_filter_and_antenna_...` | `tests/test_acceptance.py`；`tests/test_warehouse_ingestion.py` | 通过 | 天线为夹具，无PyAEDT实算 |
| 2 | 避免大量null | 合成仓库、两类CSV器件 | `test_02_...`、`test_device_extensions_remain_sparse...` | `tests/test_acceptance.py`；`tests/test_tabular_cross_device.py` | 通过 | `coordinate_system_ref:null`为显式语义 |
| 3 | 新器件不改核心Schema | 声学profile、功率电阻、CSV器件 | `test_03_...`等 | 上述三个测试文件 | 通过 | 非任意未来器件保证 |
| 4 | Metric追溯Observable/Run/Artifact | 天线、HFSS、CSV、电热 | 多个trace测试 | `tests/test_acceptance.py`等 | 通过 | 本次未实跑共享warehouse trace |
| 5 | 区分原始/计算/人工值 | 合成天线、电感夹具 | `test_05_...`、电感origin测试 | `tests/test_acceptance.py`；`tests/test_inductor_ingestion.py`；`schemas/v1.0/metric.schema.json` | 通过 | 不证明值真实/正确 |
| 6 | 保存失败运行 | 合成failed/retry | `test_06_...` | `tests/test_acceptance.py`；`schemas/v1.0/run.schema.json` | 通过 | 未验证真实求解器崩溃采集 |
| 7 | 表达多物理场 | 合成三域、电热参考 | `test_07_...`、field bundle测试 | `tests/test_acceptance.py`；`tests/test_field_bundle_ingestion.py` | 通过 | 未证明商业求解器互操作 |
| 8 | Schema迁移 | 受控v0.9 Run | `test_08_...` | `tests/test_acceptance.py`；`docs/migration-v0.9-to-v1.0.md` | 通过（限定） | 非全实体迁移 |
| 9 | 删除/篡改后失败 | HFSS、CSV、PDF/bundle、合成Artifact | tamper/missing测试 | 对应测试文件；`src/dadc/integrity.py` | 通过 | 本次warehouse未实校；哈希不证明科学正确 |
| 10 | 示例通过Schema/Python | 临时demo | `test_10_...` | `tests/test_acceptance.py`；`schemas/v1.0/` | 通过（测试定义）；本次未运行 | 当前无 `examples/generated` |
| 11 | 相同字节判重 | 改名s2p、相同CSV | 两个dedup测试 | `tests/test_warehouse_ingestion.py`；`tests/test_tabular_ingestion.py` | 通过 | 非语义相似判重 |
| 12 | 未知/不完整数据隔离 | 未知、冲突、非单调、篡改bundle | quarantine测试 | 三个入库测试文件 | 通过 | 未穷举任意格式 |
| 13 | 入库前识别adapter/缺元数据 | 缺manifest、完整、未知、歧义 | 4个preflight测试 | `tests/test_ingestion_preflight.py`；`src/dadc/ingestion/registry.py` | 通过 | Preflight不做full case validation |

## 7. 指标追溯链

选择 `metric_hfss_bandpass_real_001_bandwidth_3db`：

`Metric: metric_hfss_bandpass_real_001_bandwidth_3db`

→ `Observable: 未完全验证`。`src/dadc/ingestion/importer.py` 定义原始复数 ID `obs_hfss_bandpass_real_001_s_parameters_complex`，但本次断言未直接列出 bandwidth Metric 的 `source_observable_ids`，故不编造其直接指向原始复数还是派生dB。

→ `Run: run_hfss_bandpass_real_001_hfss`、`run_hfss_bandpass_real_001_touchstone_import`（`tests/test_touchstone_ingestion.py::...test_metric_trace_reaches_source_run_raw_file_and_adapter` 明确断言）。

→ `Artifact: art_hfss_bandpass_real_001_touchstone_raw`、`art_hfss_bandpass_real_001_results_h5`、`art_hfss_bandpass_real_001_adapter_script`；角色含 `raw_input`、`result_hdf5`、`script`。

→ 原始文件 `tests/fixtures/bandpass_filter_run_001_HFSSDesign1.s2p`；入库相对路径 `cases/hfss_bandpass_real_001/raw/bandpass_filter_run_001_HFSSDesign1.s2p`；测试钉住 SHA-256 `6b8c8e41071e29f262ac3c67cea69a01b81af1cd7d646857108ffc1fbffbe620`。

→ `Provenance: prov_hfss_bandpass_real_001_hfss`、`prov_hfss_bandpass_real_001_touchstone_import`。

→ `Validation: 未验证为该Metric追溯返回项`。实现定义相关 `val_hfss_bandpass_real_001_passivity`、`val_hfss_bandpass_real_001_reciprocity`，证据相对路径 `cases/hfss_bandpass_real_001/evidence/touchstone_checks.json`，但测试未断言其在该Metric返回链中。

因此链已直接证明到Run、Artifact、原始文件和Provenance；精确直接Observable与Validation闭环未完全确认。

## 8. 可信能力分级

| 维度 | 结论 | 证据 | 限制 |
|---|---|---|---|
| 结构合规性 | 已验证 | Schema严格约束、`report.valid`测试 | 本次未运行；限已有案例 |
| 来源可追溯性 | 已验证 | 引用约束及Touchstone/CSV/电热trace测试 | 案例范围；HFSS到Validation闭环未确认 |
| 文件完整性 | 已验证 | SHA-256 Schema/实现及tamper/dedup测试 | 字节一致不等于真实/正确 |
| 数据处理可审计性 | 部分验证 | 原始/派生分离、计算/脚本Artifact、转换链测试 | 未覆盖所有算法/人工操作 |
| 仿真可复现性 | 部分验证 | HFSS原始s2p；电热脚本、参数、日志、Validation | HFSS缺完整模型上下文；未复算商业求解器 |
| 物理正确性 | 部分验证 | 无源/互易筛查；收敛、守恒、网格差异 | 无独立实验或真实跨求解器复核 |
| 实验真实性 | 尚未验证 | CSV manifest、电感实验语义 | 都是夹具，无真实实验/校准证书/厂商原件 |
| 跨求解器一致性 | 尚未验证 | Schema支持、demo有合成比较 | 合成不构成真实证明 |
| 跨仪器一致性 | 尚未验证 | Schema可表达实验比较 | 无多仪器对比案例 |

## 9. 当前尚未证明的内容

- 覆盖所有器件类别：尚未验证；九实体不是九类器件。
- 任意厂商PDF自动抽取：尚未验证；固定下载脚本和伪PDF测试不等于通用抽取。
- OCR和表格语义抽取：无法从当前仓库确认。
- 知识图谱：无法从当前仓库确认；实体关系模型不等于知识图谱产品。
- 向量检索：尚未验证；`docs/00-overview.md` 将读取/检索接口列为后续。
- 团队智能体接口：尚未验证。
- 多人并发：无法从当前仓库确认。
- 服务器或对象存储：无法从当前仓库确认；现证据为本地文件系统。
- 智能体运行追踪：无法从当前仓库确认；Run/Provenance不自动等于该系统。
- 参数优化轨迹：尚未验证；虽有 `optimization_step` 枚举，无完整轨迹案例。
- 大规模真实数据压力测试：尚未验证。

## 10. 与团队对接前可以保留的稳定成果

原始数据不可覆盖；九实体语义；来源追溯；SHA-256；显式单位；复数 `real`/`imaginary`；原始值与派生值分离；Schema版本及限定非破坏迁移；适配器隔离、目录、preflight和quarantine；结构、存储、判重、篡改、追溯和多物理场回归测试资产。它们是工程数据治理能力，不替代科学真实性证明。

## 11. 当前阶段严格结论

1. **已经证明的**：九实体Schema、存储分工、SHA-256机制，以及HFSS Touchstone、通用CSV、参考电热bundle的测试断言；案例范围内的结构共用、稀疏字段、来源区分、原件保留、判重、隔离和部分Metric追溯。
2. **部分证明的**：数据处理审计、仿真复现、物理正确性；缺完整HFSS复算上下文、真实跨求解器/实验符合性和完整Metric→Validation闭环。
3. **尚未证明的**：全部器件、真实实验、真实厂商样本落地、通用PDF/OCR、知识图谱、向量检索、智能体接口、并发、服务器/对象存储、智能体追踪、完整优化轨迹、压力、跨求解器/仪器一致性。
4. **团队共同确认的**：目标器件/来源、数据授权与校准口径、warehouse运行基线、跨平台验证方案、服务化边界、智能体接口与九实体/Provenance映射。

严格结论：**DADC当前已经形成有实现、Schema、受控案例和自动测试断言支撑的异构科研数据底座原型，并对若干代表性器件、来源和数据形态完成最小工程验证。现有证据支持结构合规、来源追溯、文件完整性和部分处理链审计；但本次测试及共享Warehouse校验均未成功启动，真实实验、跨求解器/仪器和平台化证据不足，尚不能证明全部器件覆盖、科学真实性、跨平台部署或与团队智能体无缝集成。**

## 12. 可复现检查命令

以下为本次实际使用的只读证据命令；复合PowerShell调用按一次计。目录/文件创建是获准交付操作，不计入证据命令。

1. 检查并读取可能存在的 `AGENTS.md`。
2. `git branch --show-current`、`git rev-parse HEAD`、`git describe --tags --abbrev=0`、`git status --short`、Python探测、`rg --files ...`。
3. `py -0p`，并限定检索版本、架构和九实体关键字。
4. `rg -n "^\s*(class Test|def test_)|assert[A-Z]|assert |with self\.assertRaises|subTest" tests`
5. 读取三份intake manifest并检索案例ID、来源、activity、adapter、Observable/Metric。
6. `py -3.12 -m unittest discover -s tests -v`（未进入unittest）。
7. 检查可用Python和warehouse，执行版本并尝试 `python -m dadc validate "..\DADC_DATA\warehouse"`（缺`h5py`中止）。
8. `rg -n "^\s*def test_" tests` 并检索Schema/完整性/追溯/适配器约束。
9. `rg --files scripts examples tests/fixtures` 并检索各案例关键字。
10. 检索importer、电感和field bundle中的Metric/Observable/Artifact/Provenance/Validation、路径和夹具构造。
11. `Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"`，并检索OCR、PDF/表格、知识图谱、向量、智能体、并发、对象存储、优化、压力、跨求解器/仪器。
12. 报告生成后执行 `git diff --stat` 和 `git status --short`。

---

证据更新时间：`2026-08-20T10:53:08+08:00`  
Git提交号：`49b0ef3f82c6bdfc5744fd86a05673b153d874ea`
