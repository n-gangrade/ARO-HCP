#!/usr/bin/env python3
"""Build the PM page: one self-contained HTML file that works out the cores customers' hosted clusters need.

Reads cores-per-cluster.json (from cores_per_cluster.py) and size-pods.json (for the test run its reservations come
from), puts them into pm-page-template.html and writes pm-page.html, all next to this script by default. The page
needs no server or network connection: open it in a browser, or send the file to someone. Rerun this whenever those
files change.

On the page, PMs add a row for each customer, region, cluster size and number of clusters, and get the cores to
request per region and per customer: each cluster's own share of a full management cluster's control-plane machines
and its amortized share of what's there for all the clusters. The management cluster's settings are on the page, with
cores-per-cluster.json's as defaults.

Usage:
  python3 build_pm_page.py [--cores FILE] [--output FILE]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CORES = os.path.join(HERE, "cores-per-cluster.json")
DEFAULT_OUTPUT = os.path.join(HERE, "pm-page.html")
TEMPLATE = os.path.join(HERE, "pm-page-template.html")
SIZE_PODS = os.path.join(HERE, "size-pods.json")
PLACEHOLDER = "__PAGE_DATA__"
RESOURCE_NAMES = {"cpu_cores": "CPU", "memory_gib": "memory", "swift_nics": "network cards (SWIFT NICs)",
                  "pods": "pod slots"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cores", default=DEFAULT_CORES,
                        help="cores per cluster from cores_per_cluster.py (default: cores-per-cluster.json next to "
                             "this script)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="page to write (default: pm-page.html next to this script)")
    return parser.parse_args()


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as error:
        sys.exit(f"couldn't read {path}: {error}")


def others_text(other_machines):
    """ "1 system machine with 8 cores and 3 infra machines with 32 cores"."""
    parts = [f"{pool['machines']} {pool['pool']} machine{'s' if pool['machines'] != 1 else ''} with {pool['cores']} "
             f"cores" for pool in other_machines]
    return " and ".join(parts)


def notes(cores, reserved_from):
    """The page's "How these numbers are worked out" list."""
    settings = cores["settings"]
    room = settings["room_per_machine"]
    run = reserved_from or {}
    source = (f"the newest scale test that recorded them ({run['run']}: OpenShift {run['ocp_version']}, {run['date']})"
              if run.get("run") else "the newest scale test that recorded them")
    return [
        f"What a cluster needs: the pods one hosted cluster of each size runs and what each one reserves (its "
        f"resource requests), from {source}, with each size's memory settings from HyperShift's cluster sizing "
        f"configuration. A size covers its number of nodes, infra nodes as well as workers.",
        f"How many fit: the most clusters of each size that fit on a full management cluster, worked out by an exact "
        f"solver and checked pod by pod, by production's rules. Each copy of a component with more than one copy runs "
        f"in a different availability zone ({settings['zones']} zones), and each {settings['machine_cores']}-core "
        f"control-plane machine has room for {room['cpu_cores']:g} cores, {room['memory_gib']:g} GiB of memory, "
        f"{room['swift_nics']:g} network cards (SWIFT NICs: each router copy takes one) and {room['pods']:g} pods, "
        f"after what it keeps for its system and the helper pods every machine runs. Small and medium clusters run "
        f"out of network cards first; large and bigger ones run out of memory.",
        "Own cores: one cluster's share of a full management cluster's control-plane machines, the machines its own "
        "control plane runs on: their cores divided by the clusters that fit.",
        "Amortized cores: one cluster's share of what's there for all the clusters: the management cluster's other "
        "machines (its system and infra machines, which run its logging, monitoring and cluster-management agents), "
        "and any control-plane room left unused because of the limit on clusters per management cluster.",
        "Quota is planned per management cluster, for all its machines at their largest, so a cluster's cores are an "
        "average share: a region whose management clusters are full needs a whole new one at once.",
        "The settings above are placeholders, to replace with production's own numbers from its deployment "
        "configuration.",
        "Not included: spare room for upgrades, a zone failure or new clusters; the cluster autoscaler of clusters "
        "that use autoscaling; the shared pods some control-plane machines run, such as the HyperShift operator's. "
        "The numbers assume machines are packed as tightly as the rules allow, which production's scheduler and "
        "placement may not manage for large and bigger clusters. Mixing sizes on a management cluster packs a little "
        "better than their shares add up to.",
        "These are estimates for planning, not an exact count.",
    ]


def page_data(cores, reserved_from):
    settings = cores["settings"]
    return {
        "sizes": [{
            "size": size["size"],
            "nodes_from": size["nodes_from"],
            "nodes_to": size["nodes_to"],
            "clusters_that_fit": size["clusters_that_fit"],
            "runs_out_of": RESOURCE_NAMES.get(size["runs_out_of"], size["runs_out_of"]),
        } for size in cores["sizes"]],
        "machine_cores": settings["machine_cores"],
        "zones": settings["zones"],
        "defaults": {
            "machines_per_zone": settings["full_machines_per_zone"],
            "other_cores": sum(pool["machines"] * pool["cores"] for pool in settings["other_machines"]),
            "other_text": others_text(settings["other_machines"]),
            "limit": settings["limit"],
        },
        "notes": notes(cores, reserved_from),
    }


def main():
    args = parse_args()
    cores_path = os.path.expanduser(args.cores)
    cores = read_json(cores_path)
    if not cores.get("sizes"):
        sys.exit(f"{cores_path} has no sizes")
    if cores.get("basis") != "reserved":
        print(f"warning: {cores_path} packs what the pods {cores.get('basis')}, not what they reserve",
              file=sys.stderr)
    reserved_from = read_json(SIZE_PODS).get("reserved_from") if os.path.exists(SIZE_PODS) else None
    with open(TEMPLATE) as f:
        template = f.read()
    if template.count(PLACEHOLDER) != 1:
        sys.exit(f"{TEMPLATE} must contain {PLACEHOLDER} exactly once")
    # Escaping "<" keeps the data from ever closing its <script> element early.
    data = json.dumps(page_data(cores, reserved_from), ensure_ascii=False).replace("<", "\\u003c")
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(template.replace(PLACEHOLDER, data))
    print(f"wrote {target}: {len(cores['sizes'])} sizes", file=sys.stderr)


if __name__ == "__main__":
    main()
