#!/usr/bin/env python3
"""How many management-cluster machines, and so how many cores of quota, a customer's hosted clusters need.

Reads the pods each t-shirt size runs from size-pods.json (written by size_pods.py) and packs every pod of
the clusters given onto production's control-plane machines, spread over 3 availability zones. Then it
reports the machines in each zone, the cores to request for them (whole machines), and how full each kind
of room is.

The rules production's scheduler follows:
- The copies of a component with more than one copy run in different zones, so on different machines
  (one_per_zone in size-pods.json). A component with one copy can run in any zone.
- A pod fits on a machine if what's left of the machine's CPU, memory, SWIFT NICs and pod slots covers what
  the pod reserves, and one slot. What's left starts from ROOM: the machine's own, less the kubelet's system
  reservation and the helper pods every machine runs.

How it packs: first the components with a copy in every zone, then the rest, each biggest first. Each copy
goes to a zone where it fits on a machine already there, the least full in what the copy needs first, and
there on the machine with the most room left, as production's scheduler does, which keeps the machines
evenly full. It adds a machine only when a copy fits on none.

That isn't always the fewest machines possible. Compared with an exact solver (check_packing.py), it needs
the fewest for small and medium clusters, and up to a few more for mixes with larger ones, as the fewest can
take a different number of machines in each zone. No packing that follows the rules needs fewer than the
fewest, so it never asks for too few, and production's scheduler, which places clusters one at a time as
they come, doesn't pack as tightly either. Not included:
- the machines every management cluster keeps whatever its load: its system and infra pools, and the
  minimum number of machines production keeps in each of its control-plane pools;
- spare room for upgrades, a zone failure or new clusters;
- the cluster autoscaler, which clusters that use autoscaling run (not_included in size-pods.json);
- the kubelet's eviction threshold, under 1 GiB of each machine's memory.

--per-set shows how many clusters of each size fit on one machine in each zone.

Usage:
  python3 pack_clusters.py SIZE=COUNT [SIZE=COUNT ...] [--basis reserved|used] [--pods FILE] [--json]
  python3 pack_clusters.py --per-set [--basis reserved|used] [--pods FILE] [--json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PODS = os.path.join(HERE, "size-pods.json")
ZONES = 3
MACHINE = "Standard_E32ds_v6"  # mgmt.aks.userAgentPool.vmSize in config/config.yaml
MACHINE_CORES = 32             # what one machine counts against the quota
RESOURCES = ("cpu_cores", "memory_gib", "swift_nics", "pods")
RESOURCE_NAMES = {"cpu_cores": "CPU (cores)", "memory_gib": "memory (GiB)", "swift_nics": "SWIFT NICs",
                  "pods": "pod slots"}
# What one machine has:
# - memory: what the 49-node run's control-plane machines show (node_memory_MemTotal_bytes); Azure lists 256 GiB
# - SWIFT NICs: 8 network cards, one kept for the machine itself (fleet/pkg/azure/skucache/skucache.go, and
#   secondaryNicCount in config/config.yaml)
# - pods: maxPods of the control-plane pools (dev-infrastructure/modules/aks-cluster-base.bicep)
MACHINE_HAS = {"cpu_cores": 32.0, "memory_gib": 251.7, "swift_nics": 7, "pods": 225}
# The kubelet's system reservation on every machine (mgmt-fixes/deploy/kubelet-ds)
SYSTEM_RESERVED = {"cpu_cores": 3.0, "memory_gib": 7550 / 1024, "swift_nics": 0, "pods": 0}
# The helper pods (daemonsets) every control-plane machine runs: in the 49-node run, each of the 18 ran the same 14
# (kube-system's network, storage, monitoring and security agents, arobit's log forwarder and velero's node agent).
# What they reserve isn't in the data, so their CPU and memory are the scheduling simulator's stand-in
# (node_overhead_cpu_mc and node_overhead_mem_mib in ../scheduling-simulator/optimize/engine.py), measured as what
# such pods used on a production management cluster; in the 49-node run, the pods outside the control planes used
# 2.2 to 4.8 GiB per machine.
HELPER_PODS = {"cpu_cores": 0.468, "memory_gib": 3592 / 1024, "swift_nics": 0, "pods": 14}
ROOM = {resource: MACHINE_HAS[resource] - SYSTEM_RESERVED[resource] - HELPER_PODS[resource] for resource in RESOURCES}
TOLERANCE = 1e-9
MOST_PER_SET = 100  # --per-set stops counting here


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clusters", nargs="*", metavar="SIZE=COUNT",
                        help="how many clusters of each t-shirt size, such as small=10 large=2")
    parser.add_argument("--basis", choices=("reserved", "used"), default="reserved",
                        help="pack what the pods reserve today, or what they use (default: reserved)")
    parser.add_argument("--pods", default=DEFAULT_PODS,
                        help="pods per t-shirt size from size_pods.py (default: size-pods.json next to this script)")
    parser.add_argument("--per-set", action="store_true",
                        help="show how many clusters of each size fit on one machine in each zone")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()
    if not args.clusters and not args.per_set:
        parser.error("give the clusters to pack, such as small=10, or --per-set")
    return args


def load_sizes(path, basis):
    """{size: [pod]} for the basis, in size order."""
    try:
        with open(path) as f:
            data = json.load(f)
        return {size["size"]: size[basis]["pods"] for size in data["sizes"]}
    except (OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"couldn't read the {basis} pods in {path}: {error}; rerun size_pods.py")


def parse_clusters(texts, sizes):
    """{size: count} from SIZE=COUNT texts."""
    mix = {}
    for text in texts:
        size, _, count = text.partition("=")
        if size not in sizes:
            sys.exit(f"{text}: unknown size {size!r}; the sizes are {', '.join(sizes)}")
        if not count.isdigit() or int(count) < 1:
            sys.exit(f"{text}: the count must be a whole number of 1 or more")
        mix[size] = mix.get(size, 0) + int(count)
    return mix


def share(need):
    """The share of one machine's room a copy takes in its fullest resource."""
    return max(need[resource] / ROOM[resource] for resource in RESOURCES)


