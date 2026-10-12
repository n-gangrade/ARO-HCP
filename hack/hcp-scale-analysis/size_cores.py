#!/usr/bin/env python3
"""Cores one hosted cluster of each size takes up on the management-cluster machines.

Loads the per-component numbers written by simulator_profiles.py (simulator-profiles.json next to
this script by default) into the scheduling simulator in ../scheduling-simulator, which packs a
sample mix of hosted clusters onto management-cluster machines. The machines are then split between
the clusters the way the simulator splits its bill (its costing.py): each pool goes to each size by
that size's largest share of the pool's CPU, memory, SWIFT NICs or pod slots. For each size it
writes (size-cores.json next to this script by default):

  own_cores, own_memory_gib  what one cluster's own control-plane pods reserve, including the
                             router and login components the simulator adds to every cluster
  total_cores                one cluster's share of the machines' cores. It also covers what the
                             machines hold that no single cluster owns: system reservations,
                             per-machine daemonsets, spare room for upgrades and a zone failure,
                             placeholder slots for new clusters and unused capacity
  shared_cores               total_cores - own_cores
  machine_share              one cluster's share of each machine type, in machines. Multiplied by a
                             region's hourly prices, it gives the cluster's cost there

The split depends on which clusters share the machines, so it needs a mix. By default that's the
simulator's sample fleet (DEFAULT_DISTRIBUTION in its server.py); --mix replaces it, for example
with a region's real mix. Every size the mix leaves out gets EXTRA_PER_MISSING_SIZE clusters, so
every size gets a share. The simulator picks machines without looking at prices, so none of this
depends on them.

It also depends on the machines. By default (--machines production) they're production's: every
control-plane pod runs on 32-core Standard_E32ds_v6 machines (mgmt.aks.userAgentPool.vmSize in
config/config.yaml), with the same fixed system reservation on every machine as production's
kubelet (mgmt-fixes/deploy/kubelet-ds). The simulator always sizes a second pool, which can't use the
first pool's machine type; with today's placement it only holds spare copies for upgrades, which
production keeps on the same machines, so it gets the same machine under another name, merged back
in the output. With --machines simulator, the simulator picks from its own 8 machine types with its
own default settings instead, as on its website.

Usage:
  python3 size_cores.py [--profiles FILE] [--output FILE] [--mix SIZE=N,...] [--machines SETUP]
                        [--policy POLICY] [--multiplier X]

Needs the scheduling simulator's Python packages (its venv has them).
"""
import argparse
import json
import os
import sys
from dataclasses import asdict, replace

HERE = os.path.dirname(os.path.abspath(__file__))
SIMULATOR = os.path.join(os.path.dirname(HERE), "scheduling-simulator")
sys.path.insert(0, SIMULATOR)

from costing import cost_summary
from demand.model import DemandModel
from optimize.engine import RunConfig, simulate
from skus import load_catalog

DEFAULT_PROFILES = os.path.join(HERE, "simulator-profiles.json")
DEFAULT_OUTPUT = os.path.join(HERE, "size-cores.json")
USAGE_DATA = os.path.join(HERE, "container-usage.json")
# The scheduling simulator's sample fleet (DEFAULT_DISTRIBUTION in its server.py).
SAMPLE_MIX = {"12": 200, "30": 150, "60": 80, "120": 40, "250": 10}
EXTRA_PER_MISSING_SIZE = 10
MACHINE_SETUPS = ("production", "simulator")
# Production's control-plane machine (mgmt.aks.userAgentPool.vmSize in config/config.yaml), and the machine in the
# simulator's catalog with the same cores, memory and NICs (Standard_E32ds_v6 only adds a local disk).
PRODUCTION_MACHINE = "Standard_E32ds_v6"
SAME_MACHINE_IN_CATALOG = "Standard_E32s_v6"
SECOND_POOL_SUFFIX = " (second pool)"
FIELDS = {
    "run": "the test run the size's numbers come from",
    "trust": "how much to trust that run: good, caveats or low (see its notes in container-usage.json)",
    "clusters_in_mix": "how many clusters of this size the mix has",
    "own_cores": "cores one cluster's own control-plane pods reserve",
    "own_memory_gib": "memory one cluster's own control-plane pods reserve, in GiB",
    "total_cores": "one cluster's share of the machines' cores, including what no single cluster owns",
    "shared_cores": "total_cores - own_cores: the cluster's share of what no single cluster owns",
    "machine_share": "one cluster's share of each machine type, in machines; times a region's hourly prices, it "
                     "gives the cluster's cost there",
}


