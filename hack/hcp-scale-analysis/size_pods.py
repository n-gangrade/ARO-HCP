#!/usr/bin/env python3
"""The pods one hosted cluster of each t-shirt size runs, with each copy's CPU, memory and SWIFT NICs.

Reads the data file written by combine_usage.py (container-usage.json next to this script by default)
and HyperShift's sizing file (hypershiftoperator/deploy/templates/cluster.clustersizingconfiguration.yaml),
and writes size-pods.json next to this script: for each t-shirt size, every pod (component) a hosted
cluster runs, how many copies, and what one copy needs, in two versions. It's the input for simulating
how a customer's clusters pack onto management-cluster machines.

Sizes: the t-shirt sizes stage and prod use (SIZE_CLASSES), which HyperShift picks by a hosted
cluster's total node count, infra nodes as well as workers. They're the sizing file's {{ else }} part,
as stage and prod don't set limitClusterSizes.

reserved: what each copy reserves (its containers' resource requests) in a cluster on the
OpenShift version of the newest test run with reservations, which they come from, as its pods are the
closest to today's (RESERVED_FROM picks another run). Each size gets the sizing file's settings for it
instead of the run's own size's. The settings in SETTINGS_LEFT_OUT are left out, as no test run got
them: the network operator, not HyperShift, creates those pods. Containers no test run had (NOT_TESTED:
the cluster autoscaler) aren't included; the output lists them with the sizing file's settings. If the
run had no router, the router comes from the newest run that had one. It stops if the sizing file sets
anything for a container the run doesn't have and NOT_TESTED doesn't list, which catches a misspelled
name in any size. Two checks warn if this way of working them out doesn't hold: every run should have
got its size's settings, and each run, worked out the same way from another small run on its OpenShift
version, should come out as recorded.

used: what each copy uses: its containers' p95 of all copies' readings, added up (see
component_requests.py), which is what clusters could reserve if their reservations matched their use.
Each size uses the largest test cluster within its range, preferring runs whose trust is good, as the
largest is closest to the size's biggest clusters. A size without a test run uses the next larger
size's run and is marked as borrowed. CHOSEN_RUNS picks a specific run for a size instead. Every
cluster also gets a router, measured in the largest test run that had one, as production clusters run
one.

In both, each router copy needs a SWIFT NIC (the network card a pod uses to reach the customer's
network), and a component with more than one copy runs one copy per availability zone (one_per_zone),
as HyperShift requires on Azure. The two login components the scheduling simulator adds
(oauth-openshift and openshift-oauth-apiserver) are left out, as production's clusters don't run them.
It stops if no run had a router, or if a component has more copies than there are zones.

Usage:
  python3 size_pods.py [--data FILE] [--sizing FILE] [--output FILE]

Requires PyYAML.
"""
import argparse
import json
import math
import os
import sys

import yaml

from component_requests import DEFAULT_DATA, component_requests

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "size-pods.json")
DEFAULT_SIZING = os.path.normpath(os.path.join(HERE, "..", "..", "hypershiftoperator", "deploy", "templates",
                                               "cluster.clustersizingconfiguration.yaml"))
# The t-shirt sizes stage and prod use: (name, lowest and highest total node count), from the sizing file. None means
# no upper limit.
SIZE_CLASSES = [("small", 0, 60), ("medium", 61, 120), ("large", 121, 252), ("xlarge", 253, 360),
                ("xxlarge", 361, None)]
# The test run a size's used pods come from, instead of the largest one within its range,
# e.g. {"large": "120-node-aro-hcp"}.
CHOSEN_RUNS = {}
# The test run the reserved pods come from, instead of the newest one with reservations, e.g. "30-node-aro-hcp".
RESERVED_FROM = None
# The sizing file's settings that no test run got: whatever its size, every run reserved 200 MiB per copy for
# ovnkube-control-plane and 50 MiB for multus-admission-controller, as the network operator, not HyperShift, creates
# them, and HyperShift applies the sizing file only to the pods it creates. Checked each time this runs.
SETTINGS_LEFT_OUT = {("ovnkube-control-plane", "ovnkube-control-plane"),
                     ("multus-admission-controller", "multus-admission-controller")}
