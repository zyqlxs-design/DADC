"""Pluggable source-adapter registry for warehouse ingestion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .importer import (
    TouchstoneIngestionResult,
    ingest_touchstone_antenna_repository,
    ingest_touchstone_filter_repository,
    ingest_touchstone_inductor_repository,
)
from .field_bundle import FieldBundleIngestionResult, ingest_joule_thermal_field_bundle
from .tabular import TabularIngestionResult, ingest_tabular_experiment_repository

_TOUCHSTONE_SUFFIX = re.compile(r"\.s(?P<ports>[1-9][0-9]*)p$", re.IGNORECASE)


@dataclass(frozen=True)
class ProbeResult:
    adapter_id: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class AdapterCapability:
    """Machine-readable declaration of one installed adapter's safe boundary."""

    adapter_id: str
    adapter_version: str
    source_kinds: tuple[str, ...]
    source_formats: tuple[str, ...]
    device_classes: tuple[str, ...]
    activity_types: tuple[str, ...]
    physics_domains: tuple[str, ...]
    automation_level: str
    manifest_required: bool
    maturity: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "source_kinds": list(self.source_kinds),
            "source_formats": list(self.source_formats),
            "device_classes": list(self.device_classes),
            "activity_types": list(self.activity_types),
            "physics_domains": list(self.physics_domains),
            "automation_level": self.automation_level,
            "manifest_required": self.manifest_required,
            "maturity": self.maturity,
        }


class SourceAdapter(Protocol):
    adapter_id: str
    adapter_version: str
    capability: AdapterCapability

    def probe(self, source: Path, intake: dict[str, Any]) -> ProbeResult:
        """Score whether this adapter can safely interpret ``source``."""

    def build_case_repository(
        self,
        source: Path,
        target: Path,
        intake: dict[str, Any],
    ) -> TouchstoneIngestionResult | FieldBundleIngestionResult | TabularIngestionResult:
        """Build and validate one staged DADC repository."""


class TouchstoneRFFilterAdapter:
    adapter_id = "touchstone_rf_filter"
    adapter_version = "1.0.0"
    capability = AdapterCapability(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        source_kinds=("simulation_export",),
        source_formats=("Touchstone .s2p+",),
        device_classes=("rf_filter",),
        activity_types=("simulation_run",),
        physics_domains=("electromagnetics",),
        automation_level="automatic_with_manifest",
        manifest_required=True,
        maturity="validated",
    )

    def probe(self, source: Path, intake: dict[str, Any]) -> ProbeResult:
        match = _TOUCHSTONE_SUFFIX.search(source.name)
        if not match:
            return ProbeResult(self.adapter_id, 0.0, "filename is not .sNp")
        if int(match.group("ports")) < 2:
            return ProbeResult(self.adapter_id, 0.0, "RF-filter adapter requires at least two ports")
        if intake.get("device_class") not in (None, "rf_filter"):
            return ProbeResult(
                self.adapter_id,
                0.0,
                f"device_class is {intake.get('device_class')!r}, not rf_filter",
            )
        try:
            head = source.read_bytes()[:65536].decode("utf-8", errors="replace")
        except OSError as exc:
            return ProbeResult(self.adapter_id, 0.0, f"cannot read source: {exc}")
        has_option_line = any(line.lstrip().startswith("#") for line in head.splitlines())
        confidence = 0.98 if has_option_line else 0.60
        reason = "Touchstone suffix and option line" if has_option_line else "Touchstone suffix only"
        return ProbeResult(self.adapter_id, confidence, reason)

    def build_case_repository(
        self,
        source: Path,
        target: Path,
        intake: dict[str, Any],
    ) -> TouchstoneIngestionResult:
        required = ("case_id", "device_name", "filter_order", "source_timezone")
        missing = [key for key in required if intake.get(key) in (None, "")]
        if missing:
            raise ValueError(f"Touchstone RF-filter intake is missing: {', '.join(missing)}")
        if intake.get("device_class", "rf_filter") != "rf_filter":
            raise ValueError("touchstone_rf_filter requires device_class=rf_filter")
        activity_type = intake.get("activity_type", "simulation_run")
        if activity_type != "simulation_run":
            raise ValueError(
                "touchstone_rf_filter currently accepts simulation_run only; "
                "vendor and literature semantics use their own source adapters"
            )
        return ingest_touchstone_filter_repository(
            source,
            target,
            case_id=str(intake["case_id"]),
            device_name=str(intake["device_name"]),
            filter_order=int(intake["filter_order"]),
            source_timezone=str(intake["source_timezone"]),
            companion_artifacts=list(intake.get("companion_artifacts", [])),
            operator_id=str(intake.get("operator_id", "local_user")),
            platform=str(intake.get("platform", "windows")),
            compute=str(intake.get("compute", "not_recorded")),
            solver_edition=str(intake.get("solver_edition", "Student")),
            processed_at=intake.get("processed_at"),
        )


