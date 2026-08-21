# DADC V1.0 数据仓库

DADC V1.0 是一个可运行的异构器件工程数据仓库参考实现。它以九个冻结的一级实体为核心：`Device`、`DesignRevision`、`Study`、`Run`、`Observable`、`Metric`、`Artifact`、`Validation`、`Provenance`。天线、射频滤波器、电感和多物理场算例共享同一套核心 Schema；器件特有字段只进入独立的 profile 扩展，不进入全局横表。当前工具版本为 1.7.0.dev0，数据 Schema 仍固定为 1.0。

## 快速运行

需要 Python 3.10+。首次安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 scripts/create_examples.py examples/generated
python3 -m unittest discover -s tests -v
python3 -m dadc validate examples/generated
```

完整验收也可以直接运行：

```bash
make acceptance
```

`.github/workflows/acceptance.yml` 会在 Python 3.10 与 3.12 上重复执行完整测试、仓库总校验和最小智能闭环验收。

示例生成器拒绝覆盖非空目录。如需明确重建示例，使用：

```bash
python3 scripts/create_examples.py examples/generated --replace
```

## 文件职责

- JSON：九类实体、对象关系与仓库清单；
- JSON Schema：V1.0 格式和条件约束；
- HDF5：曲线、复数数组和多维场；
- Parquet：跨器件全局目录和指标查询；
- 原生格式：模型、求解输入、日志、验证证据；
- SHA-256：Artifact 对应文件的完整性校验。

示例 HDF5 的复数对象使用同组下的 `real` 和 `imaginary` 两个数据集保存；dB 数据是独立派生 Observable，并通过 `derived_from_observable_ids` 回指原始复数 Observable。场数据在 JSON 中强制记录坐标系、坐标单位、网格、分量、频率/时间条件、数据位置和归一化。

## 命令行

```bash
dadc create-demo TARGET_DIR
dadc validate REPOSITORY_DIR
dadc trace-metric REPOSITORY_DIR METRIC_ID
dadc migrate INPUT_JSON OUTPUT_JSON --target 1.0
dadc ingest-touchstone SOURCE.s2p TARGET_DIR --case-id CASE_ID --device-name NAME --filter-order N --source-timezone +08:00
dadc init-warehouse DATA_ROOT
dadc ingest SOURCE --warehouse DATA_ROOT/warehouse [intake options]
dadc knowledge-collect SOURCE_MANIFEST CORPUS_DIR
dadc knowledge-index CORPUS_DIR
dadc knowledge-search CORPUS_DIR QUERY
dadc optimize OPTIMIZATION_PLAN OUTPUT_DIR
```

`validate` 同时执行 JSON Schema、引用完整性、HDF5 数据引用、复数 real/imaginary 结构和 SHA-256 校验。删除或篡改任一受管 Artifact 时会返回非零退出码。

## 文档知识、受控调用与自动调优最小闭环

工具 1.6.0.dev0 新增独立于九对象事实库的可复现文档 corpus、可重建搜索投影、类型化仿真后端、预算化网格搜索、最优点独立复核，以及 `optimization_trace_bundle` 入库适配器。离线验收明确使用非物理解析夹具；真实后端只在 Windows + AEDT/PyAEDT 环境调用仓库固定的贴片天线脚本，不执行 LLM 生成的任意代码。架构边界、PowerShell 命令和真实运行前置条件见 [`docs/minimal-extensible-agent-loop.md`](docs/minimal-extensible-agent-loop.md)。

工具 1.7.0.dev0 增加知识来源契约 V1.1、共享知识与器件分区元数据、按器件/知识类型/主题过滤的检索，以及不使用主观评分的数据阶段验收报告。不同器件共用一个知识平台：PyAEDT/HFSS 通用知识标记为 `shared`，天线、射频滤波器和电感知识按 `device_classes` 隔离。官方种子清单包含 13 个已确认的 PyAEDT/HFSS API 与案例页面。设计与验收命令见 [`docs/data-and-knowledge-stage.md`](docs/data-and-knowledge-stage.md)。

## 导入真实 HFSS Touchstone 数据

工具 1.1.x 包含第一个真实数据适配器。它保留 AEDT 导出的 `.sNp` 原文件，并将 Touchstone 的 `RI`、`MA` 或 `DB` 复数表示确定性转换为 HDF5 `real`/`imaginary`。以仓库内真实 HFSS 文件为例，在 Windows PowerShell 中运行：

```powershell
& ".\.venv\Scripts\python.exe" -m dadc ingest-touchstone `
  "tests\fixtures\bandpass_filter_run_001_HFSSDesign1.s2p" `
  "examples\real_hfss" `
  --case-id "hfss_bandpass_real_001" `
  --device-name "HFSS official interdigital bandpass filter" `
  --filter-order 8 `
  --source-timezone "+08:00" `
  --operator-id "local_user" `
  --platform "Windows 10" `
  --solver-edition "Student"

& ".\.venv\Scripts\python.exe" -m dadc validate "examples\real_hfss"
& ".\.venv\Scripts\python.exe" -m dadc trace-metric "examples\real_hfss" `
  "metric_hfss_bandpass_real_001_bandwidth_3db"