# Containers the sizing file has settings for that no test run had, with how many copies they run: the cluster
# autoscaler, which 1 of the 12 hosted clusters on the 49-node run's management cluster ran, with 1 copy. The reserved
# pods don't include them; not_included lists them.
NOT_TESTED = {("cluster-autoscaler", "cluster-autoscaler"): 1}
ROUTER = "router"
SWIFT_NICS_PER_COPY = {ROUTER: 1}
ZONES = 3  # availability zones a management cluster's machines are spread over
DECIMALS = 3
MEMORY_UNITS = {"Ki": 2 ** -20, "Mi": 2 ** -10, "Gi": 1, "Ti": 2 ** 10}  # a memory quantity's suffix: GiB in one
FIELDS = {
    "reserved_from": "the test run the reserved pods come from, the newest one with reservations",
    "settings_left_out": "the sizing file's settings the reserved pods leave out, as no test run got them: the network "
                         "operator, not HyperShift, creates these pods",
    "nodes_from": "the size's lowest total node count, infra nodes as well as workers",
    "nodes_to": "the size's highest total node count; null means no upper limit",
    "reserved": "what one copy of each component reserves in a cluster on reserved_from's OpenShift version: "
                "its containers' resource requests in that run, with the sizing file's settings for this size",
    "not_included": "containers no test run had, so the reserved pods don't include them: how many copies they run, "
                    "and the sizing file's settings for this size, per copy; null where it sets none, as what they "
                    "reserve then isn't known",
    "used": "what one copy of each component uses: its containers' p95 of all copies' readings, added up",
    "run": "the test run the used pods come from",
    "run_total_nodes": "that run's total node count",
    "trust": "how much to trust that run: good, caveats or low (see its notes in container-usage.json)",
    "borrowed": "true when no test run falls within the size, so it uses the next larger size's run",
    "router_from": "the test run the router's numbers come from, if not the run the other pods come from",
    "totals": "one cluster's pods, CPU in cores, memory in GiB and SWIFT NICs, with every copy added up",
    "copies": "how many copies (pods) of the component one cluster runs",
    "cpu_cores": "CPU one copy reserves or uses, in cores",
    "memory_gib": "memory one copy reserves or uses, in GiB",
    "swift_nics": "SWIFT NICs one copy needs",
    "one_per_zone": "whether the copies run in different availability zones, one per zone",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="data file from combine_usage.py (default: container-usage.json next to this script)")
    parser.add_argument("--sizing", default=DEFAULT_SIZING,
                        help="production's sizing file (default: the HyperShift operator's, in this repository)")
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
    unknown_sizes = sorted(set(CHOSEN_RUNS) - {name for name, _, _ in SIZE_CLASSES})
    if unknown_sizes:
        sys.exit(f"CHOSEN_RUNS names sizes that SIZE_CLASSES doesn't have: {', '.join(unknown_sizes)}")
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
    """(the run, what one router copy needs) from the largest run that had a router."""
    with_router = [run for run in runs if ROUTER in component_requests(run)]
    if not with_router:
        sys.exit("no test run had a router, so the clusters' SWIFT NICs are unknown")
    run = max(with_router, key=lambda run: run["total_nodes"])
    return run, component_requests(run)[ROUTER]


def pods_for(run, router_run, router):
    """[pod], biggest first by memory, and the router's source run if it isn't this run."""
    components = component_requests(run)
    router_from = None
    if ROUTER not in components:
        components[ROUTER] = router
        router_from = router_run["run"]
    return as_pods(components), router_from


