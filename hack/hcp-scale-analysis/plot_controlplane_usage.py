#!/usr/bin/env python3
"""Graph total hosted-control-plane CPU and memory over one kube-burner run.

Reads one raw kube-burner dump (.ndjson or .ndjson.gz), keeps the control-plane
pod metrics of the hosted cluster under test (the namespace containing
jobSummary.clusterName), adds up every container at each timestamp, and writes
these files to graphs/<dump name>/ next to this script (or to --output DIR):

  total-cpu.png     total control-plane CPU (cores) over time, as recorded and averaged
                    over 5 minutes (which hides bursts lasting only seconds)
  total-memory.png  total control-plane memory working set (GiB) over time
  summary.json      job settings, phase times, sampling interval, gaps and skipped duplicates

Both graphs share one time axis and shade the kube-burner phases recorded in
jobSummary, so a representative steady-state window can be picked by eye.
The output is meant to be committed, so it records the dump's file name but not
the cluster ID or namespace; those are only printed to the terminal.

Usage:
  python3 plot_controlplane_usage.py DUMP [--output DIR]

Requires Python 3.11 or newer and matplotlib.
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from hcputil import opener

CPU_METRIC = "podCPU-Controlplane"        # irate(container_cpu_usage_seconds_total) * 100: percent of one core
MEMORY_METRIC = "podMemory-Controlplane"  # container_memory_working_set_bytes
CPU_TO_CORES = 1 / 100
BYTES_TO_GIB = 1 / 1024 ** 3
GAP_FACTOR = 1.5  # a step this many times the usual sampling interval means samples are missing
SMOOTH_MINUTES = 5
PHASE_COLORS = ["tab:orange", "tab:green"]
GRAPHS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graphs")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", help="raw kube-burner dump (.ndjson or .ndjson.gz)")
    parser.add_argument("--output",
                        help="directory for the graphs and summary; created if missing "
                             "(default: graphs/<dump name> next to this script)")
    return parser.parse_args()


def parse_time(value):
    """Parse a kube-burner timestamp; None when it is missing or Go's zero time. Needs Python 3.11+,
    which parses the 2- and 9-digit fractions of a second some of these timestamps have."""
    if not value or value.startswith("0001-"):
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def minutes_since(origin, when):
    return (when - origin).total_seconds() / 60


def rounded_minutes(origin, when):
    # Adding 0.0 turns -0.0 into 0.0 when a job starts microseconds before its first sample.
    return round(minutes_since(origin, when), 1) + 0.0


def read_dump(path):
    """Read the dump once.

    Returns (job_summary, usage, containers, duplicates), where
    usage[namespace][metric][time] is the sum over that namespace's containers,
    containers[namespace] holds the (pod, container) pairs seen and duplicates[namespace]
    counts repeated samples that were skipped.
    """
    job = None
    usage = defaultdict(lambda: {CPU_METRIC: defaultdict(float), MEMORY_METRIC: defaultdict(float)})
    containers = defaultdict(set)
    seen = set()
    duplicates = Counter()
    with opener(path) as f:
        for line in f:
            maybe_job = job is None and '"jobSummary"' in line
            if not maybe_job and CPU_METRIC not in line and MEMORY_METRIC not in line:
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
            if metric not in (CPU_METRIC, MEMORY_METRIC):
                continue
            labels = record.get("labels", {})
            container = labels.get("container", "")
            if container in ("", "POD"):  # pod sandbox (pause) cgroups, not workload containers
                continue
            namespace = labels.get("namespace", "")
            pod = labels.get("pod", "")
            # Keyed by the parsed time, so the same instant written two ways (…:00Z, …:00.000Z) is one sample.
            when = parse_time(record["timestamp"])
            key = (metric, namespace, pod, container, when)
            # Some dumps (e.g. the 250- and 500-node runs) index the same samples twice.
            if key in seen:
                duplicates[namespace] += 1
                continue
            seen.add(key)
            usage[namespace][metric][when] += record["value"]
            containers[namespace].add((pod, container))
    return job or {}, usage, containers, duplicates


def total_series(usage, namespaces, metric, scale):
    """[(time, total)] in time order, summed over every container in the namespaces."""
    totals = defaultdict(float)
    for namespace in namespaces:
        for when, value in usage[namespace][metric].items():
            totals[when] += value
    return sorted((when, value * scale) for when, value in totals.items())


def interval_and_gaps(points):
    """The usual seconds between samples, and the (before, after) sample pairs around missing data."""
    times = [when for when, _ in points]
    steps = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:])]
    if not steps:
        return 0.0, []
    interval = statistics.median(steps)
    gaps = [(earlier, later) for earlier, later, step in zip(times, times[1:], steps)
            if step > GAP_FACTOR * interval]
    return interval, gaps


def job_phases(job):
    """[(name, start, end)] for the kube-burner phases recorded in the job summary."""
    start, end = parse_time(job.get("timestamp")), parse_time(job.get("endTimestamp"))
    churn_start = parse_time(job.get("churnStartTimestamp"))
    churn_end = parse_time(job.get("churnEndTimestamp"))
    if not (start and end):
        return []
    if churn_start and churn_end and start <= churn_start < churn_end:
        return [("create", start, churn_start), ("churn", churn_start, churn_end)]
    return [("job", start, end)]


def rolling_average(points, minutes):
    """Centered moving average of [(time, value)]: each point becomes the mean of the readings
    in the `minutes` around it, kept only where that whole window lies inside the run."""
    half = dt.timedelta(minutes=minutes) / 2
    first, last = points[0][0], points[-1][0]
    averaged = []
    for when, _ in points:
        if when - half < first or when + half > last:
            continue
        window = [value for other, value in points if when - half <= other < when + half]
        averaged.append((when, sum(window) / len(window)))
    return averaged


def line_points(points, origin, interval):
    """Plot coordinates (minutes, value), with a break wherever samples are missing."""
    xs, ys = [], []
    for i, (when, value) in enumerate(points):
        if i and (when - points[i - 1][0]).total_seconds() > GAP_FACTOR * interval:
            xs.append(minutes_since(origin, when))
            ys.append(float("nan"))  # break the line instead of drawing across missing samples
        xs.append(minutes_since(origin, when))
        ys.append(value)
    return xs, ys


def plot_total(path, points, origin, interval, phases, title, ylabel, color, smooth_minutes=None):
    fig, ax = plt.subplots(figsize=(12, 4.5))
    for (name, start, end), shade in zip(phases, PHASE_COLORS):
        ax.axvspan(minutes_since(origin, start), minutes_since(origin, end),
                   color=shade, alpha=0.12, label=f"kube-burner {name} phase")
    xs, ys = line_points(points, origin, interval)
    if smooth_minutes:
        ax.plot(xs, ys, color=color, lw=1.0, alpha=0.35,
                label="sum of all control-plane containers, as recorded")
        smooth_xs, smooth_ys = line_points(rolling_average(points, smooth_minutes), origin, interval)
        ax.plot(smooth_xs, smooth_ys, color=color, lw=2.2, label=f"same, averaged over {smooth_minutes} minutes")
    else:
        ax.plot(xs, ys, color=color, lw=1.5, label="sum of all control-plane containers")
    ax.set_title(title, fontsize=11)
    ax.set_xlabel(f"minutes since first sample ({origin:%Y-%m-%d %H:%M:%S} UTC)")
    ax.set_ylabel(ylabel)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=4 if smooth_minutes else 3,
              frameon=False, fontsize=9)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def describe(points, origin, interval, gaps):
    return {
        "first_sample": points[0][0].isoformat(),
        "last_sample": points[-1][0].isoformat(),
        "samples": len(points),
        "interval_seconds": interval,
        "gaps": [{"from_minute": rounded_minutes(origin, before),
                  "to_minute": rounded_minutes(origin, after)} for before, after in gaps],
    }


def main():
    args = parse_args()
    dump = os.path.expanduser(args.dump)
    name = os.path.basename(dump).removesuffix(".gz").removesuffix(".ndjson")
    output = os.path.expanduser(args.output) if args.output else os.path.join(GRAPHS_DIR, name)

    job, usage, containers, duplicates = read_dump(dump)
    cluster_id = job.get("clusterName")
    if not cluster_id:
        sys.exit(f"{dump} has no jobSummary with a clusterName, so the hosted cluster under test is unknown")
    namespaces = sorted(ns for ns in usage if cluster_id in ns)
    if not namespaces:
        sys.exit(f"no control-plane metrics for cluster {cluster_id!r} in {dump}; "
                 f"namespaces present: {', '.join(sorted(usage)) or 'none'}")

    cpu = total_series(usage, namespaces, CPU_METRIC, CPU_TO_CORES)
    memory = total_series(usage, namespaces, MEMORY_METRIC, BYTES_TO_GIB)
    if not cpu or not memory:
        sys.exit(f"{dump} is missing {CPU_METRIC} or {MEMORY_METRIC} samples for {', '.join(namespaces)}")

    origin = min(cpu[0][0], memory[0][0])
    phases = job_phases(job)
    seen = set().union(*(containers[ns] for ns in namespaces))
    pods = {pod for pod, _ in seen}
    config = job.get("jobConfig", {})
    subtitle = (f"{config.get('name', 'unknown job')} · {job.get('workerNodesCount', '?')} workers · "
                f"QPS {config.get('qps', '?')} · {config.get('jobIterations', '?')} iterations · "
                f"{len(pods)} pods seen")

    os.makedirs(output, exist_ok=True)
    cpu_interval, cpu_gaps = interval_and_gaps(cpu)
    memory_interval, memory_gaps = interval_and_gaps(memory)
    plot_total(os.path.join(output, "total-cpu.png"), cpu, origin, cpu_interval, phases,
               f"{name}: total control-plane CPU\n{subtitle}", "CPU (cores)", "tab:blue",
               smooth_minutes=SMOOTH_MINUTES)
    plot_total(os.path.join(output, "total-memory.png"), memory, origin, memory_interval, phases,
               f"{name}: total control-plane memory\n{subtitle}", "memory working set (GiB)", "tab:red")

    summary = {
        "dump": os.path.basename(dump),
        "control_plane_namespaces": len(namespaces),
        "job": {
            "name": config.get("name"),
            "worker_nodes": job.get("workerNodesCount"),
            "iterations": config.get("jobIterations"),
            "qps": config.get("qps"),
            "burst": config.get("burst"),
            "achieved_qps": job.get("achievedQps"),
            "passed": job.get("passed"),
        },
        "phases": [{"name": phase, "start": start.isoformat(), "end": end.isoformat(),
                    "start_minute": rounded_minutes(origin, start),
                    "end_minute": rounded_minutes(origin, end)} for phase, start, end in phases],
        "pods_seen": len(pods),
        "containers_seen": len(seen),
        "duplicate_samples_skipped": sum(duplicates[ns] for ns in namespaces),
        "cpu": describe(cpu, origin, cpu_interval, cpu_gaps),
        "memory": describe(memory, origin, memory_interval, memory_gaps),
    }
    with open(os.path.join(output, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    # Printed rather than saved so committed output never publishes production cluster identifiers.
    print(f"cluster {cluster_id or 'unknown'}: {', '.join(namespaces)}")
    print(json.dumps(summary, indent=2))
    print(f"wrote {output}/total-cpu.png, total-memory.png and summary.json")


if __name__ == "__main__":
    main()