```

`--source-timezone` 必须填写导出文件时 Windows 的实际时区；中国标准时间使用 `+08:00`，日本标准时间使用 `+09:00`。`--filter-order` 是人工确认值，如果模型实际阶数不是 8，必须改成真实值。目标目录必须不存在或为空，导入器拒绝覆盖已有仓库。

一次导入会产生两个有明确语义的 Run：HFSS `simulation_run` 保存原始 `.s2p`，其子级 `data_processing` Run 保存 MA/DB/RI 到 real/imaginary 的标准化结果及指标。Metric 的追溯链会到达父 Run、原始 `.s2p`、标准化 HDF5、转换脚本和两级 Provenance。

Touchstone 只包含网络结果及有限头信息，不能单独证明完整几何、材料、边界、网格收敛或实际求解时长。导入器会把这些边界明确记为缺失，不会从器件外观或文件名补猜。当前物理检查只是复数 S 矩阵的无源性与互易性数值筛查，不等同于网格独立性、跨求解器或实验验证。

## 统一可追加仓库（1.2.x 主入口）

旧的 `ingest-touchstone` 保留用于单案例兼容测试；持续累计数据应使用一个共享 warehouse。以下 PowerShell 命令会明确创建 `F:\DADC_DATA\inbox`、`staging`、`quarantine`，第一条成功数据到来时才创建符合 Schema 的 `warehouse`：

```powershell
& ".\.venv\Scripts\python.exe" -m dadc init-warehouse "F:\DADC_DATA"

& ".\.venv\Scripts\python.exe" -m dadc ingest `
  "F:\00_temp\bandpass_filter_run_001_HFSSDesign1.s2p" `
  --warehouse "F:\DADC_DATA\warehouse" `
  --adapter "touchstone_rf_filter" `
  --case-id "hfss_bandpass_real_001" `
  --device-name "HFSS official interdigital bandpass filter" `
  --device-class "rf_filter" `
  --filter-order 8 `
  --activity-type "simulation_run" `
  --source-timezone "+08:00" `
  --operator-id "local_user" `
  --platform "Windows 10" `
  --solver-edition "Student"

& ".\.venv\Scripts\python.exe" -m dadc validate "F:\DADC_DATA\warehouse"
```

判重使用源文件内容的 SHA-256，不使用文件名。因此同一文件改名后再次导入会返回 `duplicate`，不会创建第二个 Case。无法识别、元数据不足、解析失败、Case ID 冲突或 Schema 冲突的输入会按原字节复制到 `F:\DADC_DATA\quarantine`，不会进入 warehouse。成功追加先在 `staging` 中构建并完整校验，然后提交 Case 并重建 Parquet 索引；冻结的核心 Schema 不会被导入器改写。

文件夹批量导入使用一个或多个 `*.dadc.json`。每份清单的 `source` 相对该清单定位，还可以通过 `companion_artifacts` 一起保存 `.aedt`、日志、网格和脚本等复现证据：

```powershell
& ".\.venv\Scripts\python.exe" -m dadc ingest `
  "examples\intake" `
  --warehouse "F:\DADC_DATA\warehouse"
```

## PyAEDT + AEDT Student 自动生成天线数据

PyAEDT 是可选依赖。当前 Python 3.13 环境可以使用正式 PyAEDT；先安装并只做连接探测：

```powershell
& ".\.venv\Scripts\python.exe" -m pip install "pyaedt>=1,<2"

& ".\.venv\Scripts\python.exe" "scripts\pyaedt_smoke_test.py" `
  --output-dir "F:\DADC_DATA\inbox\pyaedt_smoke" `
  --aedt-version "2025.2"
```

探测报告固定写到 `F:\DADC_DATA\inbox\pyaedt_smoke\pyaedt_smoke_report.json`。通过后生成、求解并导出官方 Stackup3D 方法的 10 GHz 探针馈电贴片天线：

```powershell
& ".\.venv\Scripts\python.exe" "scripts\generate_patch_antenna.py" `
  --output-dir "F:\DADC_DATA\inbox\pyaedt_patch_001" `
  --aedt-version "2025.2" `
  --cores 2 `
  --source-timezone "+08:00" `
  --operator-id "local_user"
```

该目录会保留 `.aedt`、`.s1p`、生成清单、生成脚本以及可获得的收敛/网格/求解记录，并产生 `dadc_patch_antenna.dadc.json`。成功后整目录入库：

```powershell
& ".\.venv\Scripts\python.exe" -m dadc ingest `
  "F:\DADC_DATA\inbox\pyaedt_patch_001" `
  --warehouse "F:\DADC_DATA\warehouse"

& ".\.venv\Scripts\python.exe" -m dadc validate "F:\DADC_DATA\warehouse"
```

脚本默认打开 AEDT 图形界面，便于首次观察；确认稳定后才建议加 `--non-graphical`。每次求解使用新的英文输出目录，脚本拒绝覆盖已有 `.aedt` 或 `.s1p`。

## 第三个真实最小集：厂商射频电感