def units_for(mix, sizes):
    """Every component of every cluster: first those with a copy in every zone, then the rest, each group biggest
    first by share(); ties keep the clusters' and pods' order. The pods that must be in every zone go first, so
    the rest can fill the gaps they leave, wherever there's room."""
    units = []
    for size, count in mix.items():
        for number in range(1, count + 1):
            for pod in sizes[size]:
                need = {"cpu_cores": pod["cpu_cores"], "memory_gib": pod["memory_gib"],
                        "swift_nics": pod["swift_nics"], "pods": 1}
                if share(need) > 1 + TOLERANCE:
                    sys.exit(f"a copy of {pod['component']} in a {size} cluster needs more than a whole machine")
                units.append({"cluster": f"{size} {number}", "component": pod["component"], "copies": pod["copies"],
                              "one_per_zone": pod["one_per_zone"], "need": need})
    units.sort(key=lambda unit: (not (unit["one_per_zone"] and unit["copies"] == ZONES), -share(unit["need"])))
    return units


def fits(machine, need):
    return all(machine["used"][resource] + need[resource] <= ROOM[resource] + TOLERANCE for resource in RESOURCES)


def fullness(machine, need):
    """How full the machine would be with the copy added, in the fullest of the resources the copy needs."""
    return max((machine["used"][resource] + need[resource]) / ROOM[resource]
               for resource in RESOURCES if need[resource] > 0)


def roomiest(machines, need):
    """The machine with room for the copy that has the most room left after it, or None. Like production's
    scheduler, which puts a pod on the machine with the most room left, this keeps a zone's machines evenly full."""
    candidates = [machine for machine in machines if fits(machine, need)]
    return min(candidates, key=lambda machine: fullness(machine, need)) if candidates else None


def load(machines, need):
    """How full a zone is in the resources a copy needs, in machines' worth of room: the fullest of them."""
    return max(sum(machine["used"][resource] for machine in machines) / ROOM[resource]
               for resource in RESOURCES if need[resource] > 0)


def pack(mix, sizes):
    """[[machine] for each zone], with every pod of the clusters in `mix` placed. A machine is {"zone", "used":
    {resource: what its pods reserve}, "pods": [(cluster, component)]}."""
    zones = [[] for _ in range(ZONES)]
    for unit in units_for(mix, sizes):
        need = unit["need"]
        # Copies that must run in different zones go together; other copies are placed one by one.
        groups = [unit["copies"]] if unit["one_per_zone"] else [1] * unit["copies"]
        for spread in groups:
            if spread > ZONES:
                sys.exit(f"{unit['component']} has {spread} copies, more than the {ZONES} zones")
            ranked = sorted(range(ZONES), key=lambda zone: (roomiest(zones[zone], need) is None,
                                                            load(zones[zone], need), zone))
            for zone in ranked[:spread]:
                machine = roomiest(zones[zone], need)
                if machine is None:
                    machine = {"zone": zone + 1, "used": dict.fromkeys(RESOURCES, 0.0), "pods": []}
                    zones[zone].append(machine)
                for resource in RESOURCES:
                    machine["used"][resource] += need[resource]
                machine["pods"].append((unit["cluster"], unit["component"]))
    return zones


