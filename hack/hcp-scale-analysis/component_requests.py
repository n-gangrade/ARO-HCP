#!/usr/bin/env python3
"""What one copy of each control-plane component needs, for each test run.

Reads the data file written by combine_usage.py (container-usage.json next to this script by
default) and, for each run, adds up each component's containers into what one copy (pod) of the
component needs. Kubernetes reserves CPU and memory per container, so a pod's reservation is the
sum of its containers'. By default each container's reservation is the p95 of all its copies'
readings put together (all_copies), as every copy gets the same reservation and which copy is
busiest is random. --way and --percentile pick another of the numbers in the file instead.

For each run it prints one row per component, biggest first by memory: how many copies it runs,
and the CPU (cores) and memory (GiB) of one copy and of all its copies together. Then the totals
for the whole control plane, next to what it actually used at the same percentile (the run's
total), which is lower, as containers don't all peak at the same moment.

A component runs as many copies as the most any of its containers has: a copy left out of one
container's numbers for having too few readings, such as an idle copy (the CPU metric drops
readings of exactly zero), still runs that container. A container without any CPU readings used
no CPU.

Usage:
  python3 component_requests.py [RUN ...] [--data FILE] [--way WAY] [--percentile P]

RUN is a run's name, such as 120-node-aro-hcp, or its number of worker nodes, such as 120;
without any, it prints every run.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(HERE, "container-usage.json")
WAYS = ("all_copies", "busiest_copy", "average_copy")
METRICS = ("cpu_cores", "memory_gib")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="*", metavar="RUN",
                        help="runs to show, by name or number of worker nodes (default: all)")
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="data file from combine_usage.py (default: container-usage.json next to this script)")
    parser.add_argument("--way", choices=WAYS, default="all_copies",
                        help="which of each container's numbers to use (default: all_copies)")
    parser.add_argument("--percentile", default="p95", metavar="P",
                        help="which percentile to use, one of those in the data file (default: p95)")
    return parser.parse_args()


def component_requests(run, way="all_copies", percentile="p95"):
    """{component: {"copies": n, "cpu_cores": x, "memory_gib": y}} for one run of the data file,
    where x and y are what one copy needs: its containers' `way` `percentile`, added up."""
    components = {}
    for container in run["containers"]:
        component = components.setdefault(container["component"],
                                          {"copies": 0, "cpu_cores": 0.0, "memory_gib": 0.0})
        for metric in METRICS:
            numbers = container.get(metric)
            if numbers:
                component[metric] += numbers[way][percentile]
                component["copies"] = max(component["copies"], numbers["copies"])
    return components


def print_run(run, way, percentile):
    components = component_requests(run, way, percentile)
    rows = sorted(components.items(), key=lambda item: item[1]["memory_gib"] * item[1]["copies"], reverse=True)
    total_label = f"total, {len(components)} components"
    width = max(len(total_label), *(len(name) for name in components))
    print(f"{run['run']}: {run['worker_nodes']} worker nodes, trust {run['trust']}; {percentile} of {way}")
    print(f"  {'':{width}}  {'':6}  {'one copy':^17}  {'all copies':^17}")
    print(f"  {'component':{width}}  copies  {'cores':>8} {'GiB':>8}  {'cores':>8} {'GiB':>8}")
    for name, needs in rows:
        print(f"  {name:{width}}  {needs['copies']:6d}  {needs['cpu_cores']:8.3f} {needs['memory_gib']:8.3f}"
              f"  {needs['cpu_cores'] * needs['copies']:8.3f} {needs['memory_gib'] * needs['copies']:8.3f}")
    cpu = sum(needs["cpu_cores"] * needs["copies"] for needs in components.values())
    memory = sum(needs["memory_gib"] * needs["copies"] for needs in components.values())
    print(f"  {total_label:{width}}  {'':6}  {'':8} {'':8}  {cpu:8.3f} {memory:8.3f}")
    used = run["total"]
    print(f"  actually used by the whole control plane ({percentile}): {used['cpu_cores'][percentile]:.3f} cores, "
          f"{used['memory_gib'][percentile]:.3f} GiB")


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

    def matches(run, wanted):
        return wanted in (run["run"], str(run["worker_nodes"]))

    unknown = [wanted for wanted in args.runs if not any(matches(run, wanted) for run in data["runs"])]
    if unknown:
        sys.exit(f"no run matches {', '.join(unknown)}; the runs are: "
                 + ", ".join(run["run"] for run in data["runs"]))
    runs = [run for run in data["runs"] if not args.runs or any(matches(run, wanted) for wanted in args.runs)]
    for i, run in enumerate(runs):
        if i:
            print()
        print_run(run, args.way, args.percentile)


if __name__ == "__main__":
    main()