这一组不调用 AEDT。下载器从 Würth Elektronik 官方页面取得 744765056A 的厂商 S2P 与数据手册，核对固定 SHA-256 后生成相对路径 intake 清单。PowerShell 中从仓库根目录运行：

```powershell
python "scripts\download_we_inductor_sample.py" `
  --output-dir "..\DADC_DATA\inbox\we_inductor_001" `
  --operator-id "local_user"

python -m dadc ingest `
  "..\DADC_DATA\inbox\we_inductor_001" `
  --warehouse "..\DADC_DATA\warehouse"

python -m dadc validate "..\DADC_DATA\warehouse"

python -m dadc trace-metric `
  "..\DADC_DATA\warehouse" `
  "metric_vendor_inductor_we_744765056a_real_001_effective_inductance_at_reference_frequency"
```

该 Case 明确分成三个 Run：厂商 VNA 数据为 `experiment_run`，数据手册为 `literature_record`，S 参数标准化和阻抗/L/Q 换算为 `data_processing`。原始 S 参数标记为 `raw_experiment_output`；手册规格为 `literature_extracted`；阻抗、电感和 Q 曲线及指标为 `calculated`。S2P 中的 Windows-1252 注释不会再导致 UTF-8 解码失败。

派生阻抗固定使用 `Z=Z0(I+S)(I-S)^-1` 和 `Zdiff=Z11+Z22-Z12-Z21`，电感使用 `Im(Zdiff)/(2πf)`。这些是透明记录的计算定义，不等同于厂商数据手册的夹具和测试方法，禁止直接把两组数值当作一致性验证。官方样本在统一的严格无源性/互易性数值筛查下会留下 failed Validation；这不会使仓库格式无效，也不会被适配器通过放宽阈值掩盖。下载内容若被厂商更新，SHA-256 钉住检查会停止脚本，要求先人工复核新版本。

## 第四个最小集：焦耳热—温度场耦合

第 4 组由小型 DADC 有限差分参考求解器计算，不依赖 AEDT，也不冒充第三方求解器。它求解电势方程，计算电场与焦耳损耗，再把损耗场通过显式 one-way coupling edge 传给稳态热方程。方程与一向耦合方式参考 OpenFOAM Joule heating 文档和 MFEM Joule miniapp 描述；实际数值由仓库内脚本生成。

在 Windows PowerShell 中先生成，再把整个相对路径目录入库：

```powershell
python "scripts\generate_joule_thermal_resistor.py" `
  --output-dir "..\DADC_DATA\inbox\power_resistor_001" `
  --operator-id "local_user"

python -m dadc ingest `
  "..\DADC_DATA\inbox\power_resistor_001" `
  --warehouse "..\DADC_DATA\warehouse"

python -m dadc validate "..\DADC_DATA\warehouse"

python -m dadc trace-metric `
  "..\DADC_DATA\warehouse" `
  "metric_power_resistor_multiphysics_001_maximum_temperature"
```

源目录包含网格节点/单元 CSV、电场 CSV、热场 CSV、两个求解日志、耦合映射、网格对比证据、生成脚本和一个主 bundle JSON。主 bundle 内嵌所有伴随文件的相对路径、字节数和 SHA-256，因此任一伴随文件被改动会在入库前进入 quarantine。标准化后形成两个 HDF5：电势、电场、焦耳损耗；温度、热流。每个场都记录坐标系、米制坐标、结构网格、分量、稳态条件、数据位置和归一化。

该 Case 有 4 个 Run：电求解、电场归一化、热求解、热场归一化。温度指标的 `trace-metric` 会沿 Study 的 coupling edge 回到焦耳损耗 Observable、电求解 Run、耦合映射和两侧原始场文件。内置验证包括两项求解收敛、一项耦合功率守恒和一项粗细网格温升差异；阈值与实际结果均写入 `validation.json`。

## 目录布局

```text
repository.json
schemas/v1.0/*.schema.json
schemas/v1.0/device_profiles/*.schema.json
cases/<case_id>/metadata/<entity_type>/*.json
cases/<case_id>/validation.json
cases/<case_id>/data/*.h5
cases/<case_id>/raw/*
cases/<case_id>/logs/*
cases/<case_id>/evidence/*
index/catalog.parquet
index/metrics.parquet
```

字段不适用时直接省略，而不是批量写 `null`。唯一保留的空值语义是非空间 Observable 的 `coordinate_system_ref: null`，这是冻结结构中的显式语义。新增器件只需新增 profile Schema 和 `Device.extensions` 对应命名空间，不修改核心 Schema，也不迁移已有数据。

## 示例性质

`examples/generated` 是确定性合成验收夹具；`tests/fixtures/bandpass_filter_run_001_HFSSDesign1.s2p` 是 AEDT Student 2025 R2.4 导出的真实求解结果；厂商电感样本由下载器从官方来源按哈希取得；第 4 组是仓库自带参考求解器计算出的真实数值解。真实文件用于验证导入、复数/场标准化、活动语义、追溯和防篡改。第 4 组完成自身收敛、耦合守恒和网格差异检查，但不表示已经完成跨求解器或实验符合性证明。
