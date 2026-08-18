"""Verify that PyAEDT can launch the installed AEDT Student HFSS application."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--aedt-version", default="2025.2")
    parser.add_argument("--non-graphical", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "pyaedt_smoke_report.json"
    project_path = output_dir / "pyaedt_smoke.aedt"
    report: dict[str, object] = {
        "test": "pyaedt_aedt_student_hfss_launch",
        "started_at": _now(),
        "requested_aedt_version": args.aedt_version,
        "student_version": True,
        "non_graphical": args.non_graphical,
        "python": sys.version,
        "platform": platform.platform(),
        "project_path": str(project_path),
    }
    hfss = None
    try:
        # Official workaround for AEDT Student 2025 R2 and earlier: use the
        # insecure local TCP gRPC transport before creating the Desktop.
        os.environ["PYAEDT_USE_PRE_GRPC_ARGS"] = "True"
        import ansys.aedt.core
        from ansys.aedt.core import Hfss, settings

        from _pyaedt_student_compat import enable_student_session_discovery

        settings.grpc_secure_mode = False
        report["student_session_discovery_workaround"] = enable_student_session_discovery()
        report["pyaedt_version"] = getattr(ansys.aedt.core, "__version__", "not_recorded")
        hfss = Hfss(
            project=str(project_path),
            design="DADC_PyAEDT_Smoke",
            solution_type="Terminal",
            version=args.aedt_version,
            non_graphical=args.non_graphical,
            new_desktop=True,
            close_on_exit=False,
            student_version=True,
        )
        hfss.save_project()
        report.update(
            {
                "status": "passed",
                "aedt_version": hfss.aedt_version_id,
                "design_name": hfss.design_name,
                "project_file": str(hfss.project_file),
                "desktop_install_dir": str(hfss.desktop_install_dir),
            }
        )
        return_code = 0
    except Exception as exc:
        report.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return_code = 1
    finally:
        if hfss is not None:
            try:
                hfss.release_desktop(close_projects=True, close_desktop=True)
            except Exception as exc:
                report["release_warning"] = f"{type(exc).__name__}: {exc}"
        report["finished_at"] = _now()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
