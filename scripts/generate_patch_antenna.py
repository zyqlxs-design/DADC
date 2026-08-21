"""Create, solve, and export a probe-fed patch antenna with AEDT Student."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _export_optional(callable_object, *args, **kwargs) -> dict[str, object]:
    try:
        value = callable_object(*args, **kwargs)
        return {"status": "exported", "result": str(value)}
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _source_timezone_offset(value: str) -> str:
    match = re.fullmatch(r"[+-](\d{2}):(\d{2})", value)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise argparse.ArgumentTypeError("source timezone must use +HH:MM or -HH:MM")
    return value


def _positive_mm(value: str) -> float:
    parsed = float(value)
    if parsed <= 0.0:
        raise argparse.ArgumentTypeError("millimetre dimensions must be positive")
    return parsed


def _unit_interval(value: str) -> float:
    parsed = float(value)
    if not 0.0 < parsed < 1.0:
        raise argparse.ArgumentTypeError("relative probe offset must be between 0 and 1")
    return parsed


def _case_id(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", value):
        raise argparse.ArgumentTypeError("case id must match [a-z][a-z0-9_]{2,63}")
    return value


def _companion_candidates(
    output_dir: Path,
    project_path: Path,
    manifest_path: Path,
    recipe_copy: Path,
) -> list[tuple[Path, str, str, str]]:
    """Map generator-specific files to the frozen DADC Artifact roles."""
    return [
        (project_path, "native_project", "application/octet-stream", "raw_solver_output"),
        (manifest_path, "report", "application/json", "calculated"),
        (recipe_copy, "script", "text/x-python", "manual_entry"),
        (output_dir / "convergence.conv", "validation_evidence", "text/plain", "raw_solver_output"),
        (output_dir / "mesh_stats.mstat", "validation_evidence", "text/plain", "raw_solver_output"),
        (output_dir / "solve_profile.prof", "solver_log", "text/plain", "raw_solver_output"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--aedt-version", default="2025.2")
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--non-graphical", action="store_true")
    parser.add_argument("--source-timezone", required=True, type=_source_timezone_offset)
    parser.add_argument("--operator-id", default="local_user")
    parser.add_argument("--case-id", default="pyaedt_patch_antenna_real_001", type=_case_id)
    parser.add_argument("--device-name", default="PyAEDT probe-fed 10 GHz patch antenna")
    parser.add_argument("--patch-length-mm", type=_positive_mm, default=9.57)
    parser.add_argument("--patch-width-mm", type=_positive_mm, default=9.25)
    parser.add_argument("--probe-relative-x-offset", type=_unit_interval, default=0.485)
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    project_path = output_dir / "dadc_patch_antenna_10ghz.aedt"
    touchstone_path = output_dir / "dadc_patch_antenna_10ghz.s1p"
    manifest_path = output_dir / "generation_manifest.json"
    intake_path = output_dir / "dadc_patch_antenna.dadc.json"
    if project_path.exists() or touchstone_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite prior solver output in {output_dir}; use a new output directory"
        )

    manifest: dict[str, object] = {
        "generation_manifest_version": "1.0",
        "recipe": "official_pyaedt_stackup3d_probe_fed_patch_adapted_for_dadc",
        "recipe_source": "https://examples.aedt.docs.pyansys.com/version/dev/examples/high_frequency/antenna/patch.html",
        "started_at": _now(),
        "requested_aedt_version": args.aedt_version,
        "student_version": True,
        "python": sys.version,
        "platform": platform.platform(),
        "project_path": str(project_path),
        "touchstone_path": str(touchstone_path),
        "parameters": {
            "length_unit": "mm",
            "solution_frequency": "10GHz",
            "sweep_start": "8GHz",
            "sweep_stop": "12GHz",
            "patch_length": f"{args.patch_length_mm}mm",
            "patch_width": f"{args.patch_width_mm}mm",
            "substrate_thickness": "0.5mm",
            "substrate_material": "Duroid (tm)",
            "conductor_material": "copper",
            "probe_relative_x_offset": args.probe_relative_x_offset,
        },
    }
    hfss = None
    try:
        os.environ["PYAEDT_USE_PRE_GRPC_ARGS"] = "True"
        import ansys.aedt.core
        from ansys.aedt.core import Hfss, settings
        from ansys.aedt.core.modeler.advanced_cad.stackup_3d import Stackup3D

        from _pyaedt_student_compat import enable_student_session_discovery

        settings.grpc_secure_mode = False
        manifest["student_session_discovery_workaround"] = enable_student_session_discovery()
        manifest["pyaedt_version"] = getattr(ansys.aedt.core, "__version__", "not_recorded")
        hfss = Hfss(
            project=str(project_path),
            design="DADC_Patch_Antenna",
            solution_type="Terminal",
            version=args.aedt_version,
            non_graphical=args.non_graphical,
            new_desktop=True,
            close_on_exit=False,
            student_version=True,
        )
        hfss.modeler.model_units = "mm"
        stackup = Stackup3D(hfss)
        ground = stackup.add_ground_layer(
            "ground", material="copper", thickness=0.035, fill_material="air"
        )
        stackup.add_dielectric_layer(
            "dielectric", thickness="0.5mm", material="Duroid (tm)"
        )
        signal = stackup.add_signal_layer(
            "signal", material="copper", thickness=0.035, fill_material="air"
        )
        patch = signal.add_patch(
            patch_length=args.patch_length_mm,
            patch_width=args.patch_width_mm,
            patch_name="Patch",
            frequency=1.0e10,
        )
        stackup.resize_around_element(patch)
        region = hfss.modeler.create_region([3, 3, 3, 3, 3, 3], is_percentage=False)
        if not hfss.assign_radiation_boundary_to_objects(region):
            raise RuntimeError("AEDT did not create the radiation boundary")
        # PyAEDT 1.4.0 annotates ``create_probe_port`` as returning None and
        # actually returns None even when AEDT creates the excitation. Verify
        # the resulting AEDT state instead of treating the return value as a
        # success flag.
        probe_result = patch.create_probe_port(
            ground, rel_x_offset=args.probe_relative_x_offset
        )
        probe_excitations = list(hfss.excitation_names)
        manifest["probe_port_creation"] = {
            "api_return": repr(probe_result),
            "excitations_after_call": probe_excitations,
        }
        if not any(name.startswith("Probe_Port") for name in probe_excitations):
            raise RuntimeError(
                "AEDT did not expose Probe_Port after create_probe_port(); "
                f"excitations={probe_excitations!r}"
            )

        setup = hfss.create_setup(name="Setup1", setup_type="HFSSDriven", Frequency="10GHz")
        sweep = setup.create_frequency_sweep(
            unit="GHz",
            name="Sweep1",
            start_frequency=8,
            stop_frequency=12,
            sweep_type="Interpolating",
        )
        if not sweep:
            raise RuntimeError("AEDT did not create Sweep1")
        hfss.save_project()
        validation = hfss.validate_full_design()
        manifest["design_validation"] = str(validation)
        solved = hfss.analyze(cores=args.cores)
        if not solved:
            raise RuntimeError("HFSS analyze() returned False")
        hfss.save_project()

        exported = hfss.export_touchstone(
            setup="Setup1",
            sweep="Sweep1",
            output_file=str(touchstone_path),
            gamma_impedance_comments=True,
        )
        if not exported or not touchstone_path.is_file():
            raise RuntimeError(f"Touchstone export failed: {exported!r}")

        manifest["optional_exports"] = {
            "convergence": _export_optional(
                hfss.export_convergence,
                "Setup1",
                output_file=str(output_dir / "convergence.conv"),
            ),
            "mesh_stats": _export_optional(
                hfss.export_mesh_stats,
                "Setup1",
                output_file=str(output_dir / "mesh_stats.mstat"),
            ),
            "profile": _export_optional(
                hfss.export_profile,
                "Setup1",
                output_file=str(output_dir / "solve_profile.prof"),
            ),
        }
        manifest.update(
            {
                "status": "succeeded",
                "aedt_version": hfss.aedt_version_id,
                "design_name": hfss.design_name,
                "project_file": str(hfss.project_file),
                "touchstone_file": str(touchstone_path),
                "object_names": sorted(obj.name for obj in hfss.modeler.objects.values()),
                "excitations": list(hfss.excitation_names),
            }
        )
        return_code = 0
    except Exception as exc:
        manifest.update(
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
                hfss.save_project()
                hfss.release_desktop(close_projects=True, close_desktop=True)
            except Exception as exc:
                manifest["release_warning"] = f"{type(exc).__name__}: {exc}"
        manifest["finished_at"] = _now()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if manifest.get("status") == "succeeded":
            recipe_copy = output_dir / "generation_recipe.py"
            shutil.copy2(Path(__file__).resolve(), recipe_copy)
            companion_candidates = _companion_candidates(
                output_dir,
                project_path,
                manifest_path,
                recipe_copy,
            )
            intake = {
                "intake_schema_version": "1.0",
                "source": touchstone_path.name,
                "adapter": "touchstone_antenna",
                "case_id": args.case_id,
                "device_name": args.device_name,
                "device_class": "antenna",
                "device_subtype": "probe_fed_patch_antenna",
                "activity_type": "simulation_run",
                "feed_type": "coaxial_probe",
                "radiation_mode": "broadside",
                "source_timezone": args.source_timezone,
                "operator_id": args.operator_id,
                "platform": platform.platform(),
                "compute": f"local AEDT Student; requested cores={args.cores}",
                "solver_edition": "Student",
                "companion_artifacts": [
                    {
                        "path": path.name,
                        "role": role,
                        "media_type": media_type,
                        "value_origin": value_origin,
                    }
                    for path, role, media_type, value_origin in companion_candidates
                    if path.is_file()
                ],
            }
            intake_path.write_text(json.dumps(intake, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
