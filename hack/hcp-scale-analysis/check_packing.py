#!/usr/bin/env python3
"""Checks pack_clusters.py: that its packings follow the rules, and how close they come to the fewest machines.

For every size alone (1 to --most clusters), for random mixes of small and medium clusters, and for random
mixes of every size, it packs the clusters with pack_clusters.py and then:
- checks the packing on its own: every copy of every component placed once, on machines of their own and
  in different zones when they must be, and no machine given more than its room;
- works out the fewest machines that any packing following the same rules can use, with an exact solver
  (OR-Tools' CP-SAT), and compares. The solver works in whole thousandths of a core and of a GiB, as precise
  as size-pods.json and pack_clusters.py's ROOM, so it does the same arithmetic as the packing. The packing
  itself is one answer the solver must consider, so the solver finding none, or fewer machines than the
  packing can't beat, means the two disagree, which it reports as a problem. The solver stops after --seconds
  per case; a case it can't finish by then is counted apart.

It prints how many packings broke a rule or disagreed with the solver (there should be none), how many needed
the fewest machines, and by how much the others went over. Rerun it after changing pack_clusters.py.

Usage:
  python3 check_packing.py [--basis reserved|used] [--most N] [--mixes N] [--seconds S]

Requires OR-Tools for the comparison, which the scheduling simulator's environment has; without it, it only
checks the rules.
"""
import argparse
import random
import sys
from collections import Counter, defaultdict

import pack_clusters as pc

SCALE = {"cpu_cores": 1000, "memory_gib": 1000, "swift_nics": 1, "pods": 1}  # to whole thousandths


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--basis", choices=("reserved", "used"), default="reserved",
                        help="pack what the pods reserve today, or what they use (default: reserved)")
    parser.add_argument("--most", type=int, default=15, help="most clusters of each size alone (default: 15)")
    parser.add_argument("--mixes", type=int, default=40,
                        help="random mixes of each kind; seeded, so the same every time (default: 40)")
    parser.add_argument("--seconds", type=float, default=30, help="the solver's limit per case (default: 30)")
    return parser.parse_args()


def needs(pod):
    return {"cpu_cores": pod["cpu_cores"], "memory_gib": pod["memory_gib"], "swift_nics": pod["swift_nics"], "pods": 1}


def broken_rules(mix, zones, sizes):
    """What's wrong with a packing, worked out from size-pods.json rather than from what the packing recorded."""
    problems, placed = [], defaultdict(list)
    pods = {size: {pod["component"]: pod for pod in size_pods} for size, size_pods in sizes.items()}
    for zone, machines in enumerate(zones):
        for machine in machines:
            total = Counter()
            for cluster, component in machine["pods"]:
                total.update(needs(pods[cluster.split()[0]][component]))
                placed[(cluster, component)].append((zone, id(machine)))
            problems += [f"a machine in zone {zone + 1} is given {total[resource]:.3f} {resource}, more than its "
                         f"{pc.ROOM[resource]:.3f}" for resource in pc.RESOURCES
                         if total[resource] > pc.ROOM[resource] + 1e-6]
    for size, count in mix.items():
        for number in range(1, count + 1):
            for pod in sizes[size]:
                spots = placed.pop((f"{size} {number}", pod["component"]), [])
                if len(spots) != pod["copies"]:
                    problems.append(f"{size} {number}'s {pod['component']} has {len(spots)} copies, "
                                    f"not {pod['copies']}")
                if pod["one_per_zone"] and len({zone for zone, _ in spots}) != len(spots):
                    problems.append(f"{size} {number}'s {pod['component']} has copies in the same zone")
                if len({machine for _, machine in spots}) != len(spots):
                    problems.append(f"{size} {number}'s {pod['component']} has copies on the same machine")
    problems += [f"{cluster}'s {component} isn't one of the clusters' pods" for cluster, component in placed]
    return problems


def whole(value, resource):
    """A number of cores, GiB, NICs or pods in whole thousandths (whole NICs and pods); stops if it isn't one."""
    scaled = round(value * SCALE[resource])
    if abs(value * SCALE[resource] - scaled) > 1e-6:
        sys.exit(f"{value} {resource} isn't a whole number of thousandths; the solver can't match the packing")
    return scaled


