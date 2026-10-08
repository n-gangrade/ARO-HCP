#!/usr/bin/env python3
"""Usage percentiles of control-plane containers during the steady part of one kube-burner run.

Reads one raw kube-burner dump (.ndjson or .ndjson.gz) and keeps the CPU and memory
readings of the hosted cluster under test during the run's steady window: the kube-burner
churn phase. A run without one (the 500-node run) never settles, so memory uses its last
10 minutes of readings, after memory levels off, and CPU uses the whole run, which is all
create phase and so a conservative (high) estimate. Treat such a run as low confidence.

By default it prints one row per container, for every container: how many copies (pods)
it has, the chosen percentiles of its busiest copy and of its average copy (the mean
over its copies), and what share of the expected readings exist. Copies only count if
they have readings for at least half the window, which leaves out copies being replaced.
Different components can have containers with the same name (for example "manager"), so
rows are per component and container.

With --container NAME it instead lists every copy of that one container, including copies
being replaced, busiest first by median and grouped by component.

Percentiles default to the median (p50, typical usage) and p95 (usage at busy moments).
Memory percentiles use the readings as recorded. CPU readings are first averaged over
5 minutes (see --smooth), which hides bursts lasting only seconds; the highest CPU
reading as recorded is shown separately. Missing readings are skipped, never counted
as zero. Repeated copies of the same reading, as in the 250- and 500-node dumps, are
counted once.

Usage:
  python3 container_usage.py DUMP [--container NAME] [--percentiles P,P,...] [--smooth MINUTES] [--top N]

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
from plot_controlplane_usage import (BYTES_TO_GIB, CPU_METRIC, CPU_TO_CORES, MEMORY_METRIC, SMOOTH_MINUTES,
                                     parse_time, rolling_average)

# The test takes its last reading a fraction of a second after the churn phase ends.
END_TOLERANCE = dt.timedelta(seconds=1)
NO_CHURN_MEMORY_WINDOW = dt.timedelta(minutes=10)
MIN_COPY_SHARE = 0.5  # in the summary, a copy counts only with readings for this share of the window


def percentile_list(text):
    """Parse comma-separated percentiles such as "50,75,95", sorted and without repeats."""
    values = [float(part) for part in text.split(",")]
    if any(not 0 <= p <= 100 for p in values):
        raise argparse.ArgumentTypeError("percentiles must be between 0 and 100")
    return sorted(set(values))


def non_negative_minutes(text):
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("minutes can't be negative")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", help="raw kube-burner dump (.ndjson or .ndjson.gz)")
    parser.add_argument("--container",
                        help="list every copy of this one container instead of summarizing all containers")
    parser.add_argument("--percentiles", type=percentile_list, default=[50.0, 95.0], metavar="P,P,...",
                        help="comma-separated percentiles to print, from 0 to 100 "
                             "(default: 50,95; 100 is the highest reading)")
    parser.add_argument("--smooth", type=non_negative_minutes, default=SMOOTH_MINUTES, metavar="MINUTES",
                        help=f"average CPU over this many minutes before taking percentiles "
                             f"(default: {SMOOTH_MINUTES}; 0 uses the readings as recorded)")
    parser.add_argument("--top", type=int, default=0, metavar="N",
                        help="in the summary, show only the N biggest containers (default: all)")
    args = parser.parse_args()
    if args.top < 0:
        parser.error("--top can't be negative")
    return args


def component_of(pod):
    """The component a pod is a copy of: its name without the suffix Kubernetes adds,
    e.g. etcd-0 -> etcd and kube-apiserver-66f85f7774-kjd5q -> kube-apiserver."""
    parts = pod.split("-")
    return "-".join(parts[:-1]) if parts[-1].isdigit() else "-".join(parts[:-2])


def read_readings(path, container=None):
    """Return (job_summary, readings, reading_span), where readings[metric][(namespace, pod, container)]
    maps each reading's time to its raw value, for every container or only `container`, and
    reading_span[namespace] is the (first, last) time of that namespace's readings of any container."""
    job = None
    readings = {CPU_METRIC: defaultdict(dict), MEMORY_METRIC: defaultdict(dict)}
    reading_span = {}
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
            name = labels.get("container", "")
            # "" and "POD" are pod sandbox (pause) cgroups, not workload containers.
            if metric not in readings or name in ("", "POD"):
                continue
            namespace, when = labels.get("namespace", ""), parse_time(record["timestamp"])
            first, last = reading_span.get(namespace, (when, when))
            reading_span[namespace] = (min(first, when), max(last, when))
            if container and name != container:
                continue
            copy = readings[metric][(namespace, labels.get("pod", ""), name)]
            copy.setdefault(when, record["value"])
    return job or {}, readings, reading_span


