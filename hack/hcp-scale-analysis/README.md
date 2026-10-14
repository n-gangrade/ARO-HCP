# HCP scale-test analysis

Scripts that turn raw kube-burner scale-test dumps into per-container control-plane usage for each
hosted-cluster size, plus a note on how much to trust each size.

## Files

| File | What it is |
|---|---|
| `plot_controlplane_usage.py` | Graphs one run's total control-plane CPU and memory over time, into `graphs/<run>/` |
| `container_usage.py` | Prints one run's per-container usage: every container, or each copy of one container with `--container` |
| `combine_usage.py` | Works out per-container usage for many runs and writes `container-usage.json` |
| `component_requests.py` | Prints what one copy of each component (pod) needs for each run, from `container-usage.json` |
| `simulator_profiles.py` | Writes those numbers for each size in the scheduling simulator's input format, into `simulator-profiles.json` |
| `size_cores.py` | Works out the cores one cluster of each size takes up on the management-cluster machines, with the scheduling simulator, into `size-cores.json` |
| `build_pm_page.py` | Builds the PM page, `pm-page.html`, from `pm-page-template.html` and `size-cores.json` |
| `pm-page-template.html` | The PM page's layout and code, without the numbers |
| `run-notes.json` | Hand-written trust note for each run (`good`, `caveats` or `low`, and why) |
| `container-usage.json` | Generated: per-container usage of every run, with its trust note. The input for comparing sizes and for `component_requests.py` and `simulator_profiles.py` |
| `simulator-profiles.json` | Generated: what one copy of each component needs for each size, for the scheduling simulator |
| `size-cores.json` | Generated: one cluster's own, shared and total cores for each size. The input for the PM page |
| `pm-page.html` | Generated: the PM page, a single self-contained file that estimates the cores to request per region and customer |
| `graphs/` | Generated: CPU and memory graphs and a `summary.json` for every run |

The older scripts here (`extract_all.py`, `extract_kas.py`, `plot_kas.py`, `plot_kas_sum.py`, `report_replicas.py`,
`run_all.sh`, `datasets.py`) feed the scheduling simulator and aren't used by the scripts above. `extract_all.py`
counts the duplicate records in the 250- and 500-node dumps twice.

## Requirements

Python 3.11 or newer (older versions can't parse some of the dumps' timestamps), with numpy and matplotlib. For
example, the scheduling simulator's virtual environment (`../scheduling-simulator/venv`) with matplotlib added.
`size_cores.py` also needs the scheduling simulator's own packages, which that environment has.

## Updating the numbers

The raw dumps are in the `aro-hcp-raw-usage` Google Drive folder. Download them to a folder of their own, for
example `~/Downloads/aro-hcp-raw-usage/`; the scripts only read them. Then, from this directory, with `PY` set to
your Python (for example `PY=../scheduling-simulator/venv/bin/python`):

1. Graph each new run and look at its graphs:

       $PY plot_controlplane_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz

2. Add or update the run's entry in `run-notes.json`.
3. Rebuild the data file from every run (about 30 seconds). It warns about any run without a trust note:

       $PY combine_usage.py ~/Downloads/aro-hcp-raw-usage/*.ndjson*

4. Rebuild the simulator's input from it. If a new run tests a size that another run already tests, say which one
   to use in `PREFERRED_RUNS` in `simulator_profiles.py`:

       $PY simulator_profiles.py

5. Rebuild the cores per size from it (a few seconds; about 30 with `--machines simulator`):

       $PY size_cores.py

6. Rebuild the PM page from it:

       $PY build_pm_page.py

7. Commit `graphs/`, `run-notes.json`, `container-usage.json`, `simulator-profiles.json`, `size-cores.json` and
   `pm-page.html`.

To look at one run in detail:

    $PY container_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz --top 10
    $PY container_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz --container kube-apiserver

To see what one copy of each component needs, from `container-usage.json` (by run name or number of worker nodes):

    $PY component_requests.py 120

To run the scheduling simulator's website on these numbers instead of its own:

    ARO_HCP_SIM_PROFILES=$PWD/simulator-profiles.json make -C ../scheduling-simulator dev

Its percentile setting makes no difference then, as each component's number is already worked out here; its
multiplier still applies. The simulator adds a router to every size (from the 49-node run, the only one with a
router) and two login components the test clusters didn't run, `oauth-openshift` and `openshift-oauth-apiserver`,
with small fixed sizes.

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

## Using the numbers

- **A cluster's total use:** use the run's `total`, the p50 (typical) and p95 (busy moments) of the whole
  control plane's actual total. Adding up copies x `average_copy.p50` over all containers gives about the same
  typical total, but don't add up p95s: containers don't peak at the same moment, so the sum overstates the
  cluster's p95, by about 43% for CPU at 250 nodes.
- **Reservations:** every copy of a container gets the same request, and which copy is busiest is random, as each
  client sticks to the copy it first connected to. So use `all_copies`, which treats the copies as interchangeable.
  For memory it's about the busiest copy, as each copy's memory stays steady; for CPU it's lower, as one copy's
  spikes are a small part of all the readings.
- **Filled-in readings:** `total.filled_in` says how much of a total was filled in. It's about a quarter for the
  3- and 6-node CPU and almost nothing elsewhere.

## Cores per cluster size

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
server, network connection or other files. Add a row for each customer, region, cluster size and number of
clusters, and it shows:

- **Cores to request per region:** the clusters' own cores, their shared cores and the total, rounded up to whole
  cores.
- **Per customer and region:** the same, split by customer.
- **Cores per cluster:** the table from `size-cores.json`, with each size's trust level, and a notice when a row
  uses a size whose test data is less reliable.
- **How the numbers are worked out:** the assumptions behind them, and the test run and trust note behind each size.

Sizes are the tested numbers of worker nodes; for a size in between, use the next larger one. Regions are free text
for now, used only to group the rows; costs in dollars aren't included yet. To change the layout, edit
`pm-page-template.html` and rebuild; `build_pm_page.py` fills in the numbers where it says `__PAGE_DATA__`.

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
- At 120 and 250 nodes, the busiest kube-apiserver copy's memory levels off at about the soft memory limit
  (GOMEMLIMIT) production sets for that size class (`kasGoMemLimit` in
  `hypershiftoperator/deploy/templates/cluster.clustersizingconfiguration.yaml`: 12 GiB for 61 to 120 nodes, 24 GiB
  for 121 to 252), so those memory numbers depend on today's limits.

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
4. **A run at the top of each size class:** 60, 120, 252 and 360 worker nodes (small, medium, large and xlarge), so
   each class's numbers cover its largest clusters. There's no run for xlarge (253 to 360 nodes), and the only run for
   xxlarge (500 nodes) has no churn phase.
5. **Clusters set up like production's:** a router and the login components (`oauth-openshift` and
   `openshift-oauth-apiserver`) in every run. Only the 49-node run had a router, so the scheduling simulator uses its
   numbers for every size, and no run had the login components, so the simulator gives them small fixed sizes.
6. **Clean dumps:** each reading once (the 250- and 500-node dumps repeat 60,397 and 30,417 readings), no missing
   readings (the 3- and 6-node dumps miss about a quarter of their CPU readings), and only the cluster under test
   (the 3-node dump has control-plane readings from 11 hosted clusters).
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