def summarize(mix, zones):
    """The result as printed with --json."""
    machines = [machine for zone in zones for machine in zone]
    used = {resource: sum(machine["used"][resource] for machine in machines) for resource in RESOURCES}
    room = {resource: len(machines) * ROOM[resource] for resource in RESOURCES}
    full = {resource: used[resource] / room[resource] if room[resource] else 0.0 for resource in RESOURCES}
    return {
        "clusters": mix,
        "machine": MACHINE,
        "machines_per_zone": [len(zone) for zone in zones],
        "machines": len(machines),
        "cores": len(machines) * MACHINE_CORES,
        "used": {resource: round(value, 3) for resource, value in used.items()},
        "room": {resource: round(value, 3) for resource, value in room.items()},
        "full": {resource: round(value, 3) for resource, value in full.items()},
        "fullest": max(RESOURCES, key=full.get) if machines else None,
        "machine_list": [{"zone": machine["zone"], "pods": len(machine["pods"]),
                          "clusters": len({cluster for cluster, _ in machine["pods"]}),
                          "used": {resource: round(value, 3) for resource, value in machine["used"].items()}}
                         for machine in machines],
    }


def per_set(sizes):
    """{size: (clusters, fullest resource)}: the most clusters of each size that fit on one machine in each zone."""
    out = {}
    for size in sizes:
        count, fullest = 0, None
        while count < MOST_PER_SET:
            result = summarize({size: count + 1}, pack({size: count + 1}, sizes))
            if result["machines"] > ZONES:
                break
            count, fullest = count + 1, result["fullest"]
        out[size] = (count, fullest)
    return out


def print_room():
    print(f"Room for control-plane pods on one {MACHINE} machine: " + ", ".join(
        f"{ROOM[resource]:g} {RESOURCE_NAMES[resource]}" if resource in ("swift_nics", "pods")
        else f"{ROOM[resource]:.1f} {RESOURCE_NAMES[resource]}" for resource in RESOURCES))


def main():
    args = parse_args()
    sizes = load_sizes(os.path.expanduser(args.pods), args.basis)
    if args.per_set:
        sets = per_set(sizes)
        if args.json:
            print(json.dumps({"basis": args.basis, "machines": ZONES, "cores": ZONES * MACHINE_CORES,
                              "sizes": {size: {"clusters": count, "fullest": fullest}
                                        for size, (count, fullest) in sets.items()}}, indent=2))
            return
        print_room()
        print(f"Clusters of each size that fit on one machine in each zone ({ZONES} machines, "
              f"{ZONES * MACHINE_CORES} cores), {args.basis} pods:")
        print(f"{'size':>8} {'clusters':>9} {'cores each':>11}  fullest")
        for size, (count, fullest) in sets.items():
            each = f"{ZONES * MACHINE_CORES / count:.1f}" if count else "-"
            print(f"{size:>8} {count:9d} {each:>11}  {RESOURCE_NAMES.get(fullest, '-')}")
        return

    mix = parse_clusters(args.clusters, sizes)
    result = summarize(mix, pack(mix, sizes))
    if args.json:
        print(json.dumps({"basis": args.basis, **result}, indent=2))
        return
    total = sum(mix.values())
    print_room()
    print(f"{total} cluster{'s' if total > 1 else ''} ({', '.join(f'{count} {size}' for size, count in mix.items())}), "
          f"{args.basis} pods:")
    print(f"  machines: {' + '.join(map(str, result['machines_per_zone']))} = {result['machines']} "
          f"(in each of the {ZONES} zones)")
    print(f"  cores to request: {result['cores']}")
    print("  how full the machines are:")
    for resource in RESOURCES:
        mark = "  <- fullest" if resource == result["fullest"] else ""
        print(f"    {RESOURCE_NAMES[resource]:14} {result['used'][resource]:9.1f} of {result['room'][resource]:8.1f}  "
              f"{result['full'][resource]:4.0%}{mark}")


if __name__ == "__main__":
    main()
