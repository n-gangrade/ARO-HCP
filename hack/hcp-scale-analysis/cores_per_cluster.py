#!/usr/bin/env python3
"""The cores one hosted cluster of each t-shirt size takes on a full management cluster: the numbers for the PM page.

A region's hosted clusters share its management clusters: production's placement controller puts each new hosted
cluster on the management cluster with the most free SWIFT NICs. So, as a region grows, each cluster takes up its
share of a full management cluster's machines. For each size, this works out:

- clusters_that_fit: the most clusters of the size that fit on a management cluster with 1, 2, ... up to --most
  control-plane machines in each of its 3 zones, by the rules pack_clusters.py follows (a component's copies in
  different zones, and each machine's room for CPU, memory, SWIFT NICs and pods). As all clusters of a size are the
  same, an exact solver (OR-Tools' CP-SAT) can find them quickly: it works out how many copies of each component go
  on each machine. Where two packings found for fewer machines, side by side, already hold as many clusters as the
  machines' room allows in total, it uses them instead. A number that reaches what the room allows in total is the
  most possible; otherwise the solver proves it is, or it's listed under not_proven with the most the room allows.
  Every packing is spread out into single clusters and checked with check_packing.py's rule checks, independently
  of the solver; it stops if one breaks a rule.
- the cores one cluster takes on a full management cluster, with --full control-plane machines per zone and at most
  --limit hosted clusters: its own share of the control-plane machines, by whatever runs out first (own_cores), and
  its share of what's there for all the clusters, amortized: the machines a management cluster runs besides its
  control-plane machines, its system and infra machines, which run its logging, monitoring and agents, and any
  control-plane room the limit leaves unused (shared_cores), and the two added up (cores).

The settings, the management cluster's machines, are placeholders to confirm (see FULL_PER_ZONE and the settings
after it); --full, --other and --limit change them, and settings.placeholders in the output says whether the
management cluster's machines are still the placeholders. Quota is planned per management cluster, for all its
machines at their largest, so a region that needs another management cluster needs all of its cores at once
(new_management_cluster_cores). It writes cores-per-cluster.json next to this script, or cores-per-cluster-used.json
with --basis used, which packs what the pods use instead of what they reserve.

Not included: spare room for upgrades, a zone failure or new clusters; the cluster autoscaler of clusters that use
autoscaling; the shared pods some control-plane machines run, such as the HyperShift operator's, which in the
49-node run used 3.5 GiB and about 2.5 cores in all; and the infra machines' limits: each hosted cluster runs 5 ACM
agent pods there.

Usage:
  python3 cores_per_cluster.py [--basis reserved|used] [--full N] [--other POOL=MACHINESxCORES ...] [--limit N]
                               [--most N] [--seconds S] [--pods FILE] [--room FILE] [--output FILE]

Requires OR-Tools, which the scheduling simulator's environment has.
"""
import argparse
import json
import os
import sys

import check_packing as cp
import pack_clusters as pc

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = {"reserved": os.path.join(HERE, "cores-per-cluster.json"),
                  "used": os.path.join(HERE, "cores-per-cluster-used.json")}
