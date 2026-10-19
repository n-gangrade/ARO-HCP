#!/usr/bin/env python3
"""Build the PM page: one self-contained HTML file that works out the cores customers' hosted clusters need.

Reads cores-per-cluster.json (from cores_per_cluster.py) and size-pods.json (what one cluster of each size reserves,
and the test run that comes from), puts them into pm-page-template.html and writes pm-page.html, all next to this
script by default. The page needs no server or network connection: open it in a browser, or send the file to
someone. Rerun this whenever those files change.

On the page, PMs add a row for each customer, region, cluster size and number of clusters, and get the cores needed
per region and per customer: each cluster's own share of a full management cluster's control-plane machines and its
amortized share of what's there for all the clusters. As quota comes in whole management clusters, it also gives the
quota a region needs if its management clusters are full. The management cluster's settings are on the page, with
cores-per-cluster.json's as defaults. If those are cores_per_cluster.py's placeholders, the page says so at the top;
for real quota requests, build a copy from a cores-per-cluster.json made with production's --full, --other and
--limit, and keep that copy out of this public repo.

Usage:
  python3 build_pm_page.py [--cores FILE] [--output FILE] [--settings-from TEXT]
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
RESOURCE_NAMES = {"cpu_cores": "CPU", "memory_gib": "memory", "swift_nics": "network cards", "pods": "pod slots"}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cores", default=DEFAULT_CORES,
                        help="cores per cluster from cores_per_cluster.py (default: cores-per-cluster.json next to "
                             "this script)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="page to write (default: pm-page.html next to this script)")
    parser.add_argument("--settings-from", metavar="TEXT",
                        help="where the management cluster's settings come from, shown at the top of the page, such "
                             "as \"production's deployment configuration\"; not for cores_per_cluster.py's "
                             "placeholders, which the page warns about instead")
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


def notes(cores, reserved_from, settings_from):
    """The page's "How these numbers are worked out" list."""
    settings = cores["settings"]
    room = settings["room_per_machine"]
    run = reserved_from or {}
    source = (f"the newest scale test that recorded them (OpenShift {run['ocp_version']}, {run['date']})"
              if run.get("ocp_version") and run.get("date") else "the newest scale test that recorded them")
    if settings.get("placeholders", True):
        defaults = ("The settings' defaults are placeholders, not production's: the size of the 49-node scale test's "
                    "management cluster, which held 12 hosted clusters, so wasn't full. For real quota requests, use a "
                    "copy of this page built with production's settings.")
    elif settings_from:
        defaults = f"The settings' defaults are from {settings_from}."
    else:
        defaults = "The settings' defaults are the ones this page was built with."
    return [
        f"What a cluster reserves: the CPU and memory set aside for every part of one hosted cluster's control plane, "
        f"from {source}, with each size's memory from HyperShift's cluster sizing settings. A cluster's size goes by "
        f"its total number of nodes.",
        f"How many fit: the most clusters of each size that fit on a full management cluster, worked out exactly and "
        f"checked part by part against production's rules. Copies of the same part of a cluster go in different "
        f"availability zones ({settings['zones']} zones). Each {settings['machine_cores']}-core machine has room for "
        f"{room['cpu_cores']:g} cores, {room['memory_gib']:g} GiB of memory, {room['swift_nics']:g} network cards and "
        f"{room['pods']:g} pods once its own system has taken its share, and every cluster needs a network card in "
        f"each zone, for its router. \u201cLimited by\u201d in the table of cores per cluster shows what runs out "
        f"first.",
        "Own cores: one cluster's share of the machines its control plane runs on, the control-plane machines: their "
        "cores divided by the clusters that fit.",
        "Amortized cores: one cluster's share of what's there for all the clusters: the management cluster's other "
        "machines (its system and infra machines, which run its logging, monitoring and cluster-management agents), "
        "and any room on the control-plane machines left unused because of the limit on clusters per management "
        "cluster.",
        "Quota is planned per management cluster, for all its machines at their largest, so the cores needed are the "
        "clusters' average share. A region whose management clusters have room for the clusters needs no new quota for "
        "them; a region whose management clusters are full needs whole new ones, as \u201cQuota if the region is "
        "full\u201d shows.",
        defaults,
        "Not included: spare room for upgrades, a zone failure or new clusters; the cluster autoscaler of clusters "
        "that use autoscaling; the shared pods some control-plane machines run, such as the HyperShift operator's. "
        "The numbers assume machines are packed as tightly as the rules allow, which production may not manage for "
        "large and bigger clusters. Mixing sizes on a management cluster packs a little better than their shares add "
        "up to.",
        "These are estimates for planning, not an exact count.",
    ]


