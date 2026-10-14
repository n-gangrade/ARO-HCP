#!/usr/bin/env python3
"""The pods one hosted cluster of each t-shirt size runs, with each copy's CPU, memory and SWIFT NICs.

Reads the data file written by combine_usage.py (container-usage.json next to this script by default)
and writes size-pods.json next to this script: for each of production's t-shirt sizes, every pod
(component) a hosted cluster runs, how many copies, and what one copy needs. It's the input for
simulating how a customer's clusters pack onto management-cluster machines.

Sizes: production's t-shirt sizes (SIZE_CLASSES), which HyperShift picks by a hosted cluster's total
node count, infra nodes as well as workers. Each size uses the largest test cluster within its range,
preferring runs whose trust is good, as the largest is closest to the size's biggest clusters. A size
without a test run uses the next larger size's run and is marked as borrowed. CHOSEN_RUNS picks a
specific run for a size instead.

Pods: one copy of a component needs its containers' p95 of all copies' readings, added up (see
component_requests.py), so the numbers are what the pods use, not what production reserves for them
today. Every cluster also gets a router, measured in the largest test run that had one, as production
clusters run one; each router copy needs a SWIFT NIC (the network card a pod uses to reach the
customer's network). The two login components the scheduling simulator adds (oauth-openshift and
openshift-oauth-apiserver) are left out, as production's clusters don't run them. A component with more
than one copy runs one copy per availability zone, as production requires (one_per_zone).

Usage:
  python3 size_pods.py [--data FILE] [--output FILE]
"""
import argparse
import json
import os
import sys

from component_requests import DEFAULT_DATA, component_requests

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "size-pods.json")
# Production's t-shirt sizes: (name, lowest and highest total node count), from the ClusterSizingConfiguration in
# hypershiftoperator/deploy/templates/cluster.clustersizingconfiguration.yaml. None means no upper limit.
SIZE_CLASSES = [("small", 0, 60), ("medium", 61, 120), ("large", 121, 252), ("xlarge", 253, 360),
                ("xxlarge", 361, None)]