def parse_mix(text):
    """Parse "12=200,30=150" into {"12": 200, "30": 150}."""
    mix = {}
    for part in text.split(","):
        size, _, count = (piece.strip() for piece in part.partition("="))
        if not size.isdigit() or not count.isdigit():
            raise argparse.ArgumentTypeError(f"expected SIZE=COUNT pairs such as 120=40, not {part!r}")
        mix[str(int(size))] = int(count)
    return mix


def positive(text):
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be more than 0")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profiles", default=DEFAULT_PROFILES,
                        help="simulator profiles (default: simulator-profiles.json next to this script)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="file to write (default: size-cores.json next to this script)")
    parser.add_argument("--mix", type=parse_mix, metavar="SIZE=N,...",
                        help="how many clusters of each size share the machines "
                             "(default: the simulator's sample fleet, 12=200,30=150,60=80,120=40,250=10)")
    parser.add_argument("--machines", choices=MACHINE_SETUPS, default="production",
                        help="production: production's 32-core machines and system reservation; simulator: the "
                             "simulator's own choice of machine types and default settings (default: production)")
    parser.add_argument("--policy", choices=("legacy", "minimal"), default="legacy",
                        help="the simulator's placement policy: legacy is today's, with 3 copies of the API servers, "
                             "and minimal is the proposed minimal-zonal one (default: legacy)")
    parser.add_argument("--multiplier", type=positive, default=1.0,
                        help="multiply every component's numbers by this, such as 1.15 for a 15%% margin (default: 1)")
    return parser.parse_args()


def machine_setup(setup):
    """(machine types the simulator may use, {second-pool name: machine it stands for}, RunConfig settings)."""
    catalog = load_catalog()
    if setup == "simulator":
        return catalog, {}, {}
    same = next((machine for machine in catalog if machine.name == SAME_MACHINE_IN_CATALOG), None)
    if same is None:
        sys.exit(f"the simulator's machine catalog has no {SAME_MACHINE_IN_CATALOG} to stand for {PRODUCTION_MACHINE}")
    machine = replace(same, name=PRODUCTION_MACHINE)
    second_pool = replace(same, name=PRODUCTION_MACHINE + SECOND_POOL_SUFFIX)
    return [machine, second_pool], {second_pool.name: machine.name}, {"reservation_mode": "flat"}


def merged(counts, aliases):
    """{machine type: n} with each second-pool name added into the machine it stands for."""
    out = {}
    for name, count in counts.items():
        name = aliases.get(name, name)
        out[name] = out.get(name, 0) + count
    return dict(sorted(out.items()))


def machine_shares(result, cfg, model, catalog):
    """({size: {machine type: machines per cluster}}, rows, inventory). The machines are split the way
    costing.py splits the bill: pricing one machine type at 1 an hour and the rest at 0 makes a
    cluster's hourly cost its share of that type. rows and inventory are costing.py's per-size demand
    and machine counts, which don't depend on prices."""
    shares, rows, inventory = {}, [], {}
    for machine in catalog:
        unit_prices = {"hourly": {other.name: float(other.name == machine.name) for other in catalog}}
        summary = cost_summary(result, cfg, model, unit_prices)
        if summary["error"]:
            sys.exit(summary["error"])
        rows, inventory = summary["rows"], summary["inventory"]
        for row in rows:
            if row["hourly"]:
                shares.setdefault(row["size"], {})[machine.name] = row["hourly"]
    return shares, rows, inventory