def copy_stats(points, smooth_minutes, percentiles):
    """(median reading, [percentiles], highest reading, number of readings) for one copy's
    [(time, value)]; with smooth_minutes, the percentiles come from averages over that long."""
    raw = [value for _, value in points]
    values = [value for _, value in rolling_average(points, smooth_minutes)] if smooth_minutes else raw
    # A copy present for less than one averaging window has no averages, so no percentiles.
    results = [np.percentile(values, p) if values else None for p in percentiles]
    return np.median(raw), results, max(raw), len(raw)


def cells(values, columns, digits):
    return "".join(f"  {v:{c}.{digits}f}" if v is not None else f"  {'-':>{c}}" for v, c in zip(values, columns))


def print_copies(copies, smooth_minutes, percentiles, digits):
    """One row per copy of a single container, grouped by component, busiest first."""
    headers = [f"p{p:g}" for p in percentiles]
    columns = [max(6, len(h)) for h in headers]
    rows = [(*copy_stats(points, smooth_minutes, percentiles), pod) for (_, pod, _), points in copies.items()]
    width = max(len(pod) for *_, pod in rows)
    print(f"  {'copy':{width}}  readings" + "".join(f"  {h:>{c}}" for h, c in zip(headers, columns))
          + ("  raw peak" if smooth_minutes else ""))
    components = defaultdict(list)
    for row in rows:
        components[component_of(row[-1])].append(row)
    for component in sorted(components, key=lambda c: max(row[0] for row in components[c]), reverse=True):
        if len(components) > 1:
            print(f"  {component}:")
        for _, results, peak, count, pod in sorted(components[component], key=lambda row: row[0], reverse=True):
            print(f"  {pod:{width}}  {count:8d}{cells(results, columns, digits)}"
                  + (f"  {peak:8.{digits}f}" if smooth_minutes else ""))


def summarize(copies, smooth_minutes, percentiles):
    """Combine copies into one row per component and container, biggest first by the busiest copy's
    first percentile: (component, container, copies, busiest, average, readings_present, raw_peak),
    where busiest and average hold one value per percentile. Copies with readings for less than
    MIN_COPY_SHARE of the window, such as copies being replaced, are left out."""
    expected = len({when for points in copies.values() for when, _ in points})
    groups = defaultdict(list)
    for (_, pod, container), points in copies.items():
        if len(points) >= MIN_COPY_SHARE * expected:
            groups[(component_of(pod), container)].append(copy_stats(points, smooth_minutes, percentiles))
    rows = []
    for (component, container), stats in groups.items():
        by_percentile = [[results[i] for _, results, _, _ in stats if results[i] is not None]
                         for i in range(len(percentiles))]
        busiest = [max(values) if values else None for values in by_percentile]
        average = [float(np.mean(values)) if values else None for values in by_percentile]
        present = sum(count for *_, count in stats) / (len(stats) * expected)
        peak = max(peak for _, _, peak, _ in stats)
        rows.append((component, container, len(stats), busiest, average, present, peak))
    rows.sort(key=lambda row: row[3][0] if row[3][0] is not None else float("-inf"), reverse=True)
    return rows


def print_summary(copies, smooth_minutes, percentiles, digits, top):
    """One row per component and container, with its busiest and average copy, biggest first."""
    rows = summarize(copies, smooth_minutes, percentiles)
    shown = rows[:top] if top else rows

    headers = [f"p{p:g}" for p in percentiles]
    columns = [max(6, len(h)) for h in headers]
    columns[-1] += max(0, len("busiest copy") - (sum(columns) + 2 * (len(columns) - 1)))
    group = sum(columns) + 2 * (len(columns) - 1)
    names = [f"{component} / {container}" for component, container, *_ in shown]
    width = max([len("component / container")] + [len(name) for name in names])
    print(f"  {'':{width}}  {'':6}  {'busiest copy':^{group}}  {'average copy':^{group}}")
    print(f"  {'component / container':{width}}  copies"
          + "".join(f"  {h:>{c}}" for h, c in zip(headers * 2, columns * 2))
          + "  readings" + ("  raw peak" if smooth_minutes else ""))
    for name, (_, _, count, busiest, average, present, peak) in zip(names, shown):
        print(f"  {name:{width}}  {count:6d}{cells(busiest, columns, digits)}{cells(average, columns, digits)}"
              f"  {present:8.0%}" + (f"  {peak:8.{digits}f}" if smooth_minutes else ""))
    if len(shown) < len(rows):
        print(f"  ... and {len(rows) - len(shown)} more")