def as_pods(components):
    """[pod], biggest first by memory, from {component: {"copies", "cpu_cores", "memory_gib"} for one copy}. A component
    with more than one copy runs one per zone, so it stops if one has more copies than there are zones."""
    crowded = sorted(f"{name} ({needs['copies']})" for name, needs in components.items() if needs["copies"] > ZONES)
    if crowded:
        sys.exit(f"these components have more copies than the {ZONES} zones, so they can't run one per zone: "
                 f"{', '.join(crowded)}")
    pods = [{
        "component": name,
        "copies": needs["copies"],
        "cpu_cores": round(needs["cpu_cores"], DECIMALS),
        "memory_gib": round(needs["memory_gib"], DECIMALS),
        "swift_nics": SWIFT_NICS_PER_COPY.get(name, 0),
        "one_per_zone": needs["copies"] > 1,
    } for name, needs in components.items()]
    pods.sort(key=lambda pod: (-pod["memory_gib"] * pod["copies"], pod["component"]))
    return pods


def totals(pods):
    """One cluster's pods, cores, GiB and SWIFT NICs."""
    return {
        "pods": sum(pod["copies"] for pod in pods),
        "cpu_cores": round(sum(pod["copies"] * pod["cpu_cores"] for pod in pods), DECIMALS),
        "memory_gib": round(sum(pod["copies"] * pod["memory_gib"] for pod in pods), DECIMALS),
        "swift_nics": sum(pod["copies"] * pod["swift_nics"] for pod in pods),
    }


def size_of(run):
    """The t-shirt size of a run's test cluster."""
    return next(name for name, low, high in SIZE_CLASSES if within(run, low, high))


def newest(run):
    return run["date"] or "", run["total_nodes"], run["run"]


def gib(quantity):
    """A Kubernetes memory quantity, such as 500Mi, in GiB."""
    for suffix, scale in MEMORY_UNITS.items():
        if str(quantity).endswith(suffix):
            return float(str(quantity).removesuffix(suffix)) * scale
    sys.exit(f"the sizing file has a memory quantity this script doesn't handle: {quantity}")


def cores(quantity):
    """A Kubernetes CPU quantity, such as 100m, in cores."""
    quantity = str(quantity)
    return float(quantity.removesuffix("m")) / 1000 if quantity.endswith("m") else float(quantity)


def sizing_settings(path):
    """{size: {(component, container): {"memory_gib" and/or "cpu_cores": for one copy}}}: what the sizing file sets
    for each size. Stage and prod don't set limitClusterSizes, so they use the file's {{ else }} part."""
    try:
        with open(path) as f:
            text = f.read()
        if "{{ else }}" in text:
            text = text.split("{{ else }}", 1)[1].split("{{ end }}", 1)[0]
        sizes = {size["name"]: size for size in yaml.safe_load(text)["spec"]["sizes"]}
    except (OSError, KeyError, TypeError, yaml.YAMLError) as error:
        sys.exit(f"couldn't read the sizes in {path}: {error}")
    settings = {}
    for name, low, high in SIZE_CLASSES:
        criteria = sizes.get(name, {}).get("criteria", {})
        if name not in sizes or criteria.get("from") != low or (high is not None and criteria.get("to") != high):
            sys.exit(f"{path} doesn't have the {name} size as SIZE_CLASSES has it")
        settings[name] = {}
        for request in sizes[name].get("effects", {}).get("resourceRequests", []):
            setting = {}
            if "memory" in request:
                setting["memory_gib"] = gib(request["memory"])
            if "cpu" in request:
                setting["cpu_cores"] = cores(request["cpu"])
            settings[name][(request["deploymentName"], request["containerName"])] = setting
    return settings


