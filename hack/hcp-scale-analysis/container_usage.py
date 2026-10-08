#!/usr/bin/env python3
"""Usage percentiles of one control-plane container, per copy.

Reads one raw kube-burner dump (.ndjson or .ndjson.gz), keeps one container's CPU and
memory readings from the hosted cluster under test during the kube-burner churn phase,
and prints each copy's (pod's) number of readings and chosen percentiles: by default
the median (p50, typical usage) and p95 (usage at busy moments). Copies are listed
busiest first, by median. Repeated copies of the same reading, as in the 250- and
500-node dumps, are counted once.

Usage:
  python3 container_usage.py DUMP [--container NAME] [--percentiles P,P,...]

Requires numpy, and matplotlib through plot_controlplane_usage.
"""
import argparse
import datetime as dt
import json
import os
import sys
from collections import defaultdict

import numpy as np

from hcputil import opener
from plot_controlplane_usage import BYTES_TO_GIB, CPU_METRIC, CPU_TO_CORES, MEMORY_METRIC, parse_time

# The test takes its last reading a fraction of a second after the churn phase ends.
END_TOLERANCE = dt.timedelta(seconds=1)


def percentile_list(text):
    """Parse comma-separated percentiles such as "50,75,95", sorted and without repeats."""
    values = [float(part) for part in text.split(",")]
    if any(not 0 <= p <= 100 for p in values):
        raise argparse.ArgumentTypeError("percentiles must be between 0 and 100")
    return sorted(set(values))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", help="raw kube-burner dump (.ndjson or .ndjson.gz)")
    parser.add_argument("--container", default="kube-apiserver",
                        help="container to look at (default: kube-apiserver)")
    parser.add_argument("--percentiles", type=percentile_list, default=[50.0, 95.0], metavar="P,P,...",
                        help="comma-separated percentiles to print, from 0 to 100 "
                             "(default: 50,95; 100 is the highest reading)")
    return parser.parse_args()


def read_container(path, container):
    """Return (job_summary, readings), where readings[metric][(namespace, pod)] maps each
    reading's time to its raw value, for every copy of `container`."""
    job = None
    readings = {CPU_METRIC: defaultdict(dict), MEMORY_METRIC: defaultdict(dict)}
    with opener(path) as f:
        for line in f:
            if '"jobSummary"' not in line and CPU_METRIC not in line and MEMORY_METRIC not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            metric = record.get("metricName")
            if metric == "jobSummary":
                if job is None and record.get("clusterName"):
                    job = record
                continue
            labels = record.get("labels", {})
            if metric in readings and labels.get("container") == container:
                copy = readings[metric][(labels.get("namespace", ""), labels.get("pod", ""))]
                copy.setdefault(parse_time(record["timestamp"]), record["value"])
    return job or {}, readings


def main():
    args = parse_args()
    dump = os.path.expanduser(args.dump)
    job, readings = read_container(dump, args.container)
    cluster_id = job.get("clusterName")
    churn_start = parse_time(job.get("churnStartTimestamp"))
    churn_end = parse_time(job.get("churnEndTimestamp"))
    if not (cluster_id and churn_start and churn_end):
        sys.exit(f"{dump} has no churn phase; only runs with one are handled so far")

    name = os.path.basename(dump).removesuffix(".gz").removesuffix(".ndjson")
    print(f"{name}: {args.container} container, churn phase "
          f"{churn_start:%H:%M:%S}-{churn_end:%H:%M:%S} UTC")
    headers = [f"p{p:g}" for p in args.percentiles]
    columns = [max(6, len(h)) for h in headers]
    for metric, label, scale, digits in ((MEMORY_METRIC, "memory (GiB)", BYTES_TO_GIB, 2),
                                         (CPU_METRIC, "CPU (cores)", CPU_TO_CORES, 3)):
        copies = []
        for (namespace, pod), copy in readings[metric].items():
            if cluster_id not in namespace:
                continue
            values = [value * scale for when, value in copy.items()
                      if churn_start <= when <= churn_end + END_TOLERANCE]
            if values:
                results = [np.percentile(values, p) for p in args.percentiles]
                copies.append((np.median(values), results, len(values), pod))
        print(f"\n{label}")
        if not copies:
            print(f"  no readings for container {args.container!r}")
            continue
        width = max(len(pod) for *_, pod in copies)
        print(f"  {'copy':{width}}  readings" + "".join(f"  {h:>{c}}" for h, c in zip(headers, columns)))
        for _, results, count, pod in sorted(copies, key=lambda copy: copy[0], reverse=True):
            print(f"  {pod:{width}}  {count:8d}"
                  + "".join(f"  {r:{c}.{digits}f}" for r, c in zip(results, columns)))


if __name__ == "__main__":
    main()