def window_copies(readings, cluster_id, start, end, scale):
    """{(namespace, pod, container): [(time, value)]} for the tested cluster's copies, keeping only
    the readings inside the window and converting them to GiB or cores with `scale`."""
    copies = {}
    for key, copy in readings.items():
        if cluster_id in key[0]:
            points = sorted((when, value * scale) for when, value in copy.items()
                            if start <= when <= end + END_TOLERANCE)
            if points:
                copies[key] = points
    return copies


def steady_windows(job, reading_span, cluster_id):
    """{metric: (description, start, end)}: the readings to use for each metric. That's the churn
    phase, or for a run without one, the last NO_CHURN_MEMORY_WINDOW of readings for memory and
    the whole run for CPU. None when there is nothing to go on."""
    churn_start = parse_time(job.get("churnStartTimestamp"))
    churn_end = parse_time(job.get("churnEndTimestamp"))
    if churn_start and churn_end:
        churn = ("churn phase", churn_start, churn_end)
        return {MEMORY_METRIC: churn, CPU_METRIC: churn}
    spans = [span for namespace, span in reading_span.items() if cluster_id in namespace]
    if not spans:
        return None
    first, last = min(start for start, _ in spans), max(end for _, end in spans)
    minutes = NO_CHURN_MEMORY_WINDOW.total_seconds() / 60
    return {MEMORY_METRIC: (f"last {minutes:g} minutes", last - NO_CHURN_MEMORY_WINDOW, last),
            CPU_METRIC: ("whole run", first, last)}


def main():
    args = parse_args()
    dump = os.path.expanduser(args.dump)
    job, readings, reading_span = read_readings(dump, args.container)
    cluster_id = job.get("clusterName")
    windows = steady_windows(job, reading_span, cluster_id) if cluster_id else None
    if not windows:
        sys.exit(f"no readings for the hosted cluster under test in {dump}")
    same_window = windows[MEMORY_METRIC] == windows[CPU_METRIC]

    name = os.path.basename(dump).removesuffix(".gz").removesuffix(".ndjson")
    what = f"{args.container} container" if args.container else "all containers"
    if same_window:
        description, start, end = windows[CPU_METRIC]
        print(f"{name}: {what}, {description} {start:%H:%M:%S}-{end:%H:%M:%S} UTC")
    else:
        print(f"{name}: {what}, no churn phase")
        print("The run never settles, so memory uses its last part, after memory levels off, and CPU uses the "
              "whole run, which is all create phase and so a conservative (high) estimate. "
              "Treat both as low confidence.")
    if not args.container:
        print(f"Copies count only if they have readings for at least {MIN_COPY_SHARE:.0%} of the window.")
    cpu_label = (f"CPU (cores), percentiles of {args.smooth:g}-minute averages" if args.smooth
                 else "CPU (cores)")
    for metric, label, scale, digits in ((MEMORY_METRIC, "memory (GiB)", BYTES_TO_GIB, 2),
                                         (CPU_METRIC, cpu_label, CPU_TO_CORES, 3)):
        description, start, end = windows[metric]
        if not same_window:
            label += f", {description} {start:%H:%M:%S}-{end:%H:%M:%S} UTC"
        smooth_minutes = args.smooth if metric == CPU_METRIC else 0
        copies = window_copies(readings[metric], cluster_id, start, end, scale)
        print(f"\n{label}")
        if not copies:
            print(f"  no readings for container {args.container!r}" if args.container else "  no readings")
        elif args.container:
            print_copies(copies, smooth_minutes, args.percentiles, digits)
        else:
            print_summary(copies, smooth_minutes, args.percentiles, digits, args.top)


if __name__ == "__main__":
    main()
