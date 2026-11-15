"""The measurement table of a data to ODE mapping: one row per measured value.

Units are never inferred: a missing unit is a problem. Every row cites a source and a
locator. `source` is a source id declared in the mapping's inputs.sources, or the literal
`proposal` when the table is an approved data-analyst `ode_measurements` proposal.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

from scripts import state
from scripts.data import sources

COLUMNS = ("measurement_id", "readout", "condition", "time", "time_unit", "dose", "dose_unit", "value",
           "value_unit", "error", "error_kind", "n", "use", "source", "locator")
OPTIONAL_COLUMNS = ("dose", "dose_unit", "error", "error_kind", "n")
USES = ("fit", "holdout")
ERROR_KINDS = ("sd", "sem")
PROPOSAL_SOURCE = "proposal"
_FLOATS = ("time", "dose", "value", "error")


def read(path: Path) -> list[dict]:
    """The rows of a measurement table; ValueError on a missing column or an unparseable number.

    Numbers become floats (`n` an int); an empty dose, error or n becomes None. Non-finite
    values are kept so that `check` can report them with the row.
    """
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [c.strip() for c in reader.fieldnames or []]
        if len(set(header)) != len(header) or not set(COLUMNS) <= set(header):
            raise ValueError(f"measurement table needs the columns {', '.join(COLUMNS)} "
                             f"(missing: {', '.join(c for c in COLUMNS if c not in header) or 'none'}; "
                             "duplicates are refused)")
        rows = []
        for line, raw in enumerate(reader, start=2):
            row = {c: (raw.get(c) or "").strip() for c in COLUMNS}
            for col in _FLOATS:
                if row[col] == "":
                    if col in ("time", "value"):
                        raise ValueError(f"line {line}: {col} is required")
                    row[col] = None
                    continue
                try:
                    row[col] = float(row[col])
                except ValueError:
                    raise ValueError(f"line {line}: {col} {row[col]!r} is not a number") from None
            if row["n"] == "":
                row["n"] = None
            else:
                try:
                    row["n"] = int(row["n"])
                except ValueError:
                    raise ValueError(f"line {line}: n {row['n']!r} is not an integer") from None
            rows.append(row)
    return rows


def _finite(value) -> bool:
    return isinstance(value, float) and math.isfinite(value)


def check(rows, source_ids: set[str]) -> list[str]:
    """Every problem with the rows, one message each; [] when valid."""
    errors: list[str] = []
    if not rows:
        return ["measurement table has no rows"]
    seen: set[str] = set()
    for n, row in enumerate(rows, start=1):
        mid = row.get("measurement_id")
        label = f"measurement {mid!r}" if mid else f"measurement row {n}"
        if not isinstance(mid, str) or not state.ID.fullmatch(mid):
            errors.append(f"{label}: measurement_id must be a safe identifier")
        elif mid in seen:
            errors.append(f"{label}: duplicate measurement_id")
        else:
            seen.add(mid)
        for col in ("readout", "condition", "time_unit", "value_unit"):
            if not isinstance(row.get(col), str) or not row[col].strip():
                errors.append(f"{label}: {col} is required (units are never inferred)" if col.endswith("unit")
                              else f"{label}: {col} is required")
        if not _finite(row.get("time")) or row["time"] < 0:
            errors.append(f"{label}: time must be a finite number >= 0")
        if not _finite(row.get("value")):
            errors.append(f"{label}: value must be a finite number")
        dose = row.get("dose")
        if dose is not None and (not _finite(dose) or dose < 0):
            errors.append(f"{label}: dose must be a finite number >= 0")
        if dose is not None and not row.get("dose_unit"):
            errors.append(f"{label}: dose_unit is required when dose is set (units are never inferred)")
        if dose is None and row.get("dose_unit"):
            errors.append(f"{label}: dose_unit is set without a dose")
        err, kind = row.get("error"), row.get("error_kind")
        if err is not None and (not _finite(err) or err < 0):
            errors.append(f"{label}: error must be a finite number >= 0")
        if err is not None and kind not in ERROR_KINDS:
            errors.append(f"{label}: error_kind must be one of {ERROR_KINDS} when error is set")
        if err is None and kind:
            errors.append(f"{label}: error_kind is set without an error")
        count = row.get("n")
        if count is not None and (type(count) is not int or count < 1):
            errors.append(f"{label}: n must be an integer >= 1")
        if row.get("use") not in USES:
            errors.append(f"{label}: use must be one of {USES}")
        if row.get("source") not in source_ids:
            errors.append(f"{label}: source {row.get('source')!r} is not one of {sorted(source_ids)}")
        problem = sources.check_locator(row.get("locator"))
        if problem:
            errors.append(f"{label}: locator: {problem}")
    return errors
