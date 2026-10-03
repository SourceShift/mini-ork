# CSV export for the run ledger

Add a `mini-ork ledger export --csv <path>` command that writes every ledger
row of one run to a CSV file.

## Inputs

- `--run <run_id>`: the run whose ledger rows are exported (required).
- `--csv <path>`: destination file; parent directories are created.

## Outputs

- A UTF-8 CSV file with the header `clause_id,deliverable_id,run_id,commit,verdict`.
- One line per ledger row, ordered by `clause_id` then `deliverable_id`.
- Exit code 0 when the file is written.

## Edge cases

- Unknown run id: no file is written, stderr names the run, exit code 2.
- A run with zero ledger rows: the file contains only the header line.
- Fields containing commas or quotes are quoted per RFC 4180.

## Acceptance criteria

- AC1: for a run with 3 ledger rows the CSV has exactly 4 lines (header + 3).
- AC2: an unknown run id exits with code 2 and creates no file.
- AC3: a field value `a,"b"` round-trips through `csv.reader` unchanged.
- Verify with `python3 -m pytest -q tests/example.py`.