# The test run to use for a size, instead of the largest one within its range, e.g. {"medium": "120-node-aro-hcp"}.
CHOSEN_RUNS = {}
ROUTER = "router"
SWIFT_NICS_PER_COPY = {ROUTER: 1}
DECIMALS = 3
FIELDS = {
    "nodes_from": "the size's lowest total node count, infra nodes as well as workers",
    "nodes_to": "the size's highest total node count; null means no upper limit",
    "run": "the test run the size's pods come from",
    "run_total_nodes": "that run's total node count",
    "borrowed": "true when no test run falls within the size, so it uses the next larger size's run",
    "router_from": "the test run the router's numbers come from, if not the size's own run",
    "copies": "how many copies (pods) of the component one cluster runs",
    "cpu_cores": "CPU one copy needs, in cores",
    "memory_gib": "memory one copy needs, in GiB",
    "swift_nics": "SWIFT NICs one copy needs",
    "one_per_zone": "whether the copies run in different availability zones, one per zone",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="data file from combine_usage.py (default: container-usage.json next to this script)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="file to write (default: size-pods.json next to this script)")
    return parser.parse_args()


def within(run, low, high):
    return run["total_nodes"] >= low and (high is None or run["total_nodes"] <= high)


def largest_run(runs):
    """The run with the most nodes, preferring runs whose trust is good, or None."""
    trusted = [run for run in runs if run["trust"] == "good"] or runs
    return max(trusted, key=lambda run: (run["total_nodes"], run["run"])) if trusted else None


def runs_for_sizes(runs):
    """{size: (run, borrowed)}, borrowing the next larger size's run for a size without one."""
    by_name = {run["run"]: run for run in runs}
    unknown = sorted(set(CHOSEN_RUNS.values()) - set(by_name))
    if unknown:
        sys.exit(f"CHOSEN_RUNS names runs that aren't in the data file: {', '.join(unknown)}")
    chosen = {name: (by_name[CHOSEN_RUNS[name]] if name in CHOSEN_RUNS
                     else largest_run([run for run in runs if within(run, low, high)]))
              for name, low, high in SIZE_CLASSES}
    out, borrowed_from = {}, None
    for name, _, _ in reversed(SIZE_CLASSES):
        if chosen[name] is not None:
            borrowed_from = chosen[name]
            out[name] = (chosen[name], False)
        elif borrowed_from is not None:
            out[name] = (borrowed_from, True)
        else:
            sys.exit(f"no test run is big enough for the {name} size or any larger one")
    return out


def router_numbers(runs):
    """(the run, what one router copy needs) from the largest run that had a router, or (None, None)."""
    with_router = [run for run in runs if ROUTER in component_requests(run)]
    if not with_router:
        return None, None
    run = max(with_router, key=lambda run: run["total_nodes"])
    return run, component_requests(run)[ROUTER]


def pods_for(run, router_run, router):
    """[pod], biggest first by memory, and the router's source run if it isn't this run."""
    components = component_requests(run)
    router_from = None
    if ROUTER not in components and router is not None:
        components[ROUTER] = router
        router_from = router_run["run"]
    pods = [{
        "component": name,
        "copies": needs["copies"],
        "cpu_cores": round(needs["cpu_cores"], DECIMALS),
        "memory_gib": round(needs["memory_gib"], DECIMALS),
        "swift_nics": SWIFT_NICS_PER_COPY.get(name, 0),
        "one_per_zone": needs["copies"] > 1,
    } for name, needs in components.items()]
    pods.sort(key=lambda pod: (-pod["memory_gib"] * pod["copies"], pod["component"]))
    return pods, router_from


def to_json(data):
    """Like json.dumps(data, indent=2), but with each pod on one line, so the file reads like a table."""
    lines = {}

    def placeholder(pod):
        key = f"@pod-{len(lines)}@"
        lines[key] = json.dumps(pod)
        return key

    sizes = [{**size, "pods": [placeholder(pod) for pod in size["pods"]]} for size in data["sizes"]]
    text = json.dumps({**data, "sizes": sizes}, indent=2)
    for key, line in lines.items():
        text = text.replace(json.dumps(key), line, 1)
    return text + "\n"


def main():
    args = parse_args()
    path = os.path.expanduser(args.data)
    try:
        with open(path) as f:
            runs = json.load(f)["runs"]
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"couldn't read {path}: {error}")
    missing = [run["run"] for run in runs if not run.get("total_nodes")]
    if missing:
        sys.exit(f"{path} has no total_nodes for {', '.join(missing)}; rerun combine_usage.py")

    router_run, router = router_numbers(runs)
    if router is None:
        print("warning: no test run had a router, so the clusters get none", file=sys.stderr)
    choices = runs_for_sizes(runs)
    sizes = []
    for name, low, high in SIZE_CLASSES:
        run, borrowed = choices[name]
        pods, router_from = pods_for(run, router_run, router)
        sizes.append({
            "size": name,
            "nodes_from": low,
            "nodes_to": high,
            "run": run["run"],
            "run_total_nodes": run["total_nodes"],
            "trust": run["trust"],
            "borrowed": borrowed,
            "router_from": router_from,
            "totals": {
                "pods": sum(pod["copies"] for pod in pods),
                "cpu_cores": round(sum(pod["copies"] * pod["cpu_cores"] for pod in pods), DECIMALS),
                "memory_gib": round(sum(pod["copies"] * pod["memory_gib"] for pod in pods), DECIMALS),
                "swift_nics": sum(pod["copies"] * pod["swift_nics"] for pod in pods),
            },
            "pods": pods,
        })
    output = {
        "description": "The pods one hosted cluster of each t-shirt size runs, written by "
                       "hack/hcp-scale-analysis/size_pods.py from container-usage.json: what one copy of each "
                       "component needs, the sum of its containers' p95 of all copies' readings during the churn phase.",
        "fields": FIELDS,
        "sizes": sizes,
    }
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(to_json(output))
    print(f"wrote {target}", file=sys.stderr)
    print(f"{'size':>8} {'nodes':>9}  {'from run':36} {'trust':>8} {'pods':>5} {'cores':>6} {'GiB':>6} {'NICs':>5}")
    for size in sizes:
        nodes = f"{size['nodes_from']}-{size['nodes_to'] if size['nodes_to'] is not None else ''}"
        source = size["run"] + (" (borrowed)" if size["borrowed"] else "")
        totals = size["totals"]
        print(f"{size['size']:>8} {nodes:>9}  {source:36} {size['trust']:>8} {totals['pods']:5d} "
              f"{totals['cpu_cores']:6.1f} {totals['memory_gib']:6.1f} {totals['swift_nics']:5d}")


if __name__ == "__main__":
    main()