def run_trust(profiles):
    """{size: (run, trust)} from the profiles' runs and the trust notes in container-usage.json, where known."""
    runs = profiles.get("runs", {})
    trust = {}
    if os.path.exists(USAGE_DATA):
        with open(USAGE_DATA) as f:
            trust = {run["run"]: run["trust"] for run in json.load(f)["runs"]}
    return {size: (run, trust.get(run)) for size, run in runs.items()}


def main():
    args = parse_args()
    path = os.path.expanduser(args.profiles)
    try:
        with open(path) as f:
            profiles = json.load(f)
        model = DemandModel(profiles_path=path)
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"couldn't read {path}: {error}")
    given = SAMPLE_MIX if args.mix is None else args.mix
    unknown = sorted(set(given) - set(model.sizes), key=int)
    if unknown:
        sys.exit(f"--mix has sizes without numbers: {', '.join(unknown)}; the sizes are {', '.join(model.sizes)}")
    mix = {size: given.get(size) or EXTRA_PER_MISSING_SIZE for size in model.sizes}

    catalog, aliases, settings = machine_setup(args.machines)
    cfg = RunConfig(mode="exact", policy=args.policy, multiplier=args.multiplier, **settings)
    result = simulate(mix, cfg, model, catalog)
    if "error" in result:
        sys.exit(f"the simulator couldn't place the mix: {result['error']}")
    shares, rows, inventory = machine_shares(result, cfg, model, catalog)
    shares = {size: merged(share, aliases) for size, share in shares.items()}
    inventory = merged(inventory, aliases)
    machines = {machine.name: machine for machine in catalog}
    sources = run_trust(profiles)

    sizes = {}
    for row in rows:
        size, share = row["size"], shares.get(row["size"], {})
        total = sum(count * machines[name].vcpu for name, count in share.items())
        run, trust = sources.get(size, (None, None))
        sizes[size] = {
            "run": run,
            "trust": trust,
            "clusters_in_mix": row["count"],
            "own_cores": round(row["cpu"], 2),
            "own_memory_gib": round(row["mem"], 2),
            "total_cores": round(total, 2),
            "shared_cores": round(total - row["cpu"], 2),
            "machine_share": {name: round(count, 6) for name, count in sorted(share.items())},
        }
        if total < row["cpu"]:
            print(f"warning: size {size}'s share of the machines' cores ({total:.2f}) is less than its own pods "
                  f"reserve ({row['cpu']:.2f}), as it's split by each size's largest share of any resource",
                  file=sys.stderr)

    region = result["region"]
    used = sorted({name for share in shares.values() for name in share})
    output = {
        "description": "Cores one hosted cluster of each size takes up on the management-cluster machines, written by "
                       "hack/hcp-scale-analysis/size_cores.py with the scheduling simulator.",
        "settings": {
            "profiles": os.path.basename(path),
            "mix": mix,
            "extra_per_missing_size": EXTRA_PER_MISSING_SIZE,
            "machines": args.machines,
            "simulator": asdict(cfg),
        },
        "fields": FIELDS,
        "fleet": {
            "clusters": result["total_hcps"],
            "management_clusters": result["n_mcs"],
            "machines": region["zonal_nodes"] + region["overflow_nodes"],
            "machine_cores": region["zonal_cores"] + region["overflow_cores"],
            "machines_by_type": dict(sorted(inventory.items())),
        },
        "machine_types": {name: {"vcpu": machines[name].vcpu, "memory_gib": machines[name].memory_gib}
                          for name in used},
        "sizes": sizes,
    }
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")
    print(f"wrote {target}: {len(sizes)} sizes, {result['total_hcps']} clusters on {output['fleet']['machines']} "
          f"{args.machines} machines with {output['fleet']['machine_cores']} cores", file=sys.stderr)


if __name__ == "__main__":
    main()