def reservation_base(runs):
    """The run the reserved pods come from: RESERVED_FROM, or the newest run with reservations, as its pods are
    the closest to today's."""
    candidates = [run for run in runs if run.get("reservations")]
    if RESERVED_FROM:
        candidates = [run for run in candidates if run["run"] == RESERVED_FROM]
        if not candidates:
            sys.exit(f"RESERVED_FROM is {RESERVED_FROM}, which isn't in the data file or has no reservations")
    if not candidates:
        sys.exit("no test run in the data file has reservations; rerun combine_usage.py")
    base = max(candidates, key=newest)
    latest = max(runs, key=newest)
    if not RESERVED_FROM and latest is not base:
        print(f"warning: the newest run, {latest['run']}, has no reservations, so the reserved pods come from "
              f"{base['run']}", file=sys.stderr)
    return base


def recorded_containers(run):
    """{(component, container): what one copy reserved, and its copies} for a run's containers."""
    return {(container["component"], container["container"]): container["reserved"]
            for container in run["containers"] if container["reserved"]}


def check_names(base, settings):
    """Stop if the sizing file sets anything for a container the base run doesn't have, unless NOT_TESTED lists it, or
    if NOT_TESTED lists a container the sizing file sets nothing for. That catches a misspelled name in any size,
    including those no test run is."""
    have = recorded_containers(base)
    missing = sorted({f"{component} / {container} ({size})" for size, size_settings in settings.items()
                      for component, container in size_settings
                      if (component, container) not in have and (component, container) not in NOT_TESTED})
    if missing:
        sys.exit(f"the sizing file sets resources for containers {base['run']} doesn't have: {', '.join(missing)}. Fix "
                 "the name if it's misspelled, or add the container to NOT_TESTED if no test run had it")
    unset = sorted(f"{component} / {container}" for component, container in NOT_TESTED
                   if not any((component, container) in size_settings for size_settings in settings.values()))
    if unset:
        sys.exit(f"NOT_TESTED lists containers the sizing file sets nothing for: {', '.join(unset)}")


def reserved_router(runs, base):
    """({(component, container): what one copy reserved, and its copies} for the router's containers, the run they come
    from): none if the base run had a router, or else the newest run with reservations that had one. Stops if no run
    did, as the router's copies decide the clusters' SWIFT NICs."""
    def router_of(run):
        return {key: reserved for key, reserved in recorded_containers(run).items() if key[0] == ROUTER}

    if router_of(base):
        return {}, None
    donors = [run for run in runs if run.get("reservations") and router_of(run)]
    if not donors:
        sys.exit("no test run with reservations had a router, so the clusters' SWIFT NICs are unknown")
    donor = max(donors, key=newest)
    return router_of(donor), donor["run"]


def reserved_containers(run, size, settings):
    """({(component, container): {"copies", "memory_gib", "cpu_cores"}}, [not included]): what `run`'s containers
    would reserve in a cluster of `size`. A container the sizing file has a setting for gets the one for `size`
    instead of the one for the run's own size, unless it's in SETTINGS_LEFT_OUT. Not included are the containers in
    NOT_TESTED that the run doesn't have, with their copies and the file's settings for `size`, or null where it
    sets none."""
    own = settings[size_of(run)]
    containers = {}
    for key, reserved in recorded_containers(run).items():
        numbers = {name: reserved[name] for name in ("copies", "memory_gib", "cpu_cores")}
        if key not in SETTINGS_LEFT_OUT:
            if key in settings[size]:
                numbers.update({name: round(value, DECIMALS) for name, value in settings[size][key].items()})
            elif key in own:
                sys.exit(f"{run['run']}'s {key[0]} / {key[1]} has the sizing file's setting for the {size_of(run)} "
                         f"size, which has none for the {size} size, so what it reserves there is unknown; set "
                         "RESERVED_FROM to a smaller run")
        containers[key] = numbers
    not_included = []
    for (component, container), copies in sorted(NOT_TESTED.items()):
        if (component, container) not in containers:
            setting = settings[size].get((component, container), {})
            not_included.append({"component": component, "container": container, "copies": copies,
                                 **{name: round(setting[name], DECIMALS) if name in setting else None
                                    for name in ("memory_gib", "cpu_cores")}})
    return containers, not_included


