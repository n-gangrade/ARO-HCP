#!/usr/bin/env python3
"""Combine per-container control-plane usage from many kube-burner runs into one JSON file.

For each raw kube-burner dump it works out the same numbers as container_usage.py's summary
(same windows, same 5-minute CPU averages, copies being replaced left out) for memory and CPU,
and writes them with each run's details to one file, container-usage.json next to this script
by default. That file is the input for comparing sizes and for the PM page: rerun this
whenever new test runs arrive.

For each run: its worker nodes, OpenShift version, date, whether it had a churn phase, the
window used for memory and for CPU, and the share of expected readings that exist. For each
container, separately for memory (GiB) and CPU (cores): how many copies count, the p50 and
p95 of its busiest copy and of its average copy, and the share of expected readings that
exist, plus the CPU raw peak. The file is meant to be committed, so it leaves out cluster IDs.

Usage:
  python3 combine_usage.py DUMP [DUMP ...] [--output FILE]

Requires numpy, and matplotlib through plot_controlplane_usage.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

from container_usage import (MIN_COPY_SHARE, NO_CHURN_MEMORY_WINDOW, read_readings, steady_windows,
                             summarize, window_copies)
from plot_controlplane_usage import BYTES_TO_GIB, CPU_METRIC, CPU_TO_CORES, MEMORY_METRIC, SMOOTH_MINUTES

PERCENTILES = [50.0, 95.0]
DECIMALS = 3  # 0.001 GiB is about 1 MiB, and 0.001 cores is 1 millicore
# (name in the file, metric, conversion to GiB or cores, minutes to average readings over first)
METRICS = [("memory_gib", MEMORY_METRIC, BYTES_TO_GIB, 0),
           ("cpu_cores", CPU_METRIC, CPU_TO_CORES, SMOOTH_MINUTES)]
FIELDS = {
    "busiest_copy": "the highest p50 and p95 among the container's copies; every copy reserves the same, "
                    "so this is what a reservation has to fit",
    "average_copy": "the p50 and p95 averaged over the container's copies; times copies, what it actually uses",
    "readings_present": "share of the expected readings that exist; below 1 means missing data",
    "raw_peak": "highest single CPU reading of any copy, before averaging",
}
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "container-usage.json")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dumps", nargs="+", metavar="DUMP", help="raw kube-burner dumps (.ndjson or .ndjson.gz)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="JSON file to write (default: container-usage.json next to this script)")
    return parser.parse_args()


def by_percentile(values):
    return {f"p{p:g}": None if v is None else round(float(v), DECIMALS) for p, v in zip(PERCENTILES, values)}


def summarize_run(path):
    """The run's details and its per-container numbers, as stored in the file."""
    job, readings, reading_span = read_readings(path)
    cluster_id = job.get("clusterName")
    windows = steady_windows(job, reading_span, cluster_id) if cluster_id else None
    if not windows:
        sys.exit(f"no readings for the hosted cluster under test in {path}")
    run = {
        "run": os.path.basename(path).removesuffix(".gz").removesuffix(".ndjson"),
        "worker_nodes": job.get("workerNodesCount"),
        "ocp_version": job.get("ocpVersion"),
        "date": (job.get("timestamp") or "")[:10] or None,
        "churn_phase": windows[CPU_METRIC][0] == "churn phase",
        "windows": {},
        "readings_present": {},
    }
    containers = defaultdict(dict)
    for name, metric, scale, smooth_minutes in METRICS:
        description, start, end = windows[metric]
        run["windows"][name] = {"description": description, "start": start.isoformat(), "end": end.isoformat()}
        rows = summarize(window_copies(readings[metric], cluster_id, start, end, scale),
                         smooth_minutes, PERCENTILES)
        counted = sum(copies for _, _, copies, *_ in rows)
        present = sum(share * copies for _, _, copies, _, _, share, _ in rows)
        run["readings_present"][name] = round(present / counted, DECIMALS) if counted else None
        for component, container, copies, busiest, average, share, peak in rows:
            entry = {"copies": copies, "busiest_copy": by_percentile(busiest),
                     "average_copy": by_percentile(average), "readings_present": round(share, DECIMALS)}
            if smooth_minutes:
                entry["raw_peak"] = round(float(peak), DECIMALS)
            containers[(component, container)][name] = entry
    run["containers"] = [{"component": component, "container": container, **metrics}
                         for (component, container), metrics in sorted(containers.items())]
    return run


def to_json(data):
    """Like json.dumps(data, indent=2), but with each container on one line, so the file reads
    like a table and stays small."""
    lines = {}

    def placeholder(container):
        key = f"@container-{len(lines)}@"
        lines[key] = json.dumps(container)
        return key

    runs = [{**run, "containers": [placeholder(c) for c in run["containers"]]} for run in data["runs"]]
    text = json.dumps({**data, "runs": runs}, indent=2)
    for key, line in lines.items():
        text = text.replace(json.dumps(key), line, 1)
    return text + "\n"


def main():
    args = parse_args()
    runs = []
    for path in args.dumps:
        started = time.monotonic()
        run = summarize_run(os.path.expanduser(path))
        print(f"{run['run']}: {len(run['containers'])} containers ({time.monotonic() - started:.0f} s)",
              file=sys.stderr)
        runs.append(run)
    runs.sort(key=lambda run: (run["worker_nodes"] or 0, run["run"]))
    data = {
        "description": "Per-container control-plane usage of each kube-burner run, written by combine_usage.py",
        "settings": {
            "percentiles": PERCENTILES,
            "cpu_averaged_over_minutes": SMOOTH_MINUTES,
            "min_copy_share": MIN_COPY_SHARE,
            "no_churn_memory_window_minutes": NO_CHURN_MEMORY_WINDOW.total_seconds() / 60,
        },
        "fields": FIELDS,
        "runs": runs,
    }
    output = os.path.expanduser(args.output)
    with open(output, "w") as f:
        f.write(to_json(data))
    print(f"wrote {output}: {len(runs)} runs", file=sys.stderr)


if __name__ == "__main__":
    main()
