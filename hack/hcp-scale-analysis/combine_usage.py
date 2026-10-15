#!/usr/bin/env python3
"""Combine per-container control-plane usage from many kube-burner runs into one JSON file.

For each raw kube-burner dump it works out the same numbers as container_usage.py's summary
(same windows, readings as recorded, copies being replaced left out) for memory and CPU,
and writes them with each run's details to one file, container-usage.json next to this script
by default. That file is the input for comparing sizes and for the PM page: rerun this
whenever new test runs arrive.

For each run: its worker nodes and total nodes, OpenShift version, date, whether it had a churn
phase, the window used for memory and for CPU, the share of expected readings that exist, and the
whole control plane's total (p50 and p95 of the sum of every container at each moment, with each
copy's missing readings filled in from its neighbors). For each container, separately for
memory (GiB) and CPU (cores): how many copies count, the p50 and p95 of its busiest copy, of
its average copy and of all its copies' readings put together, and the share of expected
readings that exist.

Each run also gets what Kubernetes reserved for its control plane: its containers' resource
requests, which the test recorded once, at the end of the run. For each container, how many copies
it had then and what each copy reserved; for the run, the total. The reservations come from
kube-state-metrics, whose series the monitoring can keep more than one copy of (the 49-node dump
keeps one per Prometheus replica), which multiplies their recorded sums. The management cluster's
node roles, also from kube-state-metrics but recorded without adding anything up, show how many
copies it kept, and the reservations are divided by that.
The file is meant to be committed, so it leaves out cluster IDs.

Each run also gets its trust note, copied from a hand-written notes file (run-notes.json next
to this script by default): a trust level (good, caveats or low) and why. Runs without a note
get a warning, as a reminder to look at a new run's graphs and write one.

Usage:
  python3 combine_usage.py DUMP [DUMP ...] [--output FILE] [--notes FILE]

Requires Python 3.11 or newer, numpy, and matplotlib through plot_controlplane_usage.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict

from container_usage import (MIN_COPY_SHARE, NO_CHURN_MEMORY_WINDOW, component_of, read_readings,
                             steady_windows, summarize, total_usage, window_copies)
from hcputil import opener
from plot_controlplane_usage import BYTES_TO_GIB, CPU_METRIC, CPU_TO_CORES, MEMORY_METRIC, parse_time

PERCENTILES = [50.0, 95.0]
DECIMALS = 3  # 0.001 GiB is about 1 MiB, and 0.001 cores is 1 millicore
# (name in the file, metric, conversion to GiB or cores)
METRICS = [("memory_gib", MEMORY_METRIC, BYTES_TO_GIB),
           ("cpu_cores", CPU_METRIC, CPU_TO_CORES)]
# kube_pod_container_resource_requests from kube-state-metrics, added up per copy (pod) and container: what
# Kubernetes reserved
MEMORY_RESERVED_METRIC = "podMemoryReq"  # bytes
CPU_RESERVED_METRIC = "podCPUReq"        # cores
RESERVATION_METRICS = [("memory_gib", MEMORY_RESERVED_METRIC, BYTES_TO_GIB),
                       ("cpu_cores", CPU_RESERVED_METRIC, 1)]
# kube_node_role from kube-state-metrics, as recorded, without adding anything up, so it shows how many copies
# of each kube-state-metrics series the monitoring kept
NODE_ROLES_METRIC = "mgmtNodeRoles"
# A container that reserves nothing counts as running at the end if it has a reading this close to it.
RUNNING_AT_END = dt.timedelta(minutes=2)
FIELDS = {
    "total_nodes": "the test cluster's nodes, infra nodes as well as workers; HyperShift picks a cluster's t-shirt "
                   "size by this count",
    "trust": "how much to trust the run, from the hand-written notes file: good, caveats or low",
    "notes": "why, from the hand-written notes file",
    "memory_gib": "memory working set in GiB; percentiles are of the readings as recorded",
    "cpu_cores": "CPU in cores; percentiles are of the readings as recorded",
    "total": "the whole control plane's actual total over the window: p50 and p95 of the sum of every "
             "container's readings at each moment. Use this for the cluster's p95, rather than adding up "
             "containers' p95s. filled_in is the share of the summed readings that were missing and filled in "
             "along a straight line from the copy's neighboring readings",
    "copies": "how many copies (pods) count: those with readings for at least half the window, which leaves out "
              "copies being replaced; it can differ between memory and CPU",
    "busiest_copy": "the highest p50 and the highest p95 among the copies, each taken separately, so they can come "
                    "from different copies; which copy is busiest depends on where clients happened to connect",
    "average_copy": "the p50 and p95 averaged over the copies; copies x p50, added up over all containers, closely "
                    "matches the cluster's typical total use. p95s don't add up like that: containers don't peak at "
                    "the same moment, so the sum overstates the cluster's p95",
    "all_copies": "the p50 and p95 of all the copies' readings put together. Every copy gets the same reservation, "
                  "and which copy is busiest is random, as each client sticks to the copy it first connected to, so "
                  "reservations come from this, treating the copies as interchangeable",
    "readings_present": "share of the expected readings that exist, over the copies that count (for a run, over all "
                        "its containers); below 1 means missing data or, for CPU, an idle container, as the CPU "
                        "metric drops readings of exactly zero",
    "reservations": "what Kubernetes reserved for the control plane (its containers' resource requests), which the "
                    "test recorded once, at the end of the run (time). total adds up every copy of the containers "
                    "listed; left_out lists containers with a reservation but no usage counted in the window, such "
                    "as a finished job's, which aren't in the total. counted_times is how many copies of each "
                    "kube-state-metrics series, where reservations come from, the monitoring kept then (2 when it "
                    "kept both Prometheus replicas' copies, which doubles the recorded reservations); the numbers "
                    "here are divided by it",
    "reserved": "what Kubernetes reserved for each copy (its resource requests), as memory in GiB and CPU in cores, "
                "and how many copies the container had, at the end of the run. 0 means it reserves nothing: "
                "Kubernetes records nothing for such a container, although it was running; null means none of its "
                "copies were left by then",
}
TRUST_LEVELS = ("good", "caveats", "low")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "container-usage.json")
DEFAULT_NOTES = os.path.join(HERE, "run-notes.json")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dumps", nargs="+", metavar="DUMP", help="raw kube-burner dumps (.ndjson or .ndjson.gz)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="JSON file to write (default: container-usage.json next to this script)")
    parser.add_argument("--notes", default=DEFAULT_NOTES,
                        help="hand-written trust notes (default: run-notes.json next to this script)")
    return parser.parse_args()


def load_notes(path):
    """(the note for all runs, {run name: {"trust": ..., "notes": ...}}) from the notes file."""
    if not os.path.exists(path):
        print(f"warning: there's no notes file at {path}, so no run gets a trust note", file=sys.stderr)
        return None, {}
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError as error:
        sys.exit(f"couldn't read {path}: {error}")
    notes = data.get("runs", {})
    for run, note in notes.items():
        if not isinstance(note, dict) or note.get("trust") not in TRUST_LEVELS:
            sys.exit(f"{path}: the trust for {run} must be one of: {', '.join(TRUST_LEVELS)}")
    return data.get("all_runs"), notes


def with_note(run, notes):
    """The run with its trust note, placed right after its name and size."""
    note = notes.get(run["run"])
    if note is None:
        print(f"warning: {run['run']} has no trust note; look at its graphs and add one to the notes file",
              file=sys.stderr)
        note = {}
    head = {key: run[key] for key in ("run", "worker_nodes", "total_nodes")}
    rest = {key: value for key, value in run.items() if key not in head}
    return {**head, "trust": note.get("trust"), "notes": note.get("notes"), **rest}


def by_percentile(values):
    return {f"p{p:g}": None if v is None else round(float(v), DECIMALS) for p, v in zip(PERCENTILES, values)}


def read_reservations(path, cluster_id):
    """(moment, counted_times, reserved), where reserved[(namespace, pod, container)] maps "memory_gib" and
    "cpu_cores" to what Kubernetes reserved for that copy of one of the tested cluster's containers at `moment`,
    the last moment the test recorded them; a copy has no entry for what it reserves nothing of. counted_times
    is how many copies of each kube-state-metrics series the monitoring kept then: each recorded reservation
    counts that many copies, so the numbers are divided by it. None when there are none for the tested cluster."""
    names = {metric: (name, scale) for name, metric, scale in RESERVATION_METRICS}
    recorded = defaultdict(dict)  # {time: {(namespace, pod, container, name): value}}
    roles = defaultdict(set)      # {(time, node, role): {label set}}, so repeated records count once
    with opener(path) as f:
        for line in f:
            if (MEMORY_RESERVED_METRIC not in line and CPU_RESERVED_METRIC not in line
                    and NODE_ROLES_METRIC not in line):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            metric, labels = record.get("metricName"), record.get("labels", {})
            when = parse_time(record.get("timestamp"))
            if metric == NODE_ROLES_METRIC:
                roles[(when, labels.get("node"), labels.get("role"))].add(frozenset(labels.items()))
                continue
            container = labels.get("container", "")
            # "" and "POD" are pod sandbox (pause) cgroups, not workload containers.
            if metric in names and cluster_id in labels.get("namespace", "") and container not in ("", "POD"):
                name, scale = names[metric]
                recorded[when][(labels["namespace"], labels.get("pod", ""), container, name)] = record["value"] * scale
    if not recorded:
        return None
    moment = max(recorded)
    if roles:
        nearest = min({when for when, _, _ in roles}, key=lambda when: abs(when - moment))
        kept = {len(label_sets) for (when, _, _), label_sets in roles.items() if when == nearest}
        if len(kept) > 1:
            sys.exit(f"{path}: the monitoring kept {' or '.join(map(str, sorted(kept)))} copies of different series, "
                     "so the reservations can't be corrected")
        counted_times = kept.pop()
    else:
        print(f"warning: {path} has no {NODE_ROLES_METRIC} records, so whether the monitoring kept more than one "
              "copy of each series is unknown; assuming one", file=sys.stderr)
        counted_times = 1
    reserved = defaultdict(dict)
    for (namespace, pod, container, name), value in recorded[moment].items():
        reserved[(namespace, pod, container)][name] = value / counted_times
    return moment, counted_times, reserved


def per_copy(copies, run_name, component, container):
    """{"copies", "memory_gib", "cpu_cores"}: one container's copies and what each reserves. Copies reserve the
    same, unless one is being replaced by a copy that reserves something else; then it's the largest."""
    result = {"copies": len(copies)}
    for name, _, _ in RESERVATION_METRICS:
        values = [copy.get(name, 0.0) for copy in copies]
        if len(set(values)) > 1:
            print(f"warning: {run_name}: copies of {component} / {container} reserve different {name}; "
                  "using the largest", file=sys.stderr)
        result[name] = round(max(values), DECIMALS)
    return result