class TouchstoneAntennaAdapter:
    adapter_id = "touchstone_antenna"
    adapter_version = "1.0.0"
    capability = AdapterCapability(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        source_kinds=("simulation_export",),
        source_formats=("Touchstone .s1p",),
        device_classes=("antenna",),
        activity_types=("simulation_run",),
        physics_domains=("electromagnetics",),
        automation_level="automatic_with_manifest",
        manifest_required=True,
        maturity="validated",
    )

    def probe(self, source: Path, intake: dict[str, Any]) -> ProbeResult:
        match = _TOUCHSTONE_SUFFIX.search(source.name)
        if not match or int(match.group("ports")) != 1:
            return ProbeResult(self.adapter_id, 0.0, "single-feed antenna adapter requires .s1p")
        if intake.get("device_class") not in (None, "antenna"):
            return ProbeResult(
                self.adapter_id,
                0.0,
                f"device_class is {intake.get('device_class')!r}, not antenna",
            )
        try:
            head = source.read_bytes()[:65536].decode("utf-8", errors="replace")
        except OSError as exc:
            return ProbeResult(self.adapter_id, 0.0, f"cannot read source: {exc}")
        has_option_line = any(line.lstrip().startswith("#") for line in head.splitlines())
        confidence = 0.99 if has_option_line else 0.60
        reason = "single-port Touchstone suffix and option line" if has_option_line else "s1p suffix only"
        return ProbeResult(self.adapter_id, confidence, reason)

    def build_case_repository(
        self,
        source: Path,
        target: Path,
        intake: dict[str, Any],
    ) -> TouchstoneIngestionResult:
        required = ("case_id", "device_name", "source_timezone", "feed_type", "radiation_mode")
        missing = [key for key in required if intake.get(key) in (None, "")]
        if missing:
            raise ValueError(f"Touchstone antenna intake is missing: {', '.join(missing)}")
        if intake.get("device_class", "antenna") != "antenna":
            raise ValueError("touchstone_antenna requires device_class=antenna")
        activity_type = intake.get("activity_type", "simulation_run")
        if activity_type != "simulation_run":
            raise ValueError("touchstone_antenna currently accepts simulation_run only")
        return ingest_touchstone_antenna_repository(
            source,
            target,
            case_id=str(intake["case_id"]),
            device_name=str(intake["device_name"]),
            source_timezone=str(intake["source_timezone"]),
            feed_type=str(intake["feed_type"]),
            radiation_mode=str(intake["radiation_mode"]),
            companion_artifacts=list(intake.get("companion_artifacts", [])),
            device_subtype=str(intake.get("device_subtype", "probe_fed_patch_antenna")),
            operator_id=str(intake.get("operator_id", "local_user")),
            platform=str(intake.get("platform", "windows")),
            compute=str(intake.get("compute", "not_recorded")),
            solver_edition=str(intake.get("solver_edition", "Student")),
            processed_at=intake.get("processed_at"),
        )


