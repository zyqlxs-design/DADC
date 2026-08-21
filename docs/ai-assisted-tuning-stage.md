# DADC 受约束AI辅助调优阶段

## 当前已经具备什么

DADC 1.8.0.dev0 在原有确定性网格调优基础上增加了数据沟通智能体的最小闭环：

```text
用户目标
  -> 按器件检索知识库
  -> 读取已验证DADC仓库中的历史优化试算
  -> LLM只选择白名单参数值
  -> 程序校验参数、单位、预算和知识引用
  -> 固定PyAEDT后端执行全求解
  -> 最优点独立复算
  -> 程序判断是否达到明确阈值
  -> 优化证据包写回DADC
```

LLM不能修改求解器、目标函数、单位、预算、文件路径或代码，也不能自行宣布目标已经达到。任何越界参数、未知参数、伪造知识引用或超预算网格都会在调用求解器之前被拒绝。

## 查看已经完成的真实自动调优

用户此前的真实PyAEDT结果可以直接生成表格报告：

```powershell
$Python = "D:\DADC\.venv\Scripts\python.exe"
$Bundle = "D:\DADC_TEST\pyaedt_patch_smoke_20260821_113206\optimization_bundle.json"
$Report = "D:\DADC_TEST\pyaedt_patch_smoke_20260821_113206\automatic_tuning_report.md"

& $Python -m dadc optimization-report $Bundle $Report
Get-Content $Report
```

报告会逐行列出 `search_001`、`search_002`、`verify_001` 的参数、状态、目标指标和证据文件数量。

## 不使用API的智能体闭环验收

先运行离线夹具，确认知识检索、参数边界、执行、独立复核和阈值判断能够连接：

```powershell
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
& "D:\DADC\.venv\Scripts\python.exe" ".\scripts\run_agent_tuning_acceptance.py" `
  --output-dir "D:\DADC_TEST\agent_tuning_acceptance_$Stamp"
```

这个夹具不是LLM，也不是物理求解器，只用于证明智能体编排契约。

## 使用DeepSeek只生成参数计划

正式运行前建议先只生成计划，不调用AEDT：

```powershell
$Python = "D:\DADC\.venv\Scripts\python.exe"
$Corpus = "D:\DADC_TEST\knowledge_official_v11_你的时间戳"
$Warehouse = "D:\DADC_TEST\real_data_pyaedt_patch_smoke_20260821_113206\warehouse"
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Planning = "D:\DADC_TEST\deepseek_plan_$Stamp"

& $Python -m dadc agent-plan `
  ".\examples\agent\pyaedt_patch_deepseek_request.json" `
  $Corpus `
  $Planning `
  --provider deepseek `
  --model deepseek-v4-flash `
  --warehouse $Warehouse

Get-Content "$Planning\parameter_proposal.json"
Get-Content "$Planning\optimization_plan.json"
```

密钥只从当前进程的 `DEEPSEEK_API_KEY` 环境变量读取，不会写入智能体输出、优化计划或DADC仓库。连接方式采用DeepSeek官方OpenAI兼容接口与[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)；模型名仍可通过 `--model` 显式指定，以适应供应商更新。

## DeepSeek参与参数选择并自动调用PyAEDT

确认请求文件中的参数范围、50 MHz验收阈值、AEDT版本、输出目录和预算后，使用新的空目录运行：

```powershell
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Run = "D:\DADC_TEST\deepseek_pyaedt_tuning_$Stamp"

& $Python -m dadc agent-tune `
  ".\examples\agent\pyaedt_patch_deepseek_request.json" `
  $Corpus `
  $Run `
  --provider deepseek `
  --model deepseek-v4-flash `
  --warehouse $Warehouse `
  --approve-execution
```

结果中的状态只有两种主要含义：

- `accepted`：独立复算值满足请求文件中声明的客观阈值，并满足物理后端要求；
- `target_not_met`：本轮没有达到阈值，证据仍会保留，不能把它描述为成功结果。

LLM辅助选择可以缩小或重排候选空间，但不能保证有限预算内一定找到满足条件的设计。当前版本尚未实现多轮主动学习、代理模型或贝叶斯优化；这些能力应继续使用同一参数白名单、证据包和独立全求解复核边界。