def page_data(cores, size_pods, settings_from):
    settings = cores["settings"]
    reserved = {size["size"]: size["reserved"]["totals"] for size in size_pods["sizes"]}
    return {
        "sizes": [{
            "size": size["size"],
            "nodes_from": size["nodes_from"],
            "nodes_to": size["nodes_to"],
            "reserves": {resource: reserved[size["size"]][resource] for resource in ("cpu_cores", "memory_gib")},
            "clusters_that_fit": size["clusters_that_fit"],
            "runs_out_of": [RESOURCE_NAMES.get(resource, resource) for resource in size["runs_out_of_by_machines"]],
        } for size in cores["sizes"]],
        "machine_cores": settings["machine_cores"],
        "zones": settings["zones"],
        "network_cards_per_machine": settings["room_per_machine"]["swift_nics"],
        "defaults": {
            "machines_per_zone": settings["full_machines_per_zone"],
            "other_cores": sum(pool["machines"] * pool["cores"] for pool in settings["other_machines"]),
            "other_text": others_text(settings["other_machines"]),
            "limit": settings["limit"],
            "placeholders": settings.get("placeholders", True),
            "from": settings_from,
        },
        "notes": notes(cores, size_pods.get("reserved_from"), settings_from),
    }


def check_inputs(cores, cores_path, size_pods):
    """Stops if the inputs don't hold what the page needs, or what its text says about them."""
    if not cores.get("sizes"):
        sys.exit(f"{cores_path} has no sizes")
    reserved = {size["size"]: size.get("reserved", {}).get("totals") for size in size_pods.get("sizes", [])}
    zones = cores["settings"]["zones"]
    for size in cores["sizes"]:
        if len(size.get("runs_out_of_by_machines", [])) != len(size["clusters_that_fit"]):
            sys.exit(f"{cores_path} has no runs_out_of_by_machines for {size['size']}: rerun cores_per_cluster.py")
        totals = reserved.get(size["size"])
        if not totals:
            sys.exit(f"{SIZE_PODS} has no reservations for {size['size']}")
        # The page says every cluster needs a network card in each zone.
        if totals["swift_nics"] != zones:
            sys.exit(f"{size['size']} reserves {totals['swift_nics']} network cards, not one per zone ({zones}): "
                     f"update the page's text")


def main():
    args = parse_args()
    cores_path = os.path.expanduser(args.cores)
    cores = read_json(cores_path)
    size_pods = read_json(SIZE_PODS)
    check_inputs(cores, cores_path, size_pods)
    if args.settings_from and cores["settings"].get("placeholders", True):
        sys.exit(f"{cores_path} has cores_per_cluster.py's placeholder settings, so the page can't say they're from "
                 f"{args.settings_from}: rebuild it with --full and --other")
    if cores.get("basis") != "reserved":
        print(f"warning: {cores_path} packs what the pods {cores.get('basis')}, not what they reserve",
              file=sys.stderr)
    with open(TEMPLATE) as f:
        template = f.read()
    if template.count(PLACEHOLDER) != 1:
        sys.exit(f"{TEMPLATE} must contain {PLACEHOLDER} exactly once")
    # Escaping "<" keeps the data from ever closing its <script> element early.
    data = json.dumps(page_data(cores, size_pods, args.settings_from), ensure_ascii=False).replace("<", "\\u003c")
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(template.replace(PLACEHOLDER, data))
    print(f"wrote {target}: {len(cores['sizes'])} sizes", file=sys.stderr)


if __name__ == "__main__":
    main()