def add_reservations(containers, reservations, readings, cluster_id, run_name):
    """Add what each container reserved to `containers` ({(component, container): its numbers}), and return
    the run's reservations as stored in the file."""
    if reservations is None:
        print(f"warning: {run_name} has no reservations for the hosted cluster under test", file=sys.stderr)
        for numbers in containers.values():
            numbers["reserved"] = None
        return None
    moment, counted_times, reserved = reservations
    groups = defaultdict(list)  # {(component, container): [each copy's reservations]}
    for (_, pod, container), copy in reserved.items():
        groups[(component_of(pod), container)].append(copy)
    # Kubernetes records nothing for a container that reserves nothing, so a container without a record
    # reserves nothing if a copy of it was still running then, which its readings show.
    running = defaultdict(set)
    for metric_readings in readings.values():
        for (namespace, pod, container), copy in metric_readings.items():
            if cluster_id in namespace and copy and max(copy) >= moment - RUNNING_AT_END:
                running[(component_of(pod), container)].add((namespace, pod))
    total = {name: 0.0 for name, _, _ in RESERVATION_METRICS}
    for (component, container), numbers in containers.items():
        copies = groups.get((component, container))
        if copies:
            numbers["reserved"] = per_copy(copies, run_name, component, container)
            for name in total:
                total[name] += sum(copy.get(name, 0.0) for copy in copies)
        elif running[(component, container)]:
            numbers["reserved"] = {"copies": len(running[(component, container)]), **dict.fromkeys(total, 0.0)}
        else:
            numbers["reserved"] = None
    left_out = [{"component": component, "container": container, **per_copy(copies, run_name, component, container)}
                for (component, container), copies in sorted(groups.items())
                if (component, container) not in containers]
    return {"time": moment.isoformat(), "counted_times": counted_times,
            "total": {name: round(value, DECIMALS) for name, value in total.items()}, "left_out": left_out}