# Control-plane machines per zone on a full management cluster. A placeholder: production's real number is in its
# private deployment config. 6 is what the 49-node run's management cluster had, with 12 hosted clusters.
FULL_PER_ZONE = 6
# The machines a management cluster runs besides its control-plane machines, as (pool, machines, cores each): in the
# 49-node run, 1 system machine with 8 cores and 3 infra machines with 32 (to confirm; quota is planned for every
# pool at its largest).
OTHER_MACHINES = [("system", 1, 8), ("infra", 3, 32)]
# The most hosted clusters to plan for on one management cluster: 60, what the capacity alerts
# (observability/alerts/mgmt-capacity-prometheusRule.yaml, which ask for another management cluster above 51) and the
# SRE capacity dashboard assume, to confirm: no planning number is written down, the dashboard says 60 comes from
# ClusterSizingConfiguration, where 60 is the small size's node count, and Clusters Service's own limit is higher.
# Highly available clusters run out of SWIFT NICs before that, and filling management clusters that far keeps nothing
# spare.
LIMIT = 60
# The most hosted clusters Clusters Service puts on one management cluster (clustersService.
# provisionShardClusterLimit in config/config.yaml): the PM page's best case fills management clusters up to it.
CLUSTERS_SERVICE_LIMIT = 100
MOST_PER_ZONE = 20  # clusters_that_fit goes up to this many control-plane machines per zone
SECONDS = 60        # the solver's limit for each number of machines
DECIMALS = 3
ROOM = {resource: cp.whole(pc.ROOM[resource], resource) for resource in pc.RESOURCES}  # in whole thousandths
FIELDS = {
    "basis": "reserved: packs what the pods reserve today; used: what they use",
    "full_machines_per_zone": "control-plane machines in each zone of a full management cluster",
    "other_machines": "the machines a management cluster runs besides its control-plane machines",
    "placeholders": "true if full_machines_per_zone and other_machines are this script's placeholders, what the "
                    "49-node run's management cluster had (it wasn't full), rather than numbers given with --full "
                    "and --other",
    "limit": "the most hosted clusters to plan for on one management cluster",
    "clusters_service_limit": "the most hosted clusters Clusters Service puts on one management cluster",
    "room_per_machine": "what one control-plane machine has room for once its system and the helper pods every "
                        "machine runs have taken their share: pack_clusters.py's, or --room's",
    "room_source": "where room_per_machine comes from, if --room gave it; null for pack_clusters.py's",
    "new_management_cluster_cores": "the cores of a full management cluster, control-plane and other machines: quota "
                                    "is planned per management cluster, for all its machines at their largest, so a "
                                    "region that needs another one needs all of these at once",
    "clusters_that_fit": "the most clusters of the size that fit on a management cluster with 1, 2, ... control-plane "
                         "machines per zone, by the rules pack_clusters.py follows",
    "not_proven": "for numbers of machines per zone where clusters_that_fit isn't proven to be the most possible: the "
                  "most the machines' room allows in total",
    "clusters": "how many clusters of the size a full management cluster holds: those that fit, at most the limit",
    "runs_out_of": "the resource clusters of the size run out of first on a full management cluster",
    "runs_out_of_by_machines": "runs_out_of for 1, 2, ... control-plane machines per zone, like clusters_that_fit",
    "limited_by": "what stops more clusters of the size going on a full management cluster: runs_out_of, or the "
                  "limit",
    "own_cores": "one cluster's own share of a full management cluster's control-plane machines: their cores over "
                 "the clusters that fit",
    "shared_cores": "one cluster's share of what's there for all the clusters: the machines a management cluster runs "
                    "besides its control-plane machines, and any control-plane room the limit leaves unused",
    "cores": "own_cores and shared_cores added up: a full management cluster's cores over the clusters it holds",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--basis", choices=("reserved", "used"), default="reserved",
                        help="pack what the pods reserve today, or what they use (default: reserved)")
    parser.add_argument("--full", type=int, default=FULL_PER_ZONE, metavar="N",
                        help=f"control-plane machines per zone on a full management cluster (default: {FULL_PER_ZONE})")
    parser.add_argument("--other", nargs="+", type=other_pool, metavar="POOL=MACHINESxCORES",
                        default=OTHER_MACHINES,
                        help="the machines a management cluster runs besides its control-plane machines (default: "
                             + " ".join(f"{pool}={machines}x{cores}" for pool, machines, cores in OTHER_MACHINES) + ")")
    parser.add_argument("--limit", type=int, default=LIMIT, metavar="N",
                        help=f"the most hosted clusters to plan for on one management cluster, at most Clusters "
                             f"Service's {CLUSTERS_SERVICE_LIMIT} (default: {LIMIT})")
    parser.add_argument("--most", type=int, default=MOST_PER_ZONE, metavar="N",
                        help=f"work out clusters_that_fit up to this many machines per zone (default: {MOST_PER_ZONE})")
    parser.add_argument("--seconds", type=float, default=SECONDS, metavar="S",
                        help=f"the solver's limit for each number of machines (default: {SECONDS})")
    parser.add_argument("--pods", default=pc.DEFAULT_PODS,
                        help="pods per t-shirt size from size_pods.py (default: size-pods.json next to this script)")
    parser.add_argument("--room", metavar="FILE",
                        help="what one control-plane machine has room for, as JSON with cpu_cores, memory_gib, "
                             "swift_nics and pods (in whole thousandths), and an optional source, such as a "
                             "production machine's allocatable room less its daemonsets' requests; production's are "
                             "private, so keep the file out of this repo (default: what pack_clusters.py works out)")
    parser.add_argument("--output",
                        help="file to write (default: cores-per-cluster.json, or cores-per-cluster-used.json with "
                             "--basis used, next to this script)")
    args = parser.parse_args()
    if not 1 <= args.full <= args.most:
        parser.error(f"--full must be between 1 and --most ({args.most})")
    if not 1 <= args.limit <= CLUSTERS_SERVICE_LIMIT:
        parser.error(f"--limit must be between 1 and Clusters Service's limit ({CLUSTERS_SERVICE_LIMIT})")
    return args