def fewest(mix, sizes, most_per_zone, seconds):
    """(status, fewest machines possible): the fewest machines that any packing by the same rules can use for the
    mix, with at most most_per_zone machines in each zone. status is "proven", "unproven" (out of time), or
    "none" (no packing found within the limit)."""
    from ortools.sat.python import cp_model
    model = cp_model.CpModel()
    room = {resource: whole(pc.ROOM[resource], resource) for resource in pc.RESOURCES}
    machines = [(zone, number) for zone in range(pc.ZONES) for number in range(most_per_zone)]
    used = {machine: model.NewBoolVar("") for machine in machines}
    for zone, number in machines:
        if number + 1 < most_per_zone:
            model.Add(used[zone, number] >= used[zone, number + 1])  # use a zone's machines in order
    load = defaultdict(list)
    for size, count in mix.items():
        for _ in range(count):
            for pod in sizes[size]:
                need = {resource: whole(value, resource) for resource, value in needs(pod).items()}
                on = {machine: model.NewBoolVar("") for machine in machines}  # a copy on each machine, at most
                model.Add(sum(on.values()) == pod["copies"])
                for zone in range(pc.ZONES):
                    model.Add(sum(on[zone, number] for number in range(most_per_zone))
                              <= (1 if pod["one_per_zone"] else pod["copies"]))
                for machine, var in on.items():
                    for resource in pc.RESOURCES:
                        if need[resource]:
                            load[machine, resource].append(need[resource] * var)
    for (machine, resource), terms in load.items():
        model.Add(sum(terms) <= room[resource] * used[machine])
    model.Minimize(sum(used.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    if status == cp_model.OPTIMAL:
        return "proven", round(solver.ObjectiveValue())
    if status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
        return "none", None
    return "unproven", None


def cases(sizes, most, mixes):
    """[(kind, mix)]: every size alone, then random mixes of small and medium, then of every size."""
    out = [("one size", {size: count}) for size in sizes for count in range(1, most + 1)]
    rng = random.Random(1)
    small_and_medium = [size for size in ("small", "medium") if size in sizes]
    for _ in range(mixes):
        out.append(("small and medium", {size: rng.randint(1, most) for size in small_and_medium}))
    for _ in range(mixes):
        chosen = rng.sample(list(sizes), rng.randint(2, len(sizes)))
        out.append(("every size", {size: rng.randint(1, 8) for size in chosen}))
    return out


def main():
    args = parse_args()
    sizes = pc.load_sizes(pc.DEFAULT_PODS, args.basis)
    try:
        import ortools  # noqa: F401
        compare = True
    except ImportError:
        print("OR-Tools isn't installed, so this only checks the rules", file=sys.stderr)
        compare = False
    broken, disagree = 0, 0
    tally = defaultdict(lambda: {"cases": 0, "fewest": 0, "over": [], "unproven": 0})
    for kind, mix in cases(sizes, args.most, args.mixes):
        zones = pc.pack(mix, sizes)
        problems = broken_rules(mix, zones, sizes)
        if problems:
            broken += 1
            print(f"{mix}: {'; '.join(problems[:3])}")
        counts = tally[kind]
        counts["cases"] += 1
        if not compare:
            continue
        machines = sum(len(zone) for zone in zones)
        # A component with a copy in every zone needs a machine in each, so a packing with no more machines than this
        # one has at most machines - 2 in any zone; capping each zone there leaves out no such packing.
        every_zone = any(pod["one_per_zone"] and pod["copies"] == pc.ZONES for size in mix for pod in sizes[size])
        status, lowest = fewest(mix, sizes, machines - (pc.ZONES - 1 if every_zone else 0), args.seconds)
        if status == "unproven":
            counts["unproven"] += 1
        elif status == "none" or machines < lowest:
            disagree += 1
            print(f"{mix}: the packing used {machines} machines, but the solver "
                  + ("found no packing" if status == "none" else f"says it takes at least {lowest}"))
        elif machines == lowest:
            counts["fewest"] += 1
        else:
            counts["over"].append(machines - lowest)
    print(f"Packings that broke a rule: {broken}")
    if compare:
        print(f"Packings the solver disagreed with: {disagree}")
        for kind, counts in tally.items():
            over = Counter(counts["over"])
            print(f"{kind}: {counts['cases']} cases, the fewest machines in {counts['fewest']}"
                  + (", more in " + ", ".join(f"{n} (by {by})" for by, n in sorted(over.items())) if over else "")
                  + (f", {counts['unproven']} the solver couldn't finish" if counts["unproven"] else ""))
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
