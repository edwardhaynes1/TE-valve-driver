#!/usr/bin/env python3
"""Re-record tests/golden/control_trace.json.gz after an INTENTIONAL change
to the control law or its tuning in driver/config.py.

    python tests/record_golden.py            show what changed, don't write
    python tests/record_golden.py --write    show what changed, then save

Read the summary before writing: every scenario listed as changed should be
one you expected to change.
"""
import contextlib
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import scenarios                                   # noqa: E402
from conftest import PackageAdapter                # noqa: E402

GOLDEN = HERE / "golden" / "control_trace.json.gz"


def summarise(old, new):
    old = {r["name"]: r["trace"] for r in old}
    for r in new:
        name, trace = r["name"], r["trace"]
        ref = old.get(name)
        if ref is None:
            print(f"  NEW        {name}")
            continue
        import json
        trace = json.loads(json.dumps(trace))
        diff = [i for i, (a, b) in enumerate(zip(trace, ref)) if a != b]
        if not diff and len(trace) == len(ref):
            print(f"  same       {name}")
            continue
        i = diff[0] if diff else min(len(trace), len(ref))
        t = i * scenarios.DT
        a, b = (trace[i] if i < len(trace) else {}), (ref[i] if i < len(ref) else {})
        keys = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        dmax = max((abs(x["duty"] - y["duty"]) for x, y in zip(trace, ref)), default=0)
        print(f"  CHANGED    {name}: first at t = {t:.2f} s ({', '.join(keys)}); "
              f"{len(diff)} steps differ; max duty change {dmax:.3f}")
        trips_old = sorted({x['trip_reason'] for x in ref if x['trip_reason']})
        trips_new = sorted({x['trip_reason'] for x in trace if x['trip_reason']})
        if trips_old != trips_new:
            print(f"             TRIPS CHANGED: {trips_old} -> {trips_new}")
    for name in set(old) - {r["name"] for r in new}:
        print(f"  REMOVED    {name}")


def main():
    with contextlib.redirect_stdout(io.StringIO()):
        new = scenarios.all_scenarios(PackageAdapter())
    old = scenarios.load(GOLDEN) if GOLDEN.exists() else []
    print("Control behaviour vs the recorded golden file:")
    summarise(old, new)
    if "--write" in sys.argv:
        scenarios.save(new, GOLDEN)
        print(f"\nwritten: {GOLDEN}")
    else:
        print("\n(dry run — add --write to save)")


if __name__ == "__main__":
    main()