def pods_from(containers):
    """[pod], biggest first by memory: each component's copies and what one copy reserves, its containers added up."""
    components = {}
    for (component, _), numbers in containers.items():
        pod = components.setdefault(component, {"copies": 0, "cpu_cores": 0.0, "memory_gib": 0.0})
        pod["copies"] = max(pod["copies"], numbers["copies"])
        pod["cpu_cores"] += numbers["cpu_cores"]
        pod["memory_gib"] += numbers["memory_gib"]
    return as_pods(components)


def check_settings(runs, settings):
    """Warn about every sizing file setting a run of that size didn't get, or got although it's in
    SETTINGS_LEFT_OUT. Returns how many."""
    problems = 0
    for run in runs:
        if not run.get("reservations"):
            continue
        size = size_of(run)
        for key, reserved in recorded_containers(run).items():
            setting = settings[size].get(key)
            if setting is None:
                continue
            got = all(math.isclose(reserved[name], round(value, DECIMALS), abs_tol=1e-9)
                      for name, value in setting.items())
            if got == (key in SETTINGS_LEFT_OUT):
                problems += 1
                print(f"warning: {run['run']} ({size}) {'got' if got else 'did not get'} the sizing file's setting for "
                      f"{key[0]} / {key[1]}" + (", although SETTINGS_LEFT_OUT leaves it out" if got else
                                                f": it reserved {reserved['memory_gib']:g} GiB and "
                                                f"{reserved['cpu_cores']:g} cores per copy"), file=sys.stderr)
    return problems


def differs(one, other):
    """Whether two containers' reservations differ; a missing one only differs from one that reserves something."""
    if one is None or other is None:
        present = one or other
        return bool(present["memory_gib"] or present["cpu_cores"])
    return (one["copies"] != other["copies"]
            or not all(math.isclose(one[name], other[name], abs_tol=1e-9) for name in ("memory_gib", "cpu_cores")))


def check_projection(runs, settings):
    """Work out each run's reservations from another small run on the same OpenShift version, the way the reserved
    pods are worked out, and warn about every run that doesn't come out as recorded. Returns (runs checked, runs
    that came out as recorded)."""
    smallest = SIZE_CLASSES[0][0]
    checked, matched = [], []
    for run in runs:
        bases = [other for other in runs if other is not run and other.get("reservations")
                 and other["ocp_version"] == run["ocp_version"] and size_of(other) == smallest]
        if not run.get("reservations") or not bases:
            continue
        base = max(bases, key=newest)
        predicted, _ = reserved_containers(base, size_of(run), settings)
        recorded = recorded_containers(run)
        wrong = [f"{component} / {container}" for component, container in sorted(predicted.keys() | recorded.keys())
                 if differs(predicted.get((component, container)), recorded.get((component, container)))]
        checked.append(run["run"])
        if wrong:
            print(f"warning: {run['run']}'s reservations, worked out from {base['run']}, don't come out as recorded "
                  f"for {', '.join(wrong)}", file=sys.stderr)
        else:
            matched.append(run["run"])
    return checked, matched


