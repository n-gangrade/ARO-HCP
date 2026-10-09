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
| `run-notes.json` | Hand-written trust note for each run (`good`, `caveats` or `low`, and why) |
| `container-usage.json` | Generated: per-container usage of every run, with its trust note. The input for comparing sizes and for the PM page |
| `simulator-profiles.json` | Generated: what one copy of each component needs for each size, for the scheduling simulator |
| `graphs/` | Generated: CPU and memory graphs and a `summary.json` for every run |

The older scripts here (`extract_all.py`, `extract_kas.py`, `plot_kas.py`, `plot_kas_sum.py`, `report_replicas.py`,
`run_all.sh`, `datasets.py`) feed the scheduling simulator and aren't used by the scripts above. `extract_all.py`
counts the duplicate records in the 250- and 500-node dumps twice.

## Requirements

Python 3.11 or newer (older versions can't parse some of the dumps' timestamps), with numpy and matplotlib. For
example, the scheduling simulator's virtual environment (`../scheduling-simulator/venv`) with matplotlib added.

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

5. Commit `graphs/`, `run-notes.json`, `container-usage.json` and `simulator-profiles.json`.

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
the numbers don't change. The CPU graphs also draw a 5-minute average, only to make the spiky readings easier to
read; none of the numbers use it.

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

## Known limitations

- Each size has only one test run, so how much the numbers vary between runs is unknown.
- The 3- and 6-node dumps are missing about a quarter of their CPU readings, so their CPU numbers, especially
  peaks, are less reliable. As a missing reading adds nothing to a recorded total, their graphs mark each moment
  whose total is more than 5% too low and add a dashed estimate with the missing readings filled in, and each
  run's `summary.json` says how many readings are missing. A missing reading at the very start or end of a
  copy's readings can't be filled in, so it isn't marked.
- The CPU metric drops readings of exactly zero, so idle containers show fewer readings.
- Churn phases of 20–23 minutes have only about 40 to 47 readings per copy, so a copy's CPU p95 is about its
  third-highest reading.
- The runs weren't set up the same way: kube-burner and OpenShift versions, churn delay and actual churn length
  differ.
