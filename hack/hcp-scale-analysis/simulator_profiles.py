#!/usr/bin/env python3
"""Write the per-component numbers in the scheduling simulator's input format.

Reads the data file written by combine_usage.py (container-usage.json next to this script by
default), works out what one copy of each component needs for each size with
component_requests.py, and writes them as a profiles file for the scheduling simulator in
../scheduling-simulator (simulator-profiles.json next to this script by default). The simulator
reads it instead of its own sample_inputs/profiles.json when the ARO_HCP_SIM_PROFILES
environment variable is set to its path.

The simulator's own file holds every reading of each component, and it takes a percentile of
them as it runs. This file holds one number per component instead: what one copy needs, already
worked out here (by default the p95 of all its copies' readings put together). So the
simulator's percentile setting makes no difference with this file, while its multiplier still
does.

Each size comes from one test run: the number its name starts with, such as 500 for
500-node-aro-hcp (which ran with 498 worker nodes). When more than one run tests the same size,
PREFERRED_RUNS says which one to use. The router, which the simulator adds to every size, comes
from the largest run that had one: only the 49-node run did.

Usage:
  python3 simulator_profiles.py [--data FILE] [--output FILE] [--way WAY] [--percentile P]
"""
import argparse
import json
import os
import sys
from collections import defaultdict

from component_requests import DEFAULT_DATA, WAYS, component_requests

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "simulator-profiles.json")
# Which run to use for a size that more than one run tests: every other run used 20 QPS.
PREFERRED_RUNS = {"12": "12-nodes-20-client-qps-rate-aro-hcp"}
ROUTER = "router"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="data file from combine_usage.py (default: container-usage.json next to this script)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="profiles file to write (default: simulator-profiles.json next to this script)")
    parser.add_argument("--way", choices=WAYS, default="all_copies",
                        help="which of each container's numbers to use (default: all_copies)")
    parser.add_argument("--percentile", default="p95", metavar="P",
                        help="which percentile to use, one of those in the data file (default: p95)")
    return parser.parse_args()


def size_of(run):
    """The size a run tests: the number its name starts with, or else its number of worker nodes."""
    first = run["run"].split("-")[0]
    return first if first.isdigit() else str(run["worker_nodes"])


def runs_by_size(runs):
    """{size: run}, smallest first, with PREFERRED_RUNS choosing among runs of the same size."""
    candidates = defaultdict(list)
    for run in runs:
        candidates[size_of(run)].append(run)
    chosen = {}
    for size, size_runs in candidates.items():
        if len(size_runs) > 1:
            size_runs = [run for run in size_runs if run["run"] == PREFERRED_RUNS.get(size)]
            if not size_runs:
                sys.exit(f"more than one run tests {size} nodes ({', '.join(run['run'] for run in candidates[size])}); "
                         f"say which one to use in PREFERRED_RUNS")
        chosen[size] = size_runs[0]
    return dict(sorted(chosen.items(), key=lambda item: int(item[0])))


def profile(needs):
    """One copy's needs in the simulator's format: a single "reading" in millicores and MiB."""
    return {"cpu_mc": [round(needs["cpu_cores"] * 1000, 1)], "mem_mib": [round(needs["memory_gib"] * 1024, 1)]}


def to_json(data):
    """Like json.dumps(data, indent=2), but with each component on one line, so the file stays small."""
    lines = {}

    def placeholder(value):
        key = f"@line-{len(lines)}@"
        lines[key] = json.dumps(value)
        return key

    compact = {**data, "profiles": {size: {name: placeholder(p) for name, p in components.items()}
                                    for size, components in data["profiles"].items()},
               "router_profile": placeholder(data["router_profile"])}
    text = json.dumps(compact, indent=2)
    for key, line in lines.items():
        text = text.replace(json.dumps(key), line, 1)
    return text + "\n"


def main():
    args = parse_args()
    path = os.path.expanduser(args.data)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as error:
        sys.exit(f"couldn't read {path}: {error}")
    percentiles = [f"p{p:g}" for p in data["settings"]["percentiles"]]
    if args.percentile not in percentiles:
        sys.exit(f"--percentile must be one of the percentiles in {path}: {', '.join(percentiles)}")

    runs = runs_by_size(data["runs"])
    profiles, router_size, router = {}, None, None
    for size, run in runs.items():
        components = component_requests(run, args.way, args.percentile)
        profiles[size] = {name: {**profile(needs), "replicas": needs["copies"]} for name, needs in components.items()}
        if ROUTER in components:
            router_size, router = size, components[ROUTER]
    if router is None:
        print("warning: no run had a router, so the router the simulator adds to every size uses nothing",
              file=sys.stderr)
    output = {
        "description": f"Scheduling simulator profiles written by hack/hcp-scale-analysis/simulator_profiles.py from "
                       f"container-usage.json: what one copy of each component needs, the sum of its containers' "
                       f"{args.percentile} of {args.way} during the churn phase, as a single value, so the "
                       f"simulator's percentile setting makes no difference. See each run's trust note in "
                       f"container-usage.json.",
        "settings": {"way": args.way, "percentile": args.percentile, "router_from": router_size},
        "runs": {size: run["run"] for size, run in runs.items()},
        "sizes": list(profiles),
        "profiles": profiles,
        "router_profile": profile(router) if router else {"cpu_mc": [], "mem_mib": []},
    }
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(to_json(output))
    print(f"wrote {target}: sizes {', '.join(profiles)}", file=sys.stderr)


if __name__ == "__main__":
    main()