def to_json(data):
    """Like json.dumps(data, indent=2), but with each pod on one line, so the file reads like a table."""
    lines = {}

    def placeholder(pod):
        key = f"@pod-{len(lines)}@"
        lines[key] = json.dumps(pod)
        return key

    def compact(version):
        return {**version, "pods": [placeholder(pod) for pod in version["pods"]]}

    sizes = [{**size, "reserved": compact(size["reserved"]), "used": compact(size["used"])} for size in data["sizes"]]
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

    settings = sizing_settings(os.path.expanduser(args.sizing))
    base = reservation_base(runs)
    check_names(base, settings)
    router_containers, reserved_router_from = reserved_router(runs, base)
    setting_problems = check_settings(runs, settings)
    checked, matched = check_projection(runs, settings)
    router_run, router = router_numbers(runs)
    choices = runs_for_sizes(runs)
    sizes = []
    for name, low, high in SIZE_CLASSES:
        containers, not_included = reserved_containers(base, name, settings)
        for key, reserved_per_copy in router_containers.items():
            containers[key] = {number: reserved_per_copy[number] for number in ("copies", "memory_gib", "cpu_cores")}
        reserved = pods_from(containers)
        run, borrowed = choices[name]
        used, router_from = pods_for(run, router_run, router)
        sizes.append({
            "size": name,
            "nodes_from": low,
            "nodes_to": high,
            "reserved": {"totals": totals(reserved), "not_included": not_included, "pods": reserved},
            "used": {
                "run": run["run"],
                "run_total_nodes": run["total_nodes"],
                "trust": run["trust"],
                "borrowed": borrowed,
                "router_from": router_from,
                "totals": totals(used),
                "pods": used,
            },
        })
    output = {
        "description": "The pods one hosted cluster of each t-shirt size runs, written by "
                       "hack/hcp-scale-analysis/size_pods.py from container-usage.json and HyperShift's sizing file: "
                       "what one copy of each component reserves in a cluster on reserved_from's OpenShift "
                       "version (reserved), and what it uses (used, the sum of its containers' p95 of all copies' "
                       "readings).",
        "reserved_from": {"run": base["run"], "size": size_of(base), "ocp_version": base["ocp_version"],
                          "date": base["date"], "router_from": reserved_router_from},
        "settings_left_out": [f"{component} / {container}" for component, container in sorted(SETTINGS_LEFT_OUT)],
        "fields": FIELDS,
        "sizes": sizes,
    }
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(to_json(output))
    print(f"wrote {target}", file=sys.stderr)

    def nodes(size):
        return f"{size['nodes_from']}-{size['nodes_to'] if size['nodes_to'] is not None else ''}"

    def gib_text(value):
        return "unknown" if value is None else f"{value:g} GiB"

    print(f"Reserved in a cluster on OpenShift {base['ocp_version']}, from {base['run']} ({size_of(base)}):")
    print(f"{'size':>8} {'nodes':>9} {'pods':>5} {'cores':>6} {'GiB':>6} {'NICs':>5}  not included")
    for size in sizes:
        total = size["reserved"]["totals"]
        extra = ", ".join(f"{item['component']} ({item['copies']} {'copy' if item['copies'] == 1 else 'copies'}, "
                          f"{gib_text(item['memory_gib'])})" for item in size["reserved"]["not_included"])
        print(f"{size['size']:>8} {nodes(size):>9} {total['pods']:5d} {total['cpu_cores']:6.1f} "
              f"{total['memory_gib']:6.1f} {total['swift_nics']:5d}  {extra}")
    print("Used (p95 of the readings):")
    print(f"{'size':>8} {'nodes':>9}  {'from run':36} {'trust':>8} {'pods':>5} {'cores':>6} {'GiB':>6} {'NICs':>5}")
    for size in sizes:
        used = size["used"]
        source = used["run"] + (" (borrowed)" if used["borrowed"] else "")
        total = used["totals"]
        print(f"{size['size']:>8} {nodes(size):>9}  {source:36} {used['trust']:>8} {total['pods']:5d} "
              f"{total['cpu_cores']:6.1f} {total['memory_gib']:6.1f} {total['swift_nics']:5d}")
    if not setting_problems:
        print(f"Check: every test run got its size's settings from the sizing file, apart from the "
              f"{len(SETTINGS_LEFT_OUT)} left out.")
    if checked and matched == checked:
        print(f"Check: worked out from another small run on the same OpenShift version, all {len(checked)} runs that "
              "have one come out as recorded.")


if __name__ == "__main__":
    main()
