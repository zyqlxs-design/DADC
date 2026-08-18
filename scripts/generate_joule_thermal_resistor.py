"""Generate a deterministic electro-thermal power-resistor source bundle.

The generator solves two actual boundary-value problems on a rectangular film:

* div(sigma grad(V)) = 0
* -div(k grad(T)) = sigma |grad(V)|^2

It is deliberately a small DADC reference finite-difference solver, not an
Ansys/OpenFOAM/MFEM result.  The immutable source bundle records that distinction
and cites the public equations used by the recipe.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _solve_potential(nx: int, ny: int, voltage_v: float) -> tuple[np.ndarray, int, float]:
    """Jacobi solve with left/right Dirichlet and insulated top/bottom."""

    potential = np.zeros((ny, nx), dtype=np.float64)
    potential[:, -1] = voltage_v
    tolerance = 1.0e-11
    max_iterations = 100_000
    residual = float("inf")
    for iteration in range(1, max_iterations + 1):
        updated = potential.copy()
        updated[1:-1, 1:-1] = 0.25 * (
            potential[1:-1, :-2]
            + potential[1:-1, 2:]
            + potential[:-2, 1:-1]
            + potential[2:, 1:-1]
        )
        updated[0, 1:-1] = updated[1, 1:-1]
        updated[-1, 1:-1] = updated[-2, 1:-1]
        updated[:, 0] = 0.0
        updated[:, -1] = voltage_v
        residual = float(np.max(np.abs(updated - potential)))
        potential = updated
        if residual <= tolerance:
            return potential, iteration, residual
    raise RuntimeError(f"Electrical Jacobi solver did not converge; residual={residual:.17g}")


def _solve_temperature(
    heat_source_w_m3: np.ndarray,
    dx_m: float,
    dy_m: float,
    thermal_conductivity_w_mk: float,
    ambient_k: float,
) -> tuple[np.ndarray, int, float]:
    """Jacobi solve with a fixed ambient temperature on the outer boundary."""

    temperature = np.full(heat_source_w_m3.shape, ambient_k, dtype=np.float64)
    tolerance = 1.0e-9
    max_iterations = 200_000
    denominator = 2.0 / dx_m**2 + 2.0 / dy_m**2
    residual = float("inf")
    for iteration in range(1, max_iterations + 1):
        updated = temperature.copy()
        updated[1:-1, 1:-1] = (
            (temperature[1:-1, :-2] + temperature[1:-1, 2:]) / dx_m**2
            + (temperature[:-2, 1:-1] + temperature[2:, 1:-1]) / dy_m**2
            + heat_source_w_m3[1:-1, 1:-1] / thermal_conductivity_w_mk
        ) / denominator
        residual = float(np.max(np.abs(updated - temperature)))
        temperature = updated
        if residual <= tolerance:
            return temperature, iteration, residual
    raise RuntimeError(f"Thermal Jacobi solver did not converge; residual={residual:.17g}")


def _solve_case(nx: int, ny: int, parameters: dict[str, float]) -> dict[str, Any]:
    length_m = parameters["length_m"]
    width_m = parameters["width_m"]
    thickness_m = parameters["thickness_m"]
    conductivity = parameters["electrical_conductivity_s_m"]
    thermal_conductivity = parameters["thermal_conductivity_w_mk"]
    voltage_v = parameters["applied_voltage_v"]
    ambient_k = parameters["ambient_temperature_k"]
    x = np.linspace(0.0, length_m, nx)
    y = np.linspace(0.0, width_m, ny)
    dx_m = float(x[1] - x[0])
    dy_m = float(y[1] - y[0])

    potential, electrical_iterations, electrical_residual = _solve_potential(nx, ny, voltage_v)
    grad_y, grad_x = np.gradient(potential, dy_m, dx_m, edge_order=2)
    electric_x = -grad_x
    electric_y = -grad_y
    joule_loss = conductivity * (electric_x**2 + electric_y**2)
    # Explicit trapezoidal integration keeps the recipe compatible with the
    # declared NumPy >=1.24 range (np.trapezoid was only added in NumPy 2.0).
    integrated_x = np.sum(
        0.5 * (joule_loss[:, 1:] + joule_loss[:, :-1]) * np.diff(x)[None, :],
        axis=1,
    )
    total_power_w = float(
        np.sum(0.5 * (integrated_x[1:] + integrated_x[:-1]) * np.diff(y)) * thickness_m
    )

    temperature, thermal_iterations, thermal_residual = _solve_temperature(
        joule_loss,
        dx_m,
        dy_m,
        thermal_conductivity,
        ambient_k,
    )
    temp_grad_y, temp_grad_x = np.gradient(temperature, dy_m, dx_m, edge_order=2)
    heat_flux_x = -thermal_conductivity * temp_grad_x
    heat_flux_y = -thermal_conductivity * temp_grad_y
    maximum_temperature_k = float(np.max(temperature))
    return {
        "x": x,
        "y": y,
        "potential": potential,
        "electric_x": electric_x,
        "electric_y": electric_y,
        "joule_loss": joule_loss,
        "temperature": temperature,
        "heat_flux_x": heat_flux_x,
        "heat_flux_y": heat_flux_y,
        "electrical_iterations": electrical_iterations,
        "electrical_residual": electrical_residual,
        "thermal_iterations": thermal_iterations,
        "thermal_residual": thermal_residual,
        "total_power_w": total_power_w,
        "maximum_temperature_k": maximum_temperature_k,
        "thermal_resistance_k_w": (maximum_temperature_k - ambient_k) / total_power_w,
    }


def _write_mesh(nodes_path: Path, cells_path: Path, result: dict[str, Any]) -> None:
    x = result["x"]
    y = result["y"]
    nx = len(x)
    ny = len(y)
    with nodes_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["node_id", "i", "j", "x_m", "y_m", "z_m"])
        for j in range(ny):
            for i in range(nx):
                writer.writerow([j * nx + i, i, j, f"{x[i]:.17g}", f"{y[j]:.17g}", "0"])
    with cells_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["cell_id", "n0", "n1", "n2", "n3"])
        cell_id = 0
        for j in range(ny - 1):
            for i in range(nx - 1):
                n0 = j * nx + i
                writer.writerow([cell_id, n0, n0 + 1, n0 + 1 + nx, n0 + nx])
                cell_id += 1


def _write_fields(electrical_path: Path, thermal_path: Path, result: dict[str, Any]) -> None:
    ny, nx = result["potential"].shape
    with electrical_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(
            ["node_id", "potential_v", "electric_field_x_v_m", "electric_field_y_v_m", "joule_loss_w_m3"]
        )
        for j in range(ny):
            for i in range(nx):
                writer.writerow(
                    [
                        j * nx + i,
                        f"{result['potential'][j, i]:.17g}",
                        f"{result['electric_x'][j, i]:.17g}",
                        f"{result['electric_y'][j, i]:.17g}",
                        f"{result['joule_loss'][j, i]:.17g}",
                    ]
                )
    with thermal_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["node_id", "temperature_k", "heat_flux_x_w_m2", "heat_flux_y_w_m2"])
        for j in range(ny):
            for i in range(nx):
                writer.writerow(
                    [
                        j * nx + i,
                        f"{result['temperature'][j, i]:.17g}",
                        f"{result['heat_flux_x'][j, i]:.17g}",
                        f"{result['heat_flux_y'][j, i]:.17g}",
                    ]
                )


def _file_entry(path: Path, root: Path, *, file_id: str, stage: str, role: str, media_type: str, value_origin: str) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "stage": stage,
        "artifact_role": role,
        "media_type": media_type,
        "value_origin": value_origin,
    }


def generate(output_dir: str | Path, *, operator_id: str = "local_user") -> Path:
    root = Path(output_dir).resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Refusing to write into non-empty output directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    generated_at = _utc_now()
    parameters = {
        "length_m": 0.02,
        "width_m": 0.01,
        "thickness_m": 0.0001,
        "electrical_conductivity_s_m": 100_000.0,
        "thermal_conductivity_w_mk": 10.0,
        "applied_voltage_v": 1.0,
        "ambient_temperature_k": 293.15,
    }
    coarse = _solve_case(31, 17, parameters)
    fine = _solve_case(61, 33, parameters)
    coarse_temperature_rise = coarse["maximum_temperature_k"] - parameters["ambient_temperature_k"]
    fine_temperature_rise = fine["maximum_temperature_k"] - parameters["ambient_temperature_k"]
    mesh_relative_difference = abs(coarse_temperature_rise - fine_temperature_rise) / abs(
        fine_temperature_rise
    )

    nodes_path = root / "mesh_nodes.csv"
    cells_path = root / "mesh_cells.csv"
    electrical_path = root / "electrical_fields.csv"
    thermal_path = root / "thermal_fields.csv"
    mapping_path = root / "coupling_map.json"
    electrical_log_path = root / "electrical_solver_log.json"
    thermal_log_path = root / "thermal_solver_log.json"
    checks_path = root / "reference_solver_checks.json"
    recipe_path = root / "generation_recipe.py"
    _write_mesh(nodes_path, cells_path, coarse)
    _write_fields(electrical_path, thermal_path, coarse)
    shutil.copyfile(Path(__file__).resolve(), recipe_path)

    _write_json(
        mapping_path,
        {
            "mapping_version": "1.0",
            "coupling_type": "one_way",
            "source_quantity": "joule_loss_density",
            "target_quantity": "volumetric_heat_source",
            "source_location": "node",
            "target_location": "node",
            "method": "identity_on_shared_structured_mesh",
            "unit": "W/m^3",
            "relative_integrated_power_error": 0.0,
        },
    )
    _write_json(
        electrical_log_path,
        {
            "solver": "DADC reference finite-difference electrical solver",
            "version": "1.0",
            "equation": "div(sigma * grad(V)) = 0",
            "grid_shape": [17, 31],
            "iterations": coarse["electrical_iterations"],
            "residual_max_delta_v": coarse["electrical_residual"],
            "convergence_threshold_v": 1.0e-11,
            "status": "converged",
        },
    )
    _write_json(
        thermal_log_path,
        {
            "solver": "DADC reference finite-difference thermal solver",
            "version": "1.0",
            "equation": "-div(k * grad(T)) = joule_loss_density",
            "grid_shape": [17, 31],
            "iterations": coarse["thermal_iterations"],
            "residual_max_delta_k": coarse["thermal_residual"],
            "convergence_threshold_k": 1.0e-9,
            "status": "converged",
        },
    )
    _write_json(
        checks_path,
        {
            "check_version": "1.0",
            "total_electrical_power_w": coarse["total_power_w"],
            "mapped_thermal_source_power_w": coarse["total_power_w"],
            "coupling_relative_power_error": 0.0,
            "coarse_grid_shape": [17, 31],
            "fine_grid_shape": [33, 61],
            "coarse_maximum_temperature_k": coarse["maximum_temperature_k"],
            "fine_maximum_temperature_k": fine["maximum_temperature_k"],
            "mesh_relative_peak_temperature_rise_difference": mesh_relative_difference,
        },
    )

    entries = [
        _file_entry(nodes_path, root, file_id="mesh_nodes", stage="common", role="mesh", media_type="text/csv", value_origin="raw_solver_output"),
        _file_entry(cells_path, root, file_id="mesh_cells", stage="common", role="mesh", media_type="text/csv", value_origin="raw_solver_output"),
        _file_entry(electrical_path, root, file_id="electrical_fields", stage="electrical", role="raw_input", media_type="text/csv", value_origin="raw_solver_output"),
        _file_entry(thermal_path, root, file_id="thermal_fields", stage="thermal", role="raw_input", media_type="text/csv", value_origin="raw_solver_output"),
        _file_entry(mapping_path, root, file_id="coupling_map", stage="coupling", role="report", media_type="application/json", value_origin="calculated"),
        _file_entry(electrical_log_path, root, file_id="electrical_solver_log", stage="electrical", role="solver_log", media_type="application/json", value_origin="raw_solver_output"),
        _file_entry(thermal_log_path, root, file_id="thermal_solver_log", stage="thermal", role="solver_log", media_type="application/json", value_origin="raw_solver_output"),
        _file_entry(checks_path, root, file_id="reference_solver_checks", stage="validation", role="validation_evidence", media_type="application/json", value_origin="calculated"),
        _file_entry(recipe_path, root, file_id="generation_recipe", stage="common", role="script", media_type="text/x-python", value_origin="manual_entry"),
    ]
    bundle_path = root / "joule_thermal_bundle.json"
    _write_json(
        bundle_path,
        {
            "bundle_schema_version": "1.0",
            "bundle_type": "joule_thermal_field_bundle",
            "generated_at": generated_at,
            "operator_id": operator_id,
            "case": {
                "device_name": "DADC thin-film power resistor reference case",
                "device_class": "power_resistor",
                "device_subtype": "thin_film_power_resistor",
                "physics_domains": ["electromagnetics", "thermal"],
            },
            "coordinate_system": {
                "coordinate_system_ref": "cs_power_resistor_cartesian",
                "type": "cartesian",
                "coordinate_unit": "m",
                "origin": [0.0, 0.0, 0.0],
            },
            "mesh": {"type": "structured", "shape": [17, 31], "node_count": 527, "cell_count": 480},
            "parameters": parameters,
            "outputs": {
                "total_electrical_power_w": coarse["total_power_w"],
                "maximum_temperature_k": coarse["maximum_temperature_k"],
                "thermal_resistance_k_w": coarse["thermal_resistance_k_w"],
            },
            "references": [
                {
                    "source_id": "openfoam_joule_heating_equations",
                    "source_type": "url",
                    "title": "OpenFOAM Joule heating source documentation",
                    "uri": "https://doc.openfoam.com/2306/tools/processing/numerics/fvoptions/sources/rtm/jouleHeating/",
                    "accessed_at": generated_at,
                },
                {
                    "source_id": "mfem_joule_miniapp",
                    "source_type": "url",
                    "title": "MFEM Joule miniapp description",
                    "uri": "https://mfem.org/electromagnetics/",
                    "accessed_at": generated_at,
                },
            ],
            "files": entries,
        },
    )
    _write_json(
        root / "power_resistor_minimal_001.dadc.json",
        {
            "intake_schema_version": "1.0",
            "source": bundle_path.name,
            "adapter": "joule_thermal_field_bundle",
            "case_id": "power_resistor_multiphysics_001",
            "device_name": "DADC thin-film power resistor reference case",
            "device_class": "power_resistor",
            "device_subtype": "thin_film_power_resistor",
            "activity_type": "simulation_run",
            "operator_id": operator_id,
            "platform": sys.platform,
            "compute": "local_cpu",
        },
    )
    return bundle_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--operator-id", default="local_user")
    args = parser.parse_args(argv)
    bundle = generate(args.output_dir, operator_id=args.operator_id)
    print(json.dumps({"status": "generated", "bundle": str(bundle)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
