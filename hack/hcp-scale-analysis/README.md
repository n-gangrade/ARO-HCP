# HCP scale-test analysis

Scripts that turn raw kube-burner scale-test dumps into per-container control-plane usage for each
hosted-cluster size, plus a note on how much to trust each size.

## Files

| File | What it is |
|---|---|
| `plot_controlplane_usage.py` | Graphs one run's total control-plane CPU and memory over time, into `graphs/<run>/` |
| `container_usage.py` | Prints one run's per-container usage: every container, or each copy of one container with `--container` |
| `combine_usage.py` | Works out per-container usage for many runs, and what Kubernetes reserved for each container, and writes `container-usage.json` |
| `component_requests.py` | Prints what one copy of each component (pod) needs for each run, from `container-usage.json` |
| `simulator_profiles.py` | Writes those numbers for each size in the scheduling simulator's input format, into `simulator-profiles.json` |
| `size_pods.py` | Writes the pods one cluster of each t-shirt size runs, with what each copy reserves today and what it uses, into `size-pods.json` |
| `size_cores.py` | Works out the cores one cluster of each size takes up on the management-cluster machines, with the scheduling simulator, into `size-cores.json` |
| `pack_clusters.py` | Works out how many control-plane machines, and so cores of quota, a mix of hosted clusters needs, by packing their pods from `size-pods.json` |
| `check_packing.py` | Checks that `pack_clusters.py`'s packings follow the rules, and compares them with the fewest machines possible |
| `cores_per_cluster.py` | Works out the cores one cluster of each size takes on a full management cluster, into `cores-per-cluster.json`: the numbers for the PM page |
| `build_pm_page.py` | Builds the PM page, `pm-page.html`, from `pm-page-template.html`, `cores-per-cluster.json` and `size-pods.json` |
| `pm-page-template.html` | The PM page's layout and code, without the numbers |
| `run-notes.json` | Hand-written trust note for each run (`good`, `caveats` or `low`, and why) |
| `container-usage.json` | Generated: per-container usage of every run and what Kubernetes reserved, with the run's trust note. The input for comparing sizes and for `component_requests.py` and `simulator_profiles.py` |
| `simulator-profiles.json` | Generated: what one copy of each component needs for each size, for the scheduling simulator |
| `size-pods.json` | Generated: the pods one cluster of each t-shirt size runs, with what each copy reserves today and what it uses, for simulating how clusters pack onto machines |
| `size-cores.json` | Generated: one cluster's own, shared and total cores for each size, by the scheduling simulator |
| `cores-per-cluster.json` | Generated: how many clusters of each size fit on a management cluster, and the cores one cluster takes on a full one. The input for the PM page |
| `pm-page.html` | Generated: the PM page, a single self-contained file that works out the cores needed per region and customer, and the quota a region needs if its management clusters are full |
| `graphs/` | Generated: CPU and memory graphs and a `summary.json` for every run |

The older scripts here (`extract_all.py`, `extract_kas.py`, `plot_kas.py`, `plot_kas_sum.py`, `report_replicas.py`,
`run_all.sh`, `datasets.py`) feed the scheduling simulator and aren't used by the scripts above. `extract_all.py`
counts the duplicate records in the 250- and 500-node dumps twice.

## Requirements

Python 3.11 or newer (older versions can't parse some of the dumps' timestamps), with numpy and matplotlib. For
example, the scheduling simulator's virtual environment (`../scheduling-simulator/venv`) with matplotlib added.
`size_pods.py` also needs PyYAML, `size_cores.py` the scheduling simulator's own packages, and `check_packing.py` and
`cores_per_cluster.py` OR-Tools, which that environment has.

## Updating the numbers

The raw dumps are in the `aro-hcp-raw-usage` Google Drive folder. Download them to a folder of their own, for
example `~/Downloads/aro-hcp-raw-usage/`; the scripts only read them. Then, from this directory, with `PY` set to
your Python (for example `PY=../scheduling-simulator/venv/bin/python`):

1. Graph each new run and look at its graphs:

       $PY plot_controlplane_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz

