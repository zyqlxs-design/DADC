"""Human-readable reports for optimization evidence bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts import validate_contract


def _parameter_text(trial: dict[str, Any]) -> str:
    return ", ".join(
        f"{item['name']}={item['value']} {item['unit']}" for item in trial["job"]["parameters"]
    )


def _metric_text(trial: dict[str, Any]) -> str:
    metric = trial.get("metric")
    if not metric:
        return "—"
    return f"{metric['value']} {metric['unit']}"


def optimization_summary(bundle_path: str | Path) -> dict[str, Any]:
    path = Path(bundle_path).resolve()
    bundle = json.loads(path.read_text(encoding="utf-8"))
    validate_contract(bundle, "optimization_bundle")
    trials = [*bundle["search_trials"], *bundle["verification_trials"]]
    best = next(
        item for item in bundle["search_trials"] if item["trial_id"] == bundle["best_search_trial_id"]
    )
    return {
        "optimization_report_version": "1.0",
        "bundle": str(path),
        "optimization_id": bundle["plan"]["optimization_id"],
        "device": bundle["plan"]["device"],
        "objective": bundle["plan"]["objective"],
        "backend": bundle["backend"],
        "best_search_trial_id": bundle["best_search_trial_id"],
        "best_parameters": best["job"]["parameters"],
        "best_metric": best["metric"],
        "trial_counts": {
            "search": len(bundle["search_trials"]),
            "verification": len(bundle["verification_trials"]),
            "succeeded": sum(item["status"] == "succeeded" for item in trials),
            "failed": sum(item["status"] == "failed" for item in trials),
        },
        "trials": [
            {
                "trial_id": item["trial_id"],
                "trial_kind": item["trial_kind"],
                "status": item["status"],
                "parameters": item["job"]["parameters"],
                "metric": item.get("metric"),
                "error": item.get("error"),
                "artifact_count": len(item["artifact_paths"]),
            }
            for item in trials
        ],
    }


def write_optimization_report(bundle_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Write a Markdown table showing every attempted and verified parameter point."""

    source = Path(bundle_path).resolve()
    bundle = json.loads(source.read_text(encoding="utf-8"))
    validate_contract(bundle, "optimization_bundle")
    summary = optimization_summary(source)
    output = Path(output_path).resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite optimization report: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    objective = summary["objective"]
    backend = summary["backend"]
    lines = [
        "# DADC 自动调优结果报告",
        "",
        f"- 优化任务：`{summary['optimization_id']}`",
        f"- 器件：`{summary['device']['device_class']}` / `{summary['device']['device_subtype']}`",
        f"- 后端：`{backend['backend_id']}`（物理求解器：`{str(backend['is_physical_solver']).lower()}`）",
        f"- 目标：`{objective['quantity']}`，策略：`{objective['goal']}`",
        f"- 最优搜索点：`{summary['best_search_trial_id']}`",
        f"- 最优搜索指标：`{summary['best_metric']['value']} {summary['best_metric']['unit']}`",
        "",
        "## 参数点与结果",
        "",
        "| 试算 | 类型 | 状态 | 参数 | 指标 | 证据文件数 |",
        "|---|---|---|---|---:|---:|",
    ]
    for item in [*bundle["search_trials"], *bundle["verification_trials"]]:
        lines.append(
            "| `{trial}` | `{kind}` | `{status}` | {parameters} | {metric} | {artifacts} |".format(
                trial=item["trial_id"],
                kind=item["trial_kind"],
                status=item["status"],
                parameters=_parameter_text(item).replace("|", "\\|"),
                metric=_metric_text(item),
                artifacts=len(item["artifact_paths"]),
            )
        )
    lines.extend(
        [
            "",
            "## 结果边界",
            "",
            "最优点由搜索试算确定，并通过独立复算记录。报告展示的是优化证据包中的客观结果；"
            "是否满足工程要求还必须依据明确阈值、网格独立性以及必要的实验或跨求解器验证。",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    return {**summary, "report": str(output)}