def other_pool(text):
    """(pool, machines, cores each) from POOL=MACHINESxCORES, such as infra=3x32."""
    pool, _, count = text.partition("=")
    machines, _, cores = count.partition("x")
    if not pool or not machines.isdigit() or not cores.isdigit():
        raise argparse.ArgumentTypeError(f"{text}: give it as POOL=MACHINESxCORES, such as infra=3x32")
    return pool, int(machines), int(cores)


def use_room(path):
    """Replaces the room per machine with --room's, everywhere it's used, and returns its source."""
    try:
        with open(path) as f:
            data = json.load(f)
        room = {resource: float(data[resource]) for resource in pc.RESOURCES}
    except (OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"couldn't read the room per machine from {path}: {error}")
    if any(value <= 0 for value in room.values()):
        sys.exit(f"{path}: the room per machine must be more than 0 of everything")
    pc.ROOM.update(room)
    ROOM.update({resource: cp.whole(room[resource], resource) for resource in pc.RESOURCES})
    return data.get("source")


def load_sizes(path, basis):
    """[(size, nodes_from, nodes_to, [pod])] from size-pods.json, in size order."""
    try:
        with open(path) as f:
            data = json.load(f)
        return [(size["size"], size["nodes_from"], size["nodes_to"], size[basis]["pods"]) for size in data["sizes"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"couldn't read the {basis} pods in {path}: {error}; rerun size_pods.py")


def components(size, pods):
    """[(component, copies, {resource: what one copy needs, in whole thousandths})]. The solver spreads the copies of
    a component with more than one copy over the zones, so it stops at one that doesn't run one copy per zone."""
    out = []
    for pod in pods:
        if pod["copies"] not in range(1, pc.ZONES + 1):
            sys.exit(f"{size}'s {pod['component']} has {pod['copies']} copies; this handles 1 to {pc.ZONES}")
        if pod["copies"] > 1 and not pod["one_per_zone"]:
            sys.exit(f"{size}'s {pod['component']} has {pod['copies']} copies that don't run one per zone, which this "
                     "doesn't handle")
        out.append((pod["component"], pod["copies"], {r: cp.whole(v, r) for r, v in cp.needs(pod).items()}))
    return out


def room_allows(comps, per_zone):
    """The most clusters the machines' room allows in total: for each resource, all the machines' room over what one
    cluster needs, and each zone's room over what one cluster's copies in every zone need."""
    total = {r: sum(copies * need[r] for _, copies, need in comps) for r in pc.RESOURCES}
    every_zone = {r: sum(need[r] for _, copies, need in comps if copies == pc.ZONES) for r in pc.RESOURCES}
    return min([pc.ZONES * per_zone * ROOM[r] // total[r] for r in pc.RESOURCES if total[r]]
               + [per_zone * ROOM[r] // every_zone[r] for r in pc.RESOURCES if every_zone[r]])


def solve(comps, per_zone, start, highest, seconds):
    """(proven, packing): the most clusters, from start's to highest, that fit on per_zone machines per zone,
    starting the search from the packing `start`, or (False, None) if the solver finds none in time. A packing is
    {"clusters": how many, "machines": for each zone, for each machine, how many copies of each component it
    runs}."""
    from ortools.sat.python import cp_model
    model = cp_model.CpModel()
    clusters = model.NewIntVar(start["clusters"] if start else 0, highest, "clusters")
    on = {(c, zone, machine): model.NewIntVar(0, highest, "")
          for c in range(len(comps)) for zone in range(pc.ZONES) for machine in range(per_zone)}
    for c, (_, copies, _) in enumerate(comps):
        in_zone = [sum(on[c, zone, machine] for machine in range(per_zone)) for zone in range(pc.ZONES)]
        if copies == pc.ZONES:  # one copy of every cluster in every zone
            for count in in_zone:
                model.Add(count == clusters)
        elif copies > 1:  # each cluster's copies in different zones: at most one per cluster in a zone
            for count in in_zone:
                model.Add(count <= clusters)
            model.Add(sum(in_zone) == copies * clusters)
        else:
            model.Add(sum(in_zone) == clusters)
    for zone in range(pc.ZONES):
        memory = []
        for machine in range(per_zone):
            for resource in pc.RESOURCES:
                load = sum(need[resource] * on[c, zone, machine]
                           for c, (_, _, need) in enumerate(comps) if need[resource])
                model.Add(load <= ROOM[resource])
                if resource == "memory_gib":
                    memory.append(load)
        for fuller, emptier in zip(memory, memory[1:]):
            model.Add(fuller >= emptier)  # a zone's machines are interchangeable, so take them fullest first
    if start:  # start from the packing found for fewer machines, in the order the rule above takes them
        model.AddHint(clusters, start["clusters"])
        for zone in range(pc.ZONES):
            fullest_first = sorted(start["machines"][zone], reverse=True, key=lambda counts: sum(
                counts[c] * comps[c][2]["memory_gib"] for c in range(len(comps))))
            for machine, counts in enumerate(fullest_first):
                for c, count in enumerate(counts):
                    model.AddHint(on[c, zone, machine], count)
    model.Maximize(clusters)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return False, None
    machines = [[[solver.Value(on[c, zone, machine]) for c in range(len(comps))] for machine in range(per_zone)]
                for zone in range(pc.ZONES)]
    return status == cp_model.OPTIMAL, {"clusters": solver.Value(clusters), "machines": machines}


def side_by_side(one, other):
    """Two packings on their own machines, as one."""
    return {"clusters": one["clusters"] + other["clusters"],
            "machines": [[list(counts) for counts in one["machines"][zone] + other["machines"][zone]]
                         for zone in range(pc.ZONES)]}


def most_clusters(comps, most, seconds):
    """{machines per zone: (packing, most the room allows, proven)} for 1 to `most` machines per zone."""
    found = {}
    for per_zone in range(1, most + 1):
        allows = room_allows(comps, per_zone)
        pairs = [(found[part][0], found[per_zone - part][0]) for part in range(1, per_zone // 2 + 1)]
        best = max((side_by_side(*pair) for pair in pairs), key=lambda packing: packing["clusters"], default=None)
        if best is None or best["clusters"] < allows:
            proven, packing = solve(comps, per_zone, best, allows, seconds)
            if packing is not None and (best is None or packing["clusters"] >= best["clusters"]):
                best = packing
            else:
                proven = False
        else:
            proven = True
        if best is None:
            sys.exit(f"the solver found no packing for {per_zone} machine{'s' if per_zone > 1 else ''} per zone")
        found[per_zone] = (best, allows, proven or best["clusters"] == allows)
    return found


def spread_out(size, comps, packing):
    """The packing as pack_clusters.pack() returns it: each copy of each cluster on its own machine."""
    count = packing["clusters"]
    zones = [[{"zone": zone + 1, "pods": []} for _ in packing["machines"][zone]] for zone in range(pc.ZONES)]
    for c, (component, copies, _) in enumerate(comps):
        slots = [[machine for machine, counts in enumerate(packing["machines"][zone]) for _ in range(counts[c])]
                 for zone in range(pc.ZONES)]
        if copies == pc.ZONES:
            if any(len(spots) != count for spots in slots):
                sys.exit(f"{size}'s {component}: the packing's copies can't be split between {count} clusters")
            places = [[(zone, slots[zone][number]) for zone in range(pc.ZONES)] for number in range(count)]
        elif copies == 2:
            # With n clusters and a, b and c copies in the zones (a + b + c = 2n, none over n), n - c clusters take
            # zones 1 and 2, n - b zones 1 and 3, and n - a zones 2 and 3.
            if sum(len(spots) for spots in slots) != 2 * count or any(len(spots) > count for spots in slots):
                sys.exit(f"{size}'s {component}: the packing's copies can't be split between {count} clusters")
            pairs = ([(0, 1)] * (count - len(slots[2])) + [(0, 2)] * (count - len(slots[1]))
                     + [(1, 2)] * (count - len(slots[0])))
            taken, places = [0] * pc.ZONES, []
            for pair in pairs:
                places.append([(zone, slots[zone][taken[zone]]) for zone in pair])
                for zone in pair:
                    taken[zone] += 1
        else:
            places = [[spot] for spot in [(zone, machine) for zone in range(pc.ZONES) for machine in slots[zone]]]
        if len(places) != count:
            sys.exit(f"{size}'s {component}: the packing's copies can't be split between {count} clusters")
        for number, place in enumerate(places, 1):
            for zone, machine in place:
                zones[zone][machine]["pods"].append((f"{size} {number}", component))
    return zones


def fullest(comps, packing, per_zone):
    """The resource the packing's clusters use the largest share of."""
    used = {r: sum(counts[c] * comps[c][2][r] for zone in packing["machines"] for counts in zone
                   for c in range(len(comps))) for r in pc.RESOURCES}
    return max(pc.RESOURCES, key=lambda r: used[r] / (pc.ZONES * per_zone * ROOM[r]))


def main():
    args = parse_args()
    try:
        import ortools  # noqa: F401
    except ImportError:
        sys.exit("this needs OR-Tools; use the scheduling simulator's environment (../scheduling-simulator/venv)")
    room_source = use_room(os.path.expanduser(args.room)) if args.room else None
    other_cores = sum(machines * cores for _, machines, cores in args.other)
    full_cores = pc.ZONES * args.full * pc.MACHINE_CORES
    sizes, broken = [], 0
    for size, nodes_from, nodes_to, pods in load_sizes(os.path.expanduser(args.pods), args.basis):
        comps = components(size, pods)
        found = most_clusters(comps, args.most, args.seconds)
        for per_zone, (packing, _, _) in found.items():
            problems = cp.broken_rules({size: packing["clusters"]}, spread_out(size, comps, packing), {size: pods})
            problems += [f"zone {zone + 1} has {len(machines)} machines, not {per_zone}"
                         for zone, machines in enumerate(packing["machines"]) if len(machines) != per_zone]
            if problems:
                broken += 1
                print(f"{size}, {per_zone} machines per zone: {'; '.join(problems[:3])}", file=sys.stderr)
        packing = found[args.full][0]
        fit = packing["clusters"]
        clusters = min(fit, args.limit)
        runs_out_of = fullest(comps, packing, args.full)
        sizes.append({
            "size": size,
            "nodes_from": nodes_from,
            "nodes_to": nodes_to,
            "clusters": clusters,
            "runs_out_of": runs_out_of,
            "limited_by": "limit" if clusters < fit else runs_out_of,
            "own_cores": round(full_cores / fit, DECIMALS),
            "shared_cores": round((full_cores + other_cores) / clusters - full_cores / fit, DECIMALS),
            "cores": round((full_cores + other_cores) / clusters, DECIMALS),
            "clusters_that_fit": [found[per_zone][0]["clusters"] for per_zone in range(1, args.most + 1)],
            "runs_out_of_by_machines": [fullest(comps, found[per_zone][0], per_zone)
                                        for per_zone in range(1, args.most + 1)],
            "not_proven": {str(per_zone): allows for per_zone, (_, allows, proven) in found.items() if not proven},
        })
    if broken:
        sys.exit(f"{broken} packings broke a rule, so nothing was written")
    placeholders = args.full == FULL_PER_ZONE and args.other == OTHER_MACHINES
    output = {
        "description": "The cores one hosted cluster of each t-shirt size takes on a full management cluster, written "
                       "by hack/hcp-scale-analysis/cores_per_cluster.py from size-pods.json."
                       + (" The settings are placeholders to confirm." if placeholders else ""),
        "basis": args.basis,
        "settings": {
            "machine_cores": pc.MACHINE_CORES,
            "zones": pc.ZONES,
            "full_machines_per_zone": args.full,
            "other_machines": [{"pool": pool, "machines": machines, "cores": cores}
                               for pool, machines, cores in args.other],
            "placeholders": placeholders,
            "limit": args.limit,
            "clusters_service_limit": CLUSTERS_SERVICE_LIMIT,
            "room_per_machine": pc.ROOM,
            "room_source": room_source,
        },
        "new_management_cluster_cores": full_cores + other_cores,
        "fields": FIELDS,
        "sizes": sizes,
    }
    target = os.path.expanduser(args.output or DEFAULT_OUTPUT[args.basis])
    with open(target, "w") as f:
        f.write(json.dumps(output, indent=2) + "\n")
    print(f"wrote {target}", file=sys.stderr)

    others = " and ".join(f"{machines} {pool} machine{'s' if machines != 1 else ''} with {cores} cores"
                          for pool, machines, cores in args.other)
    print(f"A full management cluster ({args.basis} pods): {args.full} control-plane machines per zone "
          f"({full_cores} cores), plus {others} ({other_cores} cores), at most {args.limit} hosted clusters:")
    print(f"{'size':>8} {'clusters':>9} {'own cores':>10} {'shared':>7} {'cores each':>11}  limited by")
    for size in sizes:
        limited_by = pc.RESOURCE_NAMES.get(size["limited_by"], f"the limit of {args.limit}")
        print(f"{size['size']:>8} {size['clusters']:9d} {size['own_cores']:10.2f} {size['shared_cores']:7.2f} "
              f"{size['cores']:11.2f}  {limited_by}")
    print(f"Clusters that fit, for 1 to {args.most} control-plane machines per zone (* not proven the most possible):")
    for size in sizes:
        print(f"{size['size']:>8} " + " ".join(f"{count}{'*' if str(per_zone) in size['not_proven'] else ''}"
                                               for per_zone, count in enumerate(size['clusters_that_fit'], 1)))
    print(f"Quota is planned per management cluster: a region that needs another one needs all its "
          f"{output['new_management_cluster_cores']} cores at once.")
    print(f"Checked: all {len(sizes) * args.most} packings follow the rules.")


if __name__ == "__main__":
    main()
