# Generated examples

Run the following command from the repository root to materialize the antenna, RF-filter, and multiphysics examples as real JSON/HDF5/Parquet/native files:

```bash
python3 scripts/create_examples.py examples/generated
```

The generator uses fixed numerical inputs and exclusive-create writes. It will not overwrite a non-empty destination unless `--replace` is explicitly supplied and the target already contains a DADC `repository.json` manifest.

## Real HFSS Touchstone fixture

`tests/fixtures/bandpass_filter_run_001_HFSSDesign1.s2p` is a real two-port
HFSS export. Create a separate repository from it with:

```bash
python3 -m dadc ingest-touchstone \
  tests/fixtures/bandpass_filter_run_001_HFSSDesign1.s2p \
  examples/real_hfss \
  --case-id hfss_bandpass_real_001 \
  --device-name "HFSS official interdigital bandpass filter" \
  --filter-order 8 \
  --source-timezone +08:00
```

The source timezone and filter order are explicit human inputs. Change them if
they do not match the machine clock and verified source design.