def summarize_run(path):
    """The run's details and its per-container numbers, as stored in the file."""
    job, readings, reading_span = read_readings(path)
    cluster_id = job.get("clusterName")
    if not cluster_id:
        sys.exit(f"{path} has no jobSummary with a clusterName, so the hosted cluster under test is unknown")
    windows = steady_windows(job, reading_span, cluster_id)
    if not windows:
        sys.exit(f"no readings for the hosted cluster under test in {path}")
    run = {
        "run": os.path.basename(path).removesuffix(".gz").removesuffix(".ndjson"),
        "worker_nodes": job.get("workerNodesCount"),
        "total_nodes": job.get("totalNodes") or job.get("workerNodesCount"),
        "ocp_version": job.get("ocpVersion"),
        "date": (job.get("timestamp") or "")[:10] or None,
        "churn_phase": windows[CPU_METRIC][0] == "churn phase",
        "windows": {},
        "readings_present": {},
        "total": {},
    }
    containers = defaultdict(dict)
    for name, metric, scale in METRICS:
        description, start, end = windows[metric]
        run["windows"][name] = {"description": description, "start": start.isoformat(), "end": end.isoformat()}
        copies_in_window = window_copies(readings[metric], cluster_id, start, end, scale)
        rows = summarize(copies_in_window, PERCENTILES)
        counted = sum(copies for _, _, copies, *_ in rows)
        present = sum(share * copies for _, _, copies, _, _, _, share in rows)
        run["readings_present"][name] = round(present / counted, DECIMALS) if counted else None
        total, filled = total_usage(copies_in_window, PERCENTILES)
        run["total"][name] = {**by_percentile(total),
                              "filled_in": None if filled is None else round(filled, DECIMALS)}
        for component, container, copies, busiest, average, all_copies, share in rows:
            containers[(component, container)][name] = {
                "copies": copies, "busiest_copy": by_percentile(busiest),
                "average_copy": by_percentile(average), "all_copies": by_percentile(all_copies),
                "readings_present": round(share, DECIMALS)}
    run["reservations"] = add_reservations(containers, read_reservations(path, cluster_id), readings, cluster_id,
                                           run["run"])
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
    all_runs_note, notes = load_notes(os.path.expanduser(args.notes))
    runs = []
    for path in args.dumps:
        started = time.monotonic()
        run = summarize_run(os.path.expanduser(path))
        print(f"{run['run']}: {len(run['containers'])} containers ({time.monotonic() - started:.0f} s)",
              file=sys.stderr)
        runs.append(with_note(run, notes))
    for name in sorted(set(notes) - {run["run"] for run in runs}):
        print(f"note: {name} has a trust note but wasn't one of the runs given", file=sys.stderr)
    runs.sort(key=lambda run: (run["worker_nodes"] or 0, run["run"]))
    data = {
        "description": "Per-container control-plane usage of each kube-burner run, and what Kubernetes reserved for "
                       "it, written by combine_usage.py",
        "settings": {
            "percentiles": PERCENTILES,
            "min_copy_share": MIN_COPY_SHARE,
            "no_churn_memory_window_minutes": NO_CHURN_MEMORY_WINDOW.total_seconds() / 60,
        },
        "fields": FIELDS,
        "notes_for_all_runs": all_runs_note,
        "runs": runs,
    }
    output = os.path.expanduser(args.output)
    with open(output, "w") as f:
        f.write(to_json(data))
    print(f"wrote {output}: {len(runs)} runs", file=sys.stderr)


if __name__ == "__main__":
    main()
