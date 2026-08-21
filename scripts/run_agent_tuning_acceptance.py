#!/usr/bin/env python3
"""Run the portable agent-to-tuning loop and emit objective acceptance evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from dadc.agent import run_agent_tuning  # noqa: E402
from dadc.automation import write_optimization_report  # noqa: E402
from dadc.knowledge import build_index, collect_corpus  # noqa: E402


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    root = Path(args.output_dir).resolve()
    if root.exists() and (root.is_file() or any(root.iterdir())):
        raise FileExistsError(f"Refusing to overwrite non-empty output directory: {root}")
    root.mkdir(parents=True, exist_ok=True)

    corpus = root / "knowledge"
    collect_corpus(
        REPOSITORY_ROOT / "examples" / "knowledge" / "device_partition_fixture_sources.json",
        corpus,
    )
    build_index(corpus, dimensions=128)
    result = run_agent_tuning(
        REPOSITORY_ROOT / "examples" / "agent" / "analytic_fixture_request.json",
        corpus,
        root / "agent_run",
        provider="deterministic_fixture",
        approve_execution=True,
    )
    optimization_report = root / "automatic_tuning_report.md"
    report = write_optimization_report(result["optimization"]["bundle"], optimization_report)
    proposal = json.loads(
        (root / "agent_run" / "planning" / "parameter_proposal.json").read_text(
            encoding="utf-8"
        )
    )
    context = json.loads(
        (root / "agent_run" / "planning" / "agent_context.json").read_text(encoding="utf-8")
    )
    checks = [
        {
            "check": "knowledge_evidence_retrieved",
            "status": "passed" if context["knowledge_evidence"] else "failed",
            "evidence": {"chunk_count": len(context["knowledge_evidence"])},
        },
        {
            "check": "proposal_constraints_checked",
            "status": "passed" if proposal["constraints_checked"] else "failed",
            "evidence": {
                "provider_id": proposal["provider_id"],
                "selected_values": proposal["selection"]["selected_values"],
            },
        },
        {
            "check": "independent_verification_completed",
            "status": (
                "passed" if result["acceptance"]["checks"]["independent_verification"] else "failed"
            ),
            "evidence": {
                "verification_trials": report["trial_counts"]["verification"],
                "verified_value": result["acceptance"]["verified_value"],
            },
        },
        {
            "check": "declared_acceptance_rule_evaluated",
            "status": result["acceptance"]["status"],
            "evidence": result["acceptance"],
        },
    ]
    failed = [item for item in checks if item["status"] != "passed"]
    acceptance = {
        "agent_tuning_acceptance_version": "1.0",
        "overall_status": "passed" if not failed else "failed",
        "evaluation_policy": "objective checks only; no subjective score",
        "scope": (
            "Portable contract fixture only. This validates orchestration and does not constitute "
            "LLM quality or physical HFSS evidence."
        ),
        "checks": checks,
        "artifacts": {
            "agent_result": result["result"],
            "optimization_bundle": result["optimization"]["bundle"],
            "automatic_tuning_report": str(optimization_report),
        },
    }
    json_path = root / "agent_tuning_acceptance.json"
    markdown_path = root / "agent_tuning_acceptance.md"
    _write_json(json_path, acceptance)
    lines = [
        "# DADC 数据沟通智能体—调优闭环验收",
        "",
        f"- 总体状态：`{acceptance['overall_status']}`",
        "- 评价方式：只记录客观通过/失败，不使用主观评分。",
        "- 范围：离线契约夹具，只证明流程，不代表LLM质量或HFSS物理结论。",
        "",
        "| 检查 | 状态 | 证据 |",
        "|---|---|---|",
    ]
    for item in checks:
        evidence = json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True)
        lines.append(f"| `{item['check']}` | `{item['status']}` | `{evidence}` |")
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- 自动调优报告：`{optimization_report}`",
            f"- 智能体结果：`{result['result']}`",
            f"- 优化证据包：`{result['optimization']['bundle']}`",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
