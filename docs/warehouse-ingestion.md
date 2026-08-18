# DADC 1.4 统一入库契约

## 不变边界

九个一级实体和 `schemas/v1.0` 是数据契约，不由适配器修改。适配器只负责识别外部格式、保留原件、把数值标准化为现有实体，并提供证据。器件差异进入独立 profile；入库登记、锁和隔离信息位于 `system` 或数据根目录，不伪装成第十个数据实体。

## 数据根目录

```text
DADC_DATA/
  inbox/                 人工或自动采集的待处理输入
  staging/               尚未提交的事务工作区
  quarantine/            未识别、失败或冲突输入及原因
  warehouse/             唯一共享 DADC Repository
    repository.json
    schemas/v1.0/
    cases/<case_id>/
    index/*.parquet
    system/ingestion_registry.json
```

空仓库不预先生成 `repository.json`，因为冻结的 V1.0 仓库 Schema 要求至少一个 Case。首个成功事务负责创建 warehouse。

## 适配器判定

每个适配器实现两步：`probe` 只判断是否有资格读取，不生成业务事实；`build_case_repository` 在 staging 内生成一个完整、可单独校验的 Case。最高置信度低于 0.80、同分歧义或人工元数据不足时禁止猜测，输入进入 quarantine。

当前适配器：

- `touchstone_rf_filter`：至少两端口的 `.sNp`，产生滤波器 S 参数和采样式 -3 dB 指标；
- `touchstone_antenna`：单端口 `.s1p`，产生天线 S11、采样谐振频率和最小回波损耗。
- `touchstone_inductor`：显式标记为电感的两端口厂商 `.s2p`，要求同时提供数据手册；分别生成 experiment、literature 和 processing Run，并产生阻抗/L/Q 派生曲线。
- `joule_thermal_field_bundle`：自校验的电—热场数据包，要求主 JSON 为所有相对路径伴随文件钉住 SHA-256；产生两个 simulation Run、两个 processing Run、五个场 Observable 和一条 one-way coupling edge。

Touchstone 适配器保留源复数格式说明，但 HDF5 统一保存 real/imaginary。曲线的 dB 指标是计算值，不替代原复数数组。场 bundle 的原始 CSV 不覆盖，HDF5 是独立标准化 Artifact。

## 提交事务和失败判据

一次成功追加必须同时满足：

1. 源文件复制后的 SHA-256 与输入一致；
2. staging 单案例仓库通过 Schema、引用、HDF5、Parquet 和 Artifact 完整性校验；
3. Case ID 和全部实体 ID 与 warehouse 无冲突；
4. 同名既有 Schema 的 SHA-256 完全一致；
5. Case 提交后全局 Parquet 索引重建成功；
6. 整个 warehouse 再次校验通过。

失败时恢复提交前的 manifest 和索引、移出新增 Case，并保留隔离副本。SHA-256 相同的源文件即使改名也只登记为 duplicate。文件名相同但内容不同不视为重复。

## Intake 清单

`*.dadc.json` 是入库指令，不是冻结九实体之一。最小模拟滤波器清单：

```json
{
  "intake_schema_version": "1.0",
  "source": "result.s2p",
  "adapter": "touchstone_rf_filter",
  "case_id": "filter_case_001",
  "device_name": "confirmed device name",
  "device_class": "rf_filter",
  "activity_type": "simulation_run",
  "filter_order": 8,
  "source_timezone": "+08:00"
}
```

`companion_artifacts` 可列出原生工程和证据；每项必须给出 `path`，并可给出 `role`、`media_type` 和 `value_origin`。相对路径以清单所在目录为基准。

## 当前四案例验收状态

1. 真实 HFSS 滤波器：已完成并进入共享 warehouse；
2. PyAEDT/HFSS 贴片天线：已完成真实求解和共享 warehouse 入库；
3. 厂商射频电感 PDF + Touchstone：适配器、官方下载器和真实文件闭环已完成；用户 warehouse 执行一次下载与入库命令后即变为 3/4；
4. DADC 薄膜功率电阻参考求解：生成器、场 bundle 适配器、收敛/网格/耦合验证与跨物理场追溯已完成；待与第 3 组一起进入用户 warehouse。

四例的工程实现已经证明同一核心结构能同时容纳本地仿真、自动仿真、厂商实验数据、文献规格和多物理场网格数据，而且非求解器输入不会被错误冒充成 simulation Run。用户 warehouse 是否完成 4/4 仍以实际入库及总校验结果为准。