class TouchstoneInductorAdapter:
    """Interpret a two-port vendor inductor measurement with datasheet evidence."""

    adapter_id = "touchstone_inductor"
    adapter_version = "1.0.0"
    capability = AdapterCapability(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        source_kinds=("vendor_measurement", "vendor_datasheet"),
        source_formats=("Touchstone .s2p", "PDF companion"),
        device_classes=("inductor",),
        activity_types=("experiment_run", "literature_record", "data_processing"),
        physics_domains=("electromagnetics",),
        automation_level="automatic_with_manifest_and_pinned_companion",
        manifest_required=True,
        maturity="validated",
    )

    def probe(self, source: Path, intake: dict[str, Any]) -> ProbeResult:
        match = _TOUCHSTONE_SUFFIX.search(source.name)
        if not match or int(match.group("ports")) != 2:
            return ProbeResult(self.adapter_id, 0.0, "inductor adapter requires .s2p")
        if intake.get("device_class") != "inductor":
            return ProbeResult(
                self.adapter_id,
                0.0,
                "inductor adapter requires explicit device_class='inductor'",
            )
        if intake.get("activity_type") not in (None, "experiment_run"):
            return ProbeResult(
                self.adapter_id,
                0.0,
                "vendor inductor adapter requires activity_type=experiment_run",
            )
        try:
            head = source.read_bytes()[:65536].decode("cp1252")
        except OSError as exc:
            return ProbeResult(self.adapter_id, 0.0, f"cannot read source: {exc}")
        has_option_line = any(line.lstrip().startswith("#") for line in head.splitlines())
        confidence = 0.99 if has_option_line else 0.60
        reason = "two-port Touchstone and explicit inductor intake" if has_option_line else "s2p suffix only"
        return ProbeResult(self.adapter_id, confidence, reason)

    def build_case_repository(
        self,
        source: Path,
        target: Path,
        intake: dict[str, Any],
    ) -> TouchstoneIngestionResult:
        required = (
            "case_id",
            "device_name",
            "source_timezone",
            "source_timestamp",
            "manufacturer",
            "part_number",
            "construction",
            "package_size",
            "datasheet_specifications",
            "measurement_context",
            "literature_context",
            "metric_reference_frequencies_hz",
            "companion_artifacts",
        )
        missing = [key for key in required if intake.get(key) in (None, "", [], {})]
        if missing:
            raise ValueError(f"Touchstone inductor intake is missing: {', '.join(missing)}")
        if intake.get("device_class", "inductor") != "inductor":
            raise ValueError("touchstone_inductor requires device_class=inductor")
        if intake.get("activity_type", "experiment_run") != "experiment_run":
            raise ValueError("touchstone_inductor requires activity_type=experiment_run")
        companions = list(intake["companion_artifacts"])
        if sum(item.get("role") == "literature_source" for item in companions) != 1:
            raise ValueError("touchstone_inductor requires exactly one literature_source companion")
        return ingest_touchstone_inductor_repository(
            source,
            target,
            case_id=str(intake["case_id"]),
            device_name=str(intake["device_name"]),
            manufacturer=str(intake["manufacturer"]),
            part_number=str(intake["part_number"]),
            construction=str(intake["construction"]),
            package_size=str(intake["package_size"]),
            datasheet_specifications=list(intake["datasheet_specifications"]),
            measurement_context=dict(intake["measurement_context"]),
            literature_context=dict(intake["literature_context"]),
            source_timezone=str(intake["source_timezone"]),
            source_timestamp=str(intake["source_timestamp"]),
            metric_reference_frequencies_hz={
                str(key): float(value)
                for key, value in dict(intake["metric_reference_frequencies_hz"]).items()
            },
            companion_artifacts=companions,
            device_subtype=str(
                intake.get("device_subtype", "wire_wound_ceramic_rf_inductor")
            ),
            operator_id=str(intake.get("operator_id", "local_user")),
            platform=str(intake.get("platform", "vendor_measurement")),
            compute=str(intake.get("compute", "not_applicable")),
            processed_at=intake.get("processed_at"),
        )


class JouleThermalFieldBundleAdapter:
    """Interpret one self-checking electro-thermal field bundle."""

    adapter_id = "joule_thermal_field_bundle"
    adapter_version = "1.0.0"
    capability = AdapterCapability(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        source_kinds=("simulation_bundle",),
        source_formats=("DADC Joule-thermal JSON+CSV bundle",),
        device_classes=("power_resistor",),
        activity_types=("simulation_run", "data_processing"),
        physics_domains=("electromagnetics", "thermal"),
        automation_level="automatic_for_self_checking_bundle",
        manifest_required=True,
        maturity="validated",
    )

    def probe(self, source: Path, intake: dict[str, Any]) -> ProbeResult:
        if source.suffix.lower() != ".json":
            return ProbeResult(self.adapter_id, 0.0, "field-bundle source must be JSON")
        if intake.get("device_class") not in (None, "power_resistor"):
            return ProbeResult(
                self.adapter_id,
                0.0,
                "Joule-thermal adapter requires device_class='power_resistor'",
            )
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ProbeResult(self.adapter_id, 0.0, f"cannot read bundle JSON: {exc}")
        if value.get("bundle_type") != "joule_thermal_field_bundle":
            return ProbeResult(self.adapter_id, 0.0, "bundle_type is not joule_thermal_field_bundle")
        return ProbeResult(self.adapter_id, 1.0, "explicit Joule-thermal bundle type")

    def build_case_repository(
        self,
        source: Path,
        target: Path,
        intake: dict[str, Any],
    ) -> FieldBundleIngestionResult:
        required = ("case_id", "device_name")
        missing = [key for key in required if intake.get(key) in (None, "")]
        if missing:
            raise ValueError(f"Joule-thermal field intake is missing: {', '.join(missing)}")
        if intake.get("device_class", "power_resistor") != "power_resistor":
            raise ValueError("joule_thermal_field_bundle requires device_class=power_resistor")
        if intake.get("activity_type", "simulation_run") != "simulation_run":
            raise ValueError("joule_thermal_field_bundle requires activity_type=simulation_run")
        return ingest_joule_thermal_field_bundle(
            source,
            target,
            case_id=str(intake["case_id"]),
            device_name=str(intake["device_name"]),
            operator_id=str(intake.get("operator_id", "local_user")),
            platform=str(intake.get("platform", "not_recorded")),
            compute=str(intake.get("compute", "not_recorded")),
            processed_at=intake.get("processed_at"),
        )