2. Add or update the run's entry in `run-notes.json`.
3. Rebuild the data file from every run (about a minute and a half). It warns about any run without a trust note:

       $PY combine_usage.py ~/Downloads/aro-hcp-raw-usage/*.ndjson*

4. Rebuild the pods per t-shirt size from it and HyperShift's sizing file. It stops or warns if its checks fail:

       $PY size_pods.py

5. Rebuild the cores per cluster on a full management cluster from them (about 10 seconds). It stops if a packing
   breaks a rule:

       $PY cores_per_cluster.py

6. Rebuild the PM page from it:

       $PY build_pm_page.py

7. Rebuild the simulator's input from the data file. If a new run tests a size that another run already tests, say
   which one to use in `PREFERRED_RUNS` in `simulator_profiles.py`:

       $PY simulator_profiles.py

8. Rebuild the simulator's cores per size from it (a few seconds; about 30 with `--machines simulator`):

       $PY size_cores.py

9. Commit `graphs/`, `run-notes.json`, `container-usage.json`, `size-pods.json`, `cores-per-cluster.json`,
   `pm-page.html`, `simulator-profiles.json` and `size-cores.json`.

To look at one run in detail:

    $PY container_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz --top 10
    $PY container_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz --container kube-apiserver

To see what one copy of each component needs, from `container-usage.json` (by run name or number of worker nodes):

    $PY component_requests.py 120

To run the scheduling simulator's website on these numbers instead of its own:

    ARO_HCP_SIM_PROFILES=$PWD/simulator-profiles.json make -C ../scheduling-simulator dev

Its percentile setting makes no difference then, as each component's number is already worked out here; its
multiplier still applies. The simulator adds a router to every size (from the 49-node run, the only one with a
router) and two login components, `oauth-openshift` and `openshift-oauth-apiserver`, with small fixed sizes, although
neither the test clusters nor production's clusters run them.

Graph images depend on the matplotlib version, so regenerating them elsewhere can change the PNG files even when
the numbers don't change.

## How the numbers are worked out

1. Keep only the hosted cluster under test: the namespace containing the dump's `jobSummary.clusterName`. Some
   dumps also contain other clusters.
2. Count each reading once (the 250- and 500-node dumps contain exact duplicates) and skip pause containers.
3. Use only the steady part of the run: the kube-burner churn phase. The 500-node run has none, so its memory
   uses the last 10 minutes, once memory has leveled off, and its CPU uses the whole run, which is all create
   phase and so a conservative estimate.
4. Keep every copy (pod) of every container separate, and use the readings as recorded. Missing readings are
   skipped, never counted as zero.
5. For each copy, take the median (p50, typical) and p95 (busy moments).
6. Combine a container's copies three ways: all the copies' readings put together (what reservations use), the
   busiest copy (the highest value among the copies, for each percentile separately) and the average copy.
7. For each run, also add up every copy at each moment to get the whole control plane's total, and take its
   p50 and p95. Each copy's missing readings are filled in along a straight line from its neighboring
   readings, so they don't count as zero.
8. `component_requests.py` adds up each component's containers into what one copy (pod) of it needs, as
   Kubernetes reserves per container. A component runs as many copies as the most any of its containers has.
9. Separately, record what Kubernetes reserved for each container (its resource requests), which the tests
   recorded once, at the end of each run: per copy for each container (`reserved`) and in total for the run
   (`reservations`). These come from kube-state-metrics, whose series reach the monitoring through the management
   cluster's own Prometheus, which runs 2 replicas (`mgmt.prometheus.prometheusSpec.replicas` in
   `config/config.yaml`). The 49-node dump keeps both replicas' copies of every kube-state-metrics series, so the
   reservations it recorded are double the real ones. Its record of the management cluster's nodes
   (`kube_node_info`, which the dumps call `mgmtNodeRoles`), also from kube-state-metrics but recorded without adding
   anything up, shows the two copies, so `combine_usage.py` halves them. As a check, it warns unless the token
   minters, small helper containers in every run, then reserve 10m CPU and 30 MiB, as every token minter did in
   every run (and as HyperShift gives the token minters it adds, in its
   `support/controlplane-component/token-minter-container.go`). The usage readings aren't doubled: Azure Monitor's
   own agent collects them, once (`cadvisor = true` in
   `observability/prometheus/deploy/templates/ama-metrics-settings-configmap.yaml`).
10. kube-state-metrics records nothing for a container that reserves nothing, and reports containers that start
    before the main ones (sidecars) separately, as init containers (`kube_pod_init_container_resource_requests`),
    which none of the runs' queries include. HyperShift has started running token minters as sidecars: in the
    49-node run (OpenShift 4.22), the image registry operator's `apiserver-token-minter` was running but has no
    recorded reservation, while the 3- and 6-node runs (4.20) recorded 10m CPU and 30 MiB for it. So a container
    that was running at the end without a recorded reservation is marked `"recorded": false`, and gets the
    reservation the newest run that recorded it had (`borrowed_from`), or 0 if no run did (only
    `package-operator-remote-phase-manager`'s `manager`, in the runs of up to 30 nodes).
11. A container with a reservation but no usage in the window goes under `reservations.left_out` and isn't
    counted: in the 49-node run, `featuregate-generator`, which has no usage readings at all, so it's probably a
    job that had finished (the dumps don't record pods' status). Kubernetes doesn't count a finished pod's
    reservation. The other runs' query only counts pending and running pods (the
    `cluster:namespace:pod_*:active:kube_pod_container_resource_requests` rules in
    `observability/alerts/kubernetesControlPlane-prometheusRule.yaml`), while the 49-node run's counts every pod.

## Using the numbers

- **A cluster's total use:** use the run's `total`, the p50 (typical) and p95 (busy moments) of the whole
  control plane's actual total. Adding up copies x `average_copy.p50` over all containers gives about the same
  typical total, but don't add up p95s: containers don't peak at the same moment, so the sum overstates the
  cluster's p95, by about 43% for CPU at 250 nodes.
- **Reservations from usage:** every copy of a container gets the same request, and which copy is busiest is random,
  as each client sticks to the copy it first connected to. So use `all_copies`, which treats the copies as
  interchangeable. For memory it's about the busiest copy, as each copy's memory stays steady; for CPU it's lower,
  as one copy's spikes are a small part of all the readings.
- **Today's reservations:** `reserved` (per copy) and `reservations.total` (per run) are what production reserved
  for the test clusters, as opposed to what they used. On OpenShift 4.19, every run's control plane reserved 4.335
  cores whatever its size, while its memory followed its t-shirt size: 43.3 GiB for small, 75.0 for medium, 133.0
  for large and 388.0 for xxlarge, as `cluster.clustersizingconfiguration.yaml` sets memory for each of these sizes,
  but not CPU. The 3- and 6-node runs (OpenShift 4.20) reserved 4.295 cores and 43.1 GiB, and the 49-node run (4.22)
  4.555 cores and 43.6 GiB.
- **Two sizing settings that don't take effect:** `cluster.clustersizingconfiguration.yaml` also sets the memory of
  `ovnkube-control-plane` (for every size) and `multus-admission-controller` (for large and up), but no run got
  those settings: whatever its size, every run reserved 200 MiB per copy for the first and 50 MiB for the second,
  so for these two the file doesn't say what's reserved. The network operator, not HyperShift, creates these two
  pods, and HyperShift applies its resource-request overrides only to the pods it creates (`applyRequestsOverrides`
  in its `support/controlplane-component/defaults.go`); its own tests have a TODO to set requests for networking
  pods once that's possible (`test/e2e/util/reqserving/setup.go`). On big clusters, both use far more than they
  reserve: at 500 nodes, about 1.26 and 1.25 GiB per copy (p95).
- **Filled-in readings:** `total.filled_in` says how much of a total was filled in. It's about a quarter for the
  3- and 6-node CPU and almost nothing elsewhere.

## Pods per t-shirt size

`size_pods.py` writes `size-pods.json`: for each t-shirt size, every pod a hosted cluster runs, how many copies, and
what one copy needs (CPU, memory and SWIFT NICs), in two versions: what it reserves (`reserved`) and what it uses
(`used`). It's the input for simulating how a customer's clusters pack onto management-cluster machines.

- **Sizes** are the ones stage and prod use, from
  `hypershiftoperator/deploy/templates/cluster.clustersizingconfiguration.yaml` (the sizing file's `{{ else }}` part,
  as they don't set `limitClusterSizes`): small (0 to 60 nodes), medium (61 to 120), large (121 to 252), xlarge (253
  to 360) and xxlarge (361 and up). HyperShift counts all of a cluster's nodes, infra nodes as well as workers, and
  every test cluster from 12 nodes up had 2 infra nodes besides its workers, so the 60-node run counts as medium and
  the 120-node run as large.
- **Reserved** pods come from the newest test run with reservations, the 49-node run (a small cluster on OpenShift
  4.22, from August 2026), as its pods are the closest to today's. So they're what a cluster on OpenShift 4.22
  reserves: the test clusters on other versions reserved a little less CPU (4.335 cores on 4.19 and 4.295 on 4.20,
  against 4.555), and SREs can move a cluster to a bigger size than its nodes call for
  (`docs/ops/override-cluster-size.md`). For every other size, each container the sizing file sets memory for gets
  that size's setting instead of small's. So every size has numbers without a test run of its own, xlarge
  included: one cluster reserves 43.6 GiB if it's small, 75.3 if medium, 133.3 if large, 271.3 if xlarge and 388.3
  if xxlarge, and 4.555 cores whatever its size, as the file sets memory for these sizes but not CPU.
  - The file's settings for `ovnkube-control-plane` and `multus-admission-controller` are left out
    (`SETTINGS_LEFT_OUT`), as no test run got them (see "Two sizing settings that don't take effect").
  - The file also sets memory for the cluster autoscaler, 2 to 12 GiB a copy from medium up, but no test cluster
    ran one, so it isn't included (`NOT_TESTED`). `not_included` lists it for each size, with 1 copy and the
    file's setting, or null for small, where the file sets none: on the 49-node run's management cluster, 1 of the
    12 hosted clusters ran one, with 1 copy.
  - If the run had no router, the reserved pods get the newest one recorded (`reserved_from.router_from`). With
    none, the script stops, as the router's copies decide each cluster's SWIFT NICs.
  - Two checks warn if this way of working them out stops holding: every test run should have got its size's
    settings, and each run, worked out the same way from another small run on its OpenShift version, should come
    out as recorded. Both hold: worked out from a 12-node run, for example, the 60-, 120-, 250- and 500-node runs
    come out exactly as recorded. But the 49-node run is the only one on its OpenShift version, so the second check
    can't cover it, and no run is xlarge, so neither covers xlarge's settings. So the script also stops if the
    sizing file sets anything for a container the 49-node run doesn't have, unless `NOT_TESTED` lists it, which
    catches a misspelled name in any size.
- **Used** pods need their containers' p95 of all copies' readings, added up, so the numbers are what the pods use:
  what clusters could reserve if their reservations matched their use. Each size uses the largest test cluster
  within its range, preferring good trust, as the largest is closest to the size's biggest clusters: small uses the
  30-node run (32 nodes in total), medium the 60-node run (62), large the 250-node run (252) and xxlarge the
  500-node run. No run falls within xlarge, so it borrows xxlarge's run and is marked as such. `CHOSEN_RUNS` in
  `size_pods.py` picks a different run for a size. Every cluster gets a router, measured in the 49-node run, the
  only one with a router.
- **In both,** each router copy needs one SWIFT NIC, the login components the scheduling simulator adds aren't
  included, as production's clusters don't run them, and a component with more than one copy runs one copy per
  availability zone (`one_per_zone`). HyperShift requires that on Azure for the components it spreads across zones
  (`setMultizoneSpread` in its `support/controlplane-component/defaults.go`), and on the 49-node run's management
  cluster, every multi-copy component of all 12 hosted clusters had its copies on different machines in different
  zones, apart from one `capi-provider`, which has 1 copy in every test run. The script stops if no run had a router,
  or if a component has more copies than the 3 zones.

Reservations and use differ a lot. For memory, clusters reserve far more than they use: a small test cluster
reserves about 43 GiB, while at busy moments (the p95 of its `total` in `container-usage.json`) the whole cluster
used 9.5 to 28.5 GiB. For CPU, clusters of every size can use more than they reserve, which works only while their
machines have CPU to spare: the 49-node cluster used 5.3 cores typically (p50) and 6.9 at busy moments (p95),
against 4.555 reserved, and at busy moments the 30- and 60-node clusters used 4.6 and 4.4 cores and the 120- and
250-node ones 5.9 and 11.7, against about 4.3 reserved, and the 500-node one 26.1 (though that run has low trust).

## Packing clusters onto machines

`pack_clusters.py` works out how many control-plane machines, and so how many cores of quota, a mix of hosted
clusters needs on machines of its own. Production shares management clusters between customers, so the PM page's
numbers come from `cores_per_cluster.py` instead (see the next section). For example:

    $PY pack_clusters.py small=10 large=2
    $PY pack_clusters.py --per-set

It packs every pod of the clusters (the reserved pods in `size-pods.json`, or the used ones with `--basis used`) onto
production's 32-core control-plane machines, spread over 3 availability zones, by the rules production's scheduler
follows. The public config names `Standard_E32ds_v6` for these machines (`mgmt.aks.userAgentPool.vmSize` in
`config/config.yaml`), but the type can differ by region, and it decides which quota family to request; the packing
only needs its cores, memory and network cards.

- **Zones:** the copies of a component with more than one copy run in different zones, so on different machines.
  A component with one copy can run in any zone.
- **Room:** a pod fits on a machine if what's left of the machine's CPU, memory, SWIFT NICs and pod slots covers
  what the pod reserves. Each machine has 32 cores, 251.7 GiB of memory (what the 49-node run's machines show;
  Azure lists 256 GiB), 7 SWIFT NICs (its network cards other than the primary one, which mgmt-agent counts and
  offers to the scheduler, `countSwiftNICs` in `mgmt-agent/pkg/controller/controller.go`; `secondaryNicCount` in
  `config/config.yaml`) and 225 pod slots (`maxPods` in `dev-infrastructure/modules/aks-cluster-base.bicep`). It
  keeps back 3 cores and 7550 MiB for the system (the kubelet's system reservation, `mgmt-fixes/deploy/kubelet-ds`),
  and room for the 14 helper pods every control-plane machine runs: in the 49-node run, each of its 18 ran the same
  14, kube-system's network, storage, monitoring and security agents, arobit's log forwarder and velero's node agent.
  What those reserve isn't in the data, so the packing uses the scheduling simulator's measurement of what such pods
  used on a production management cluster, 0.468 cores and 3.5 GiB per machine (`node_overhead_*` in
  `../scheduling-simulator/optimize/engine.py`); in the 49-node run, the pods outside the control planes used 2.2 to
  4.8 GiB per machine. That leaves 28.532 cores, 240.819 GiB, 7 SWIFT NICs and 211 pod slots per machine.

How it packs: the sizes in a fixed order, small to xxlarge, so the answer doesn't depend on the order they're given;
first the components with a copy in every zone, then the rest, each biggest first. Each copy goes to a zone where it
fits on a machine already there, the least full first, and there on the machine with the most room left in what the
copy needs, which keeps a zone's machines from filling up lopsidedly, one out of memory and another out of pod slots.
It adds a machine only when a copy fits on none. It reports the machines in each zone, the cores to request for them
(whole machines), and how full each kind of room is. That isn't how production's scheduler places pods: HyperShift
prefers to keep a cluster's pods together, and in the 49-node run, about 40 of each complete cluster's 60 to 67 pods
were on one machine.

One machine in each zone (3 machines, 96 cores) holds 7 small or 7 medium clusters, as their SWIFT NICs run out
first: every cluster's router puts a copy, and a SWIFT NIC, in every zone. It holds 5 large, 2 xlarge or 1
xxlarge cluster, as memory runs out first. So 10 small clusters need 6 machines, 192 cores, although their pods
reserve 45.5 cores; and a single cluster of any size needs 3 machines, one in each zone.

**How close it comes to the fewest machines possible:** `check_packing.py` checks every packing against the rules,
and works out the fewest machines possible with an exact solver, doing the same arithmetic, to compare. In 155
cases, no packing broke a rule or disagreed with the solver. It needed the fewest machines for every mix of small and
medium clusters (40 of 40), and for 66 of 75 counts of one size alone (1 to 15 clusters); the other 9, with 8 or more
large, xlarge or xxlarge clusters, needed 1 or 2 more. Of 40 mixes with larger sizes, in one run it needed the
fewest in 18 and 1 to 4 more in 18, and the solver ran out of time on the other 4; these counts vary between runs, as
the solver has a time limit. The gap grows with the number of larger clusters: 20
large clusters need 14 machines here and 12 at the fewest, 30 need 21 and 17. Adding a cluster can even lower the
answer: 14 large clusters need 11 machines here, 15 only 9. No packing that follows the rules needs fewer than the
fewest, so it never comes in under that. Rerun `check_packing.py` after changing the packing (about 11 minutes).

**It packs the clusters given on their own, which production doesn't.** Production's placement controller
(`selectByCapacity` in `backend/pkg/controllers/cluster/placement/placement_controller.go`) puts each new hosted
cluster on the management cluster with the most free SWIFT NICs, counting each control-plane pool at its largest
size, and doesn't look at CPU or memory yet. So a customer's clusters share machines with other customers'.

Not included yet:

- **The machines every management cluster keeps whatever its load:** its system and infra pools, and the minimum
  number of control-plane machines production keeps in each zone. Production's management clusters are much
  emptier than this packing: in the 49-node run, its management cluster ran 11 clusters with routers on 18
  control-plane machines, 6 in each zone, none using more than 3 of its 7 SWIFT NICs, where this packing needs 6
  machines.
- **Shared pods on the control-plane machines:** besides each machine's 14 helper pods, 23 other pods ran on the
  49-node run's 18 control-plane machines, such as the HyperShift operator's, mgmt-agent's, monitoring's and one
  cluster's ACM agents.
- Spare room for upgrades, a zone failure or new clusters.
- The cluster autoscaler of clusters that use autoscaling (`not_included` in `size-pods.json`), and the kubelet's
  eviction threshold, under 1 GiB of each machine's memory.

## Cores per cluster on a full management cluster

`cores_per_cluster.py` works out the cores one hosted cluster of each size takes on a full management cluster, into
`cores-per-cluster.json`. These are the numbers for the PM page. As a region grows, its management clusters fill up
with many customers' clusters, as production's placement controller puts each new one on the management cluster with
the most free SWIFT NICs. So each cluster takes its share of a full management cluster's machines, and a customer's
cores are their clusters' shares added up.

    $PY cores_per_cluster.py
    $PY cores_per_cluster.py --full 10 --other system=1x8 infra=3x32 --limit 60 --output /tmp/cores-per-cluster.json

For each size it works out:

- **Clusters that fit** (`clusters_that_fit`): the most clusters of the size that fit on a management cluster with 1
  to 20 control-plane machines per zone, by the same rules as `pack_clusters.py`. As all clusters of a size are the
  same, an exact solver (OR-Tools' CP-SAT) can work out how many copies of each component go on each machine. Where
  two packings found for fewer machines, side by side, already hold as many clusters as the machines' room allows in
  total, it uses them instead, and it starts the solver from them otherwise. Every packing is then spread out into
  single clusters and checked with `check_packing.py`'s rule checks, independently of the solver, and it stops if one
  breaks a rule. For the reserved pods, every number is the most that the machines' room allows in total, so none
  could be higher: 7 small or medium clusters for each machine per zone, as their SWIFT NICs run out first, and for
  large and bigger, the machines' memory divided by one cluster's. It takes about 10 seconds, with the same result
  every time. A number the solver can't prove is the most possible is listed under `not_proven`, and could come out
  differently in another run.
- **Cores per cluster** on a full management cluster, holding the clusters that fit, but no more than the limit:
  - `own_cores`: the cluster's own share of the control-plane machines, which its control plane runs on: their cores
    over the clusters that fit;
  - `shared_cores`: its share of what's there for all the clusters, amortized: the machines a management cluster
    runs besides its control-plane machines, its system and infra machines, which run its logging, monitoring and
    cluster-management agents, and any control-plane room the limit leaves unused;
  - `cores`: the two added up, a full management cluster's cores over the clusters it holds.
- **What runs out first** (`runs_out_of`) on a full management cluster, and for 1 to 20 control-plane machines per
  zone (`runs_out_of_by_machines`), and what stops more clusters going on (`limited_by`): that, or the limit.

The management cluster's settings are all placeholders to confirm, as production's real numbers are in its private
deployment config. `settings.placeholders` in `cores-per-cluster.json` says whether `--full` and `--other` are still
these placeholders:

- **Control-plane machines per zone on a full management cluster** (`--full`): 6, what the 49-node run's management
  cluster had with its 12 hosted clusters; a full one can have more.
- **Its other machines** (`--other`): 1 system machine with 8 cores and 3 infra machines with 32, what the 49-node
  run's management cluster had. Quota is planned with every pool at its largest, so count them at their largest.
- **The most hosted clusters to plan for per management cluster** (`--limit`): 60, where the Service Lifecycle team's
  capacity alert (`observability/alerts/mgmt-capacity-prometheusRule.yaml`) counts a management cluster as full; above
  51 it asks for another management cluster. It's to confirm with them: the alert has kept the `info` severity it was
  given for testing since December 2025, its commits give no reason for 60, and it predates placement by SWIFT NICs.
  Clusters Service puts at most 100 on one (`clustersService.provisionShardClusterLimit` in `config/config.yaml`,
  `clusters_service_limit`), the most `--limit` can be, but highly available clusters, which need a SWIFT NIC in each
  zone, run out of them first. Filling management clusters until they run out of room keeps nothing spare for
  upgrades, a zone failure or the time a new management cluster takes to add, and as the placement controller only
  counts SWIFT NICs, it could put more large and bigger clusters on a management cluster than its memory holds.

With these settings:

| Size | Clusters on a full management cluster | Own cores | Shared cores | Cores per cluster | Limited by |
|---|---|---|---|---|---|
| small | 42 | 13.71 | 2.48 | 16.19 | SWIFT NICs |
| medium | 42 | 13.71 | 2.48 | 16.19 | SWIFT NICs |
| large | 32 | 18.00 | 3.25 | 21.25 | memory |
| xlarge | 15 | 38.40 | 6.93 | 45.33 | memory |
| xxlarge | 11 | 52.36 | 9.45 | 61.82 | memory |

So 10 small clusters take about 162 cores, where on machines of their own they need 192 (`pack_clusters.py`). With
more machines per zone, a cluster's own cores barely change, while the other machines' cores are shared by more
clusters, until the limit stops more clusters going on: then the control-plane room left unused is shared too. With
a limit of 60, small, medium and large clusters all take a full management cluster's cores over 60.

Quota is planned per management cluster, for all its machines at their largest, so these per-cluster numbers are
averages: a region whose management clusters are full needs a whole new one at once, 680 cores with these settings
(`new_management_cluster_cores`).

Not included:

- **How tightly production packs:** the solver packs the machines as tightly as the rules allow, while production's
  scheduler places pods one at a time, preferring to keep a cluster's pods together, and its placement controller
  only counts SWIFT NICs. For small and medium clusters, whose SWIFT NICs run out first, the placement controller
  stops where the solver does; large and bigger ones may take more machines in production, and the placement
  controller could put more of them on a management cluster than its memory holds.
- **Mixing sizes:** each size's share assumes a management cluster full of that size. Small and medium clusters run
  out of SWIFT NICs and larger ones out of memory, so a management cluster holding both can hold more than their
  shares add up to.
- Spare room for upgrades, a zone failure or new clusters, and the cluster autoscaler of clusters that use
  autoscaling.
- **The shared pods on the control-plane machines**, which in the 49-node run used 3.5 GiB and about 2.5 cores in all.
- **The infra machines' limits:** each hosted cluster runs 5 ACM agent pods on them. In the 49-node run, the 3 infra
  machines ran 129 pods, 52 of them agents for 11 clusters. With 225 pod slots each (`maxPods` in
  `dev-infrastructure/modules/aks-cluster-base.bicep`), they'd hold the agents of about 119 clusters, so a management
  cluster with more than that needs more infra machines.

`--basis used` packs what the pods use instead of what they reserve, into `cores-per-cluster-used.json`, which isn't
committed, as a few of its numbers aren't proven the most possible and could change between runs. It takes about 5
minutes. With the same settings, small and medium clusters take the same 16.19 cores, as their SWIFT NICs still run
out first; large clusters take 24.29, as their pods use more CPU than they reserve; and xlarge and xxlarge clusters,
whose pods both come from the 500-node run, which has low trust, take 48.57, as they use far less memory than they
reserve.

## Cores per cluster size

The PM page no longer uses these numbers: it uses `cores_per_cluster.py`'s (see "Cores per cluster on a full
management cluster").

`size_cores.py` packs a sample mix of hosted clusters onto management-cluster machines with the scheduling
simulator, then splits the machines between the clusters the way the simulator splits its bill: each pool goes to
each size by its largest share of the pool's CPU, memory, SWIFT NICs or pod slots. For each size, `size-cores.json`
has:

- `own_cores`: what one cluster's own control-plane pods reserve.
- `total_cores`: its share of the machines' cores, which also covers what no single cluster owns: system
  reservations, per-machine daemonsets, spare room for upgrades and a zone failure, placeholder slots for new
  clusters and unused capacity. `shared_cores` is the difference.
- `machine_share`: its share of each machine type, in machines. Multiplied by a region's hourly prices, it gives
  the cluster's cost there, so prices can be added without rerunning the simulator, which picks machines without
  looking at prices.

These depend on assumptions, recorded in the file's `settings`:

- **The mix.** A cluster's share depends on which clusters share the machines. The default is the simulator's
  sample fleet (200 clusters of 12 nodes, 150 of 30, 80 of 60, 40 of 120 and 10 of 250), plus 10 of each size it
  lacks. `--mix` replaces it, for example `--mix 12=300,30=100,120=20`.
- **The machines.** By default (`--machines production`) they're production's: hosted control planes run on 32-core
  `Standard_E32ds_v6` machines (`mgmt.aks.userAgentPool.vmSize` in `config/config.yaml`), each keeping back the same
  fixed 3 cores and 7550 MiB for the system as production's kubelet (`mgmt-fixes/deploy/kubelet-ds`). The simulator
  always sizes a second pool, which can't use the first pool's machine type; with today's placement it only holds
  the spare copies kept for upgrades, which production keeps on the same machines, so it gets the same machine
  under another name (one per management cluster, merged back in the output). On these machines the sample mix
  needs 13,728 cores, and every cluster of up to 120 nodes takes the same 25.2: whatever its size, a machine can
  attach at most 7 SWIFT NICs, one per router copy, so 32-core machines run out of NICs with most of their cores
  unused. That also means a margin (`--multiplier 1.15`) adds no machines.
- `--machines simulator` lets the simulator choose from its 8 machine types of 4 to 32 cores with its own defaults,
  as on its website, including a system reservation that shrinks on smaller machines. It picks mostly 16-core ones,
  and the same mix needs 8,016 cores.
- **The rest of the simulator's settings** are its defaults, with today's placement (`--policy legacy`) and no
  margin (`--multiplier 1.15` adds 15%).
- The simulator breaks ties by the order clusters are listed in, which moves the totals by about 1%.

## PM page

`pm-page.html` is a single file with the numbers built in: open it in a browser, or send it to someone. It needs no
server, network connection or other files. It's kept short for PMs: add a row for each customer, region, cluster size
and number of clusters, and it shows, using `cores-per-cluster.json`'s numbers:

- **Cores needed:** one table, each region with its customers under it: their clusters, own cores, shared (amortized)
  cores and the two added up, an average share of the region's management clusters. Each customer's are rounded up to
  whole cores, and a region's are its customers' added up, so every row adds up.
- **Quota:** a line per region with the cores its clusters take and, as quota comes in whole management clusters, how
  many new ones it needs if it has no room left (their share rounded up) and their cores. A region whose management
  clusters have room for the clusters needs no new quota for them. A copy built with `--vm-families` splits both by
  the VM family of the machines the cores are on, as Azure grants quota per region and VM family.
- **Why: cores per cluster:** that a cluster reserves only about 4.6 cores (from `size-pods.json`) but takes its share
  of a whole management cluster, and for each size how many fit on one and what stops more, its own and shared cores,
  and the best case, the same if management clusters are filled until they run out of room, up to Clusters Service's
  limit, keeping nothing spare.
- **For engineers,** collapsed: the settings and how the numbers are worked out.

Its settings are the management cluster's (see "Cores per cluster on a full management cluster"): control-plane
machines per zone, the cores of its other machines and the most hosted clusters on one. They start at
`cores-per-cluster.json`'s, and can be changed on the page, which then says so above the results. While its
management cluster's machines are `cores_per_cluster.py`'s placeholders (`settings.placeholders`) and unchanged on
the page, a banner at the top says the numbers aren't production's. As the page can't run the solver, its
control-plane machines are always the 32-core machines the numbers were worked out for, with 1 to 20 per zone.
Regions are free text, used to group the rows: "East US 2" and "eastus2" count as the same region, shown as first
typed, and rows with no region are counted under "(no region)", with a notice.

To change the layout, edit `pm-page-template.html` and rebuild; `build_pm_page.py` fills in the numbers where it says
`__PAGE_DATA__`, and stops if its inputs don't hold what the page says about them. For real quota requests, build a
copy with production's settings, outside the repo, as they're private:

    $PY cores_per_cluster.py --full N --other system=MACHINESxCORES infra=MACHINESxCORES --limit N \
        --output /tmp/cores-per-cluster-prod.json
    $PY build_pm_page.py --cores /tmp/cores-per-cluster-prod.json --output ~/pm-page-prod.html \
        --settings-from "production (WHERE AND WHEN)" --vm-families ~/vm-families-prod.json

`--settings-from` says at the top of the page where the management cluster's size comes from ("Management clusters
as in production (...)"). `--vm-families` gives each region's VM family for each pool of machines, `control_plane`
and each `--other` pool, named as in the quota request (production's are in its deployment configuration's VM sizes
and its quota files):

    {
      "source": "where the families come from, shown in the page's notes",
      "note": "anything else to say about them, shown in the page's notes",
      "regions": {"REGION": {"control_plane": "FAMILY", "system": "FAMILY", "infra": "FAMILY"}}
    }

Regions match the page's rows like its own grouping does, so "East US 2" finds "eastus2", and the page suggests the
file's regions as one is typed; a region the file doesn't have shows "VM family not known for this region". Share
that copy privately, and don't commit it or its JSON files.

## Known limitations

- Each size has only one test run, so how much the numbers vary between runs is unknown.
- The 3- and 6-node dumps are missing about a quarter of their CPU readings, so their CPU numbers, especially
  peaks, are less reliable. As a missing reading adds nothing to a recorded total, their graphs add a dashed line
  with the missing readings filled in and shade the gap between the two lines, and each run's `summary.json` says
  how many readings are missing. A missing reading at the very start or end of a copy's readings can't be filled
  in, so it doesn't show as a gap.
- The CPU metric drops readings of exactly zero, so idle containers show fewer readings.
- Churn phases of 20–23 minutes have only about 40 to 47 readings per copy, so a copy's CPU p95 is about its
  third-highest reading.
- The runs weren't set up the same way: kube-burner and OpenShift versions, churn delay and actual churn length
  differ.
- In the 250-node run (252 nodes in total, a large cluster), the busiest kube-apiserver copy's memory levels off just
  under 24 GiB, the soft memory limit (GOMEMLIMIT) production sets for large clusters (`kasGoMemLimit` in
  `hypershiftoperator/deploy/templates/cluster.clustersizingconfiguration.yaml`), so its memory numbers depend on
  today's limit.
- The test clusters from 12 nodes up had 2 infra nodes besides their workers (the 3- and 6-node ones had none), which
  HyperShift counts when it picks a t-shirt size, so the 60-node run was a medium cluster and the 120-node run a
  large one, each at the bottom of its size's range.
- Each hosted cluster also runs ACM agent pods outside its control plane's namespace, which the tests' queries don't
  cover: on the 49-node run's management cluster, 11 of the 12 hosted clusters had a `klusterlet-*` namespace of
  their own with 5 agent pods (4 in one), using about 0.15 to 0.2 GiB per cluster. ACM's settings
  (`acm/deploy/helm/multicluster-engine-config/templates/global.klusterletconfig.yaml` and
  `addon-hosted-config.addondeployconfig.yaml`) place them on the infra machines, where they ran, apart from 2
  containers of one cluster. So they take no room on the machines the control planes run on, but add to the infra
  machines with every cluster; what they reserve isn't known.

## Data we'd want from future test runs

Each item fixes a problem these runs had:

1. **A churn phase in every run, of the same length (30 minutes or more), starting once memory has leveled off.**
   The 500-node run has none, so its numbers are rough, and the others' churn phases lasted 20 to 63 minutes: the
   shortest give only about 40 readings per copy, so a CPU p95 comes down to the third-highest reading.
2. **The same setup for every size:** OpenShift and kube-burner versions, churn delay, QPS and job settings. These
   runs used three OpenShift versions, four kube-burner versions and two churn delays, so comparing sizes partly
   compares setups.
3. **Two or three runs per size**, to see how much the numbers vary between runs. With one run per size, an odd
   result, such as the 60-node cluster using less CPU than the 30-node one, can't be told apart from noise.
4. **A run at the top of each size class,** counting all nodes as HyperShift does: 60, 120, 252 and 360 nodes in
   total (small, medium, large and xlarge), so each class's numbers cover its largest clusters. With 2 infra nodes,
   that's 58, 118, 250 and 358 workers. The 60- and 120-worker runs had 2 infra nodes as well, which put them at the
   bottom of medium and large instead of the top of small and medium; there's no run for xlarge (253 to 360 nodes),
   and the only run for xxlarge (500 nodes) has no churn phase.
5. **Clusters set up like production's,** with a router in every run. Only the 49-node run had one, so its router
   numbers stand in for every size. Some runs with autoscaling would also show what the cluster autoscaler reserves
   and uses, as no run had one.
6. **Clean dumps:** each reading once (the 250- and 500-node dumps repeat 60,397 and 30,417 readings), one copy of
   each series (the 49-node dump keeps two of every kube-state-metrics series, one per Prometheus replica, which
   doubles its reservations), no missing readings (the 3- and 6-node dumps miss about a quarter of their CPU
   readings), and only the cluster under test (the 3-node dump has control-plane readings from 11 hosted clusters).
7. **CPU readings of zero kept.** The CPU query ends in `> 0`, which drops readings of exactly zero, so an idle
   container looks the same as a missing reading. Leaving out `> 0` fixes that.
8. **The settings each run actually got:** the request rate it achieved (the 30-, 60-, 120- and 250-node runs don't
   record it, and the 12-node run set to 100 QPS only reached about 6), and its size class's limits, such as the API
   server's soft memory limit, which probably held the 120- and 250-node runs' busiest copy at about 12 GiB and just
   under 24 GiB.
9. **An idle baseline:** a cluster of each size with no test load, to separate what a control plane uses just by
   existing from what the load adds.
10. **Real usage from production management clusters, over a week or more.** The tests' steady phases last 20 to 63
    minutes, and the test tool sends all its requests through one client, so they most likely all reach one API
    server copy, which makes it much busier than production's many clients would. The scheduling simulator's
    `observed` tool (`../scheduling-simulator/observed/`) collects this kind of data.
11. **Every container's reservation, recorded the same way in every run.** Every run's query missed containers
    that start before the main ones (sidecars, which kube-state-metrics reports separately, in
    `kube_pod_init_container_resource_requests`), and the 49-node run's counted finished pods, which the other runs'
    query leaves out. Recording both kinds of container and each pod's status (`kube_pod_status_phase`), with the
    same query in every run, would show every reservation and which pods were running.
12. **The ACM agents' reservations and use:** each hosted cluster's `klusterlet-*` namespace, as well as its control
    plane's, to show what every cluster adds to the infra machines.
13. **What the helper pods on every machine reserve:** kube-system's, arobit's and velero's pods on the
    control-plane machines, which the dumps don't record, so the packing uses what such pods used instead.
