#!/usr/bin/env python3
"""Build the PM page: one self-contained HTML file that estimates management-cluster cores.

Reads size-cores.json (from size_cores.py), the simulator profiles it was built from (for the
percentile used) and container-usage.json (for each test run's trust note), puts them into
pm-page-template.html and writes pm-page.html, all next to this script by default. The page needs no
server or network connection: open it in a browser, or send the file to someone. Rerun this after
size_cores.py whenever the numbers change.

Usage:
  python3 build_pm_page.py [--cores FILE] [--output FILE]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CORES = os.path.join(HERE, "size-cores.json")
DEFAULT_OUTPUT = os.path.join(HERE, "pm-page.html")
TEMPLATE = os.path.join(HERE, "pm-page-template.html")
USAGE_DATA = os.path.join(HERE, "container-usage.json")
PLACEHOLDER = "__PAGE_DATA__"
PLACEMENTS = {
    "legacy": "today's placement, with 3 copies of each API server, one in each availability zone",
    "minimal": "the proposed minimal-zonal placement, with 2 copies of each API server",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cores", default=DEFAULT_CORES,
                        help="cores per size from size_cores.py (default: size-cores.json next to this script)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="page to write (default: pm-page.html next to this script)")
    return parser.parse_args()


def read_json(path, required=True):
    if not os.path.exists(path) and not required:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as error:
        sys.exit(f"couldn't read {path}: {error}")


def ordinal(percentile):
    """ "p95" -> "95th percentile"."""
    number = int(percentile.lstrip("p"))
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix} percentile"


def machines_text(cores):
    types = cores["machine_types"]
    if cores["settings"]["machines"] == "production":
        return "production's management-cluster machines, " + ", ".join(
            f"{name} ({spec['vcpu']} cores, {spec['memory_gib']:g} GiB)" for name, spec in types.items())
    sizes = sorted({spec["vcpu"] for spec in types.values()})
    return f"the scheduling simulator's own choice of machines ({sizes[0]} to {sizes[-1]} cores)"


def assumptions(cores, profiles):
    settings = cores["settings"]
    simulator = settings["simulator"]
    mix = settings["mix"]
    mix_text = ", ".join(f"{count} of {size} nodes" for size, count in sorted(mix.items(), key=lambda item: int(item[0])))
    usage = (f"Usage comes from one kube-burner scale test per cluster size: for each container, the "
             f"{ordinal(profiles['settings']['percentile'])} of all its copies' readings during the test's steady "
             f"churn phase. A pod reserves what its containers add up to.") if profiles else \
        "Usage comes from one kube-burner scale test per cluster size."
    margin = simulator["multiplier"]
    return [
        usage,
        "Own cores: what one cluster's own control-plane pods reserve, including a router and two login components "
        "that every cluster runs.",
        "Shared cores: the cluster's share of what no single cluster owns: system reservations and agents on every "
        "machine, spare room for upgrades and a zone failure, placeholder slots for new clusters, and room left unused "
        f"when machines fill up on one resource first. It comes from the scheduling simulator packing a sample mix of "
        f"{sum(mix.values())} clusters ({mix_text}) onto {machines_text(cores)}.",
        f"Placement: {PLACEMENTS.get(simulator['policy'], simulator['policy'])}.",
        "No safety margin is added." if margin == 1 else
        f"Every component's reservation is increased by {round((margin - 1) * 100):g}%.",
        "These are estimates for planning, not an exact bill. They cover the management-cluster machines only, and "
        "don't include costs in dollars yet.",
    ]


def page_data(cores, profiles, usage):
    runs = {run["run"]: run for run in (usage or {}).get("runs", [])}
    sizes = []
    for size, numbers in sorted(cores["sizes"].items(), key=lambda item: int(item[0])):
        run = runs.get(numbers["run"], {})
        sizes.append({
            "size": size,
            "run": numbers["run"],
            "trust": numbers["trust"] or "caveats",
            "date": run.get("date"),
            "notes": run.get("notes"),
            "own_cores": numbers["own_cores"],
            "total_cores": numbers["total_cores"],
        })
    return {
        "sizes": sizes,
        "assumptions": assumptions(cores, profiles),
        "notes_for_all_runs": (usage or {}).get("notes_for_all_runs"),
    }


def main():
    args = parse_args()
    cores_path = os.path.expanduser(args.cores)
    cores = read_json(cores_path)
    if not cores.get("sizes"):
        sys.exit(f"{cores_path} has no sizes")
    profiles = read_json(os.path.join(os.path.dirname(cores_path), cores["settings"]["profiles"]), required=False)
    usage = read_json(USAGE_DATA, required=False)
    with open(TEMPLATE) as f:
        template = f.read()
    if template.count(PLACEHOLDER) != 1:
        sys.exit(f"{TEMPLATE} must contain {PLACEHOLDER} exactly once")
    # Escaping "<" keeps the data from ever closing its <script> element early.
    data = json.dumps(page_data(cores, profiles, usage), ensure_ascii=False).replace("<", "\\u003c")
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(template.replace(PLACEHOLDER, data))
    print(f"wrote {target}: {len(cores['sizes'])} sizes", file=sys.stderr)


if __name__ == "__main__":
    main()