class TabularExperimentCSVAdapter:
    """Interpret explicitly described real or complex experimental CSV curves."""

    adapter_id = "tabular_experiment_csv"
    adapter_version = "1.0.0"
    capability = AdapterCapability(
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        source_kinds=("laboratory_measurement", "instrument_export"),
        source_formats=("CSV with explicit DADC intake manifest",),
        device_classes=("*",),
        activity_types=("experiment_run", "data_processing"),
        physics_domains=("*",),
        automation_level="automatic_with_explicit_semantic_manifest",
        manifest_required=True,
        maturity="validated_minimal",
    )

    def probe(self, source: Path, intake: dict[str, Any]) -> ProbeResult:
        if source.suffix.lower() != ".csv":
            return ProbeResult(self.adapter_id, 0.0, "tabular experiment adapter requires .csv")
        if intake.get("activity_type") not in (None, "experiment_run"):
            return ProbeResult(
                self.adapter_id,
                0.0,
                "tabular experiment adapter requires activity_type=experiment_run",
            )
        if not isinstance(intake.get("tabular_contract"), dict):
            return ProbeResult(
                self.adapter_id,
                0.40,
                "CSV suffix is insufficient; explicit tabular_contract is required",
            )
        return ProbeResult(
            self.adapter_id,
            0.99,
            "CSV suffix and explicit tabular_contract",
        )

    def build_case_repository(
        self,
        source: Path,
        target: Path,
        intake: dict[str, Any],
    ) -> TabularIngestionResult:
        return ingest_tabular_experiment_repository(source, target, intake=intake)


class AdapterRegistry:
    """Select exactly one adapter; ambiguous and unknown inputs are rejected."""

    def __init__(self, adapters: list[SourceAdapter] | None = None):
        self.adapters: list[SourceAdapter] = adapters or [
            TouchstoneRFFilterAdapter(),
            TouchstoneAntennaAdapter(),
            TouchstoneInductorAdapter(),
            JouleThermalFieldBundleAdapter(),
            TabularExperimentCSVAdapter(),
        ]

    def catalog(self) -> list[dict[str, Any]]:
        """Return stable JSON-ready capability declarations for agent planning."""

        return [
            adapter.capability.to_dict()
            for adapter in sorted(self.adapters, key=lambda item: item.adapter_id)
        ]

    def select(self, source: Path, intake: dict[str, Any]) -> tuple[SourceAdapter, ProbeResult]:
        requested = intake.get("adapter")
        candidates = self.adapters
        if requested:
            candidates = [adapter for adapter in candidates if adapter.adapter_id == requested]
            if not candidates:
                known = ", ".join(adapter.adapter_id for adapter in self.adapters)
                raise ValueError(f"Unknown adapter {requested!r}; installed adapters: {known}")

        scored = sorted(
            ((adapter.probe(source, intake), adapter) for adapter in candidates),
            key=lambda item: item[0].confidence,
            reverse=True,
        )
        if not scored or scored[0][0].confidence < 0.80:
            details = "; ".join(
                f"{probe.adapter_id}={probe.confidence:.2f} ({probe.reason})"
                for probe, _ in scored
            )
            raise ValueError(f"No source adapter accepted {source.name}; {details or 'no adapters installed'}")
        if len(scored) > 1 and scored[0][0].confidence == scored[1][0].confidence:
            raise ValueError(
                "Ambiguous source adapters: "
                f"{scored[0][0].adapter_id} and {scored[1][0].adapter_id}"
            )
        probe, adapter = scored[0]
        return adapter, probe
