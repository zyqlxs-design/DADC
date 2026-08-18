"""Download one pinned Würth Elektronik inductor evidence set for DADC ingestion."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import platform
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


S_PARAMETER_URL = (
    "https://www.we-online.com/components/products/download/"
    "S-Parameter_744765056A%20%28rev22a%29.s2p"
)
DATASHEET_URL = "https://www.we-online.com/components/products/datasheet/744765056A.pdf"
PRODUCT_URL = "https://www.we-online.com/en/components/products/DESIGNKIT_744765A"
EXPECTED_SHA256 = {
    "S-Parameter_744765056A_rev22a.s2p": (
        "402d2b1060ed424b3ab24abf67a3c349e4521e96b8a5995276d53317347fc892"
    ),
    "744765056A_datasheet.pdf": (
        "5c0389153549ec11cbaeba4d9f35b27513b9a9ce1140c2cb7abbdbc7e54a6055"
    ),
}
MAX_DOWNLOAD_ATTEMPTS = 5


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(
    url: str,
    target: Path,
    *,
    max_attempts: int = MAX_DOWNLOAD_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    temporary = target.with_suffix(target.suffix + ".part")
    expected = EXPECTED_SHA256[target.name]
    failures: list[str] = []
    for attempt in range(1, max_attempts + 1):
        temporary.unlink(missing_ok=True)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "DADC/1.4 official-source-ingestion",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("xb") as output:
                content_type = response.headers.get_content_type()
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            size_bytes = temporary.stat().st_size
            if target.suffix.lower() == ".pdf":
                if content_type != "application/pdf":
                    raise RuntimeError(
                        f"expected application/pdf, received {content_type!r}"
                    )
                with temporary.open("rb") as stream:
                    if stream.read(5) != b"%PDF-":
                        raise RuntimeError("response does not begin with the PDF signature")
            digest = _sha256(temporary)
            if digest != expected:
                raise RuntimeError(
                    f"SHA-256 mismatch: expected {expected}, got {digest}; "
                    f"content_type={content_type!r}, size_bytes={size_bytes}"
                )
            temporary.replace(target)
            return {
                "file": target.name,
                "url": url,
                "sha256": digest,
                "size_bytes": target.stat().st_size,
                "download_attempts": attempt,
                "response_content_type": content_type,
            }
        except (
            http.client.IncompleteRead,
            TimeoutError,
            urllib.error.URLError,
            OSError,
            RuntimeError,
        ) as exc:
            failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            temporary.unlink(missing_ok=True)
            if attempt == max_attempts:
                history = "; ".join(failures)
                raise RuntimeError(
                    f"Unable to obtain pinned official file {target.name} after "
                    f"{max_attempts} attempts. {history}. Do not ingest an unreviewed file."
                ) from exc
            sleep(min(2 ** (attempt - 1), 8))
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and pin the DADC Würth 744765056A inductor validation sample."
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--operator-id", default="local_user")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir / ".download_staging"
    staging_dir.mkdir()
    s_parameter = output_dir / "S-Parameter_744765056A_rev22a.s2p"
    datasheet = output_dir / "744765056A_datasheet.pdf"
    staged_s_parameter = staging_dir / s_parameter.name
    staged_datasheet = staging_dir / datasheet.name
    source_manifest_path = output_dir / "source_manifest.json"
    intake_path = output_dir / "we_744765056A.dadc.json"
    accessed_at = _utc_now()

    try:
        downloads = [
            _download(S_PARAMETER_URL, staged_s_parameter),
            _download(DATASHEET_URL, staged_datasheet),
        ]
        staged_s_parameter.replace(s_parameter)
        staged_datasheet.replace(datasheet)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    source_manifest = {
        "source_manifest_version": "1.0",
        "created_at": accessed_at,
        "product_url": PRODUCT_URL,
        "downloads": downloads,
        "downloader": {
            "python": sys.version,
            "platform": platform.platform(),
            "script": Path(__file__).name,
        },
    }
    source_manifest_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    intake = {
        "intake_schema_version": "1.0",
        "source": s_parameter.name,
        "adapter": "touchstone_inductor",
        "case_id": "vendor_inductor_we_744765056a_real_001",
        "device_name": "Würth Elektronik WE-KI 0402 5.6 nH inductor 744765056A",
        "device_class": "inductor",
        "device_subtype": "wire_wound_ceramic_rf_inductor",
        "activity_type": "experiment_run",
        "manufacturer": "Würth Elektronik eiSos GmbH & Co. KG",
        "part_number": "744765056A",
        "construction": "wire_wound_ceramic_smt_inductor",
        "package_size": "0402A",
        "source_timezone": "+01:00",
        "source_timestamp": "2020-02-10T00:00:00+01:00",
        "operator_id": args.operator_id,
        "platform": "vendor VNA measurement",
        "compute": "not_applicable",
        "processed_at": accessed_at,
        "measurement_context": {
            "provider": "Würth Elektronik eiSos GmbH & Co. KG",
            "measurement_date": "2020-02-10",
            "measurement_time_of_day": "not_recorded",
            "instrument": "Keysight PNA-X",
            "instrument_version": "N5242B",
            "calibration": "eCAL N4691D",
            "de_embedding": "AFR",
            "fixture_semantics": "representative implementation; fixture behavior de-embedded",
            "sampling_points_stated_in_source": 801,
            "if_bandwidth": "100 kHz",
            "averaging": "not used",
            "model_conversion_note": "vendor states the model was converted from impedance",
        },
        "literature_context": {
            "document_type": "manufacturer datasheet",
            "document_revision": "003.000",
            "document_date": "2021-05-12",
            "published_at": "2021-05-12T00:00:00Z",
            "citation": "Würth Elektronik 744765056A datasheet, revision 003.000",
            "uri": DATASHEET_URL,
            "product_uri": PRODUCT_URL,
            "accessed_at": accessed_at,
        },
        "metric_reference_frequencies_hz": {
            "inductance": 250000000.0,
            "q_factor": 250000000.0,
        },
        "datasheet_specifications": [
            {
                "property": "inductance",
                "value": 5.6,
                "unit": "nH",
                "qualifier": "nominal",
                "tolerance": {"kind": "plus_minus", "value": 5.0, "unit": "%"},
                "test_conditions": [
                    {"quantity": "frequency", "value": 250000000.0, "unit": "Hz"}
                ],
                "value_origin": "literature_extracted",
            },
            {
                "property": "quality_factor",
                "value": 23.0,
                "unit": "1",
                "qualifier": "minimum",
                "test_conditions": [
                    {"quantity": "frequency", "value": 250000000.0, "unit": "Hz"}
                ],
                "value_origin": "literature_extracted",
            },
            {
                "property": "quality_factor",
                "value": 46.0,
                "unit": "1",
                "qualifier": "typical",
                "test_conditions": [
                    {"quantity": "frequency", "value": 900000000.0, "unit": "Hz"}
                ],
                "value_origin": "literature_extracted",
            },
            {
                "property": "dc_resistance",
                "value": 0.083,
                "unit": "ohm",
                "qualifier": "maximum",
                "test_conditions": [
                    {"quantity": "temperature", "value": 20.0, "unit": "degC"}
                ],
                "value_origin": "literature_extracted",
            },
            {
                "property": "rated_current",
                "value": 0.760,
                "unit": "A",
                "qualifier": "maximum",
                "test_conditions": [
                    {"quantity": "temperature_rise", "value": 15.0, "unit": "K"}
                ],
                "value_origin": "literature_extracted",
            },
            {
                "property": "self_resonant_frequency",
                "value": 5800000000.0,
                "unit": "Hz",
                "qualifier": "minimum",
                "test_conditions": [],
                "value_origin": "literature_extracted",
            },
        ],
        "companion_artifacts": [
            {
                "path": datasheet.name,
                "role": "literature_source",
                "media_type": "application/pdf",
                "value_origin": "literature_extracted",
            },
            {
                "path": source_manifest_path.name,
                "role": "report",
                "media_type": "application/json",
                "value_origin": "calculated",
                "activity_scope": "processing",
            },
        ],
    }
    intake_path.write_text(
        json.dumps(intake, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), "intake": str(intake_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
