# HCP scale-test analysis

Scripts that turn raw kube-burner scale-test dumps into per-container control-plane usage for each
hosted-cluster size, plus a note on how much to trust each size.

## Files

| File | What it is |
|---|---|
| `plot_controlplane_usage.py` | Graphs one run's total control-plane CPU and memory over time, into `graphs/<run>/` |
| `container_usage.py` | Prints one run's per-container usage: every container, or each copy of one container with `--container` |
| `combine_usage.py` | Works out per-container usage for many runs and writes `container-usage.json` |
| `run-notes.json` | Hand-written trust note for each run (`good`, `caveats` or `low`, and why) |
| `container-usage.json` | Generated: per-container usage of every run, with its trust note. The input for comparing sizes and for the PM page |
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

4. Commit `graphs/`, `run-notes.json` and `container-usage.json`.

To look at one run in detail:

    $PY container_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz --top 10
    $PY container_usage.py ~/Downloads/aro-hcp-raw-usage/<run>.ndjson.gz --container kube-apiserver

Graph images depend on the matplotlib version, so regenerating them elsewhere can change the PNG files even when
the numbers don't change.

## How the numbers are worked out

1. Keep only the hosted cluster under test: the namespace containing the dump's `jobSummary.clusterName`. Some
   dumps also contain other clusters.
2. Count each reading once (the 250- and 500-node dumps contain exact duplicates) and skip pause containers.
3. Use only the steady part of the run: the kube-burner churn phase. The 500-node run has none, so its memory
   uses the last 10 minutes, once memory has leveled off, and its CPU uses the whole run, which is all create
   phase and so a conservative estimate.
4. Keep every copy (pod) of every container separate. For CPU, first average each copy's readings over
   5 minutes, so bursts lasting seconds don't count. Missing readings are skipped, never counted as zero.
5. For each copy, take the median (p50, typical) and p95 (busy moments).
6. Combine a container's copies two ways: the busiest copy (the highest value among the copies, for each
   percentile separately) and the average copy.

## Using the numbers

- **Typical total use of a cluster:** add up copies x `average_copy.p50` over all containers. This closely
  matches the median of the cluster's total (within 1% for memory and about 3% for CPU, except the 3- and
  6-node CPU).
- **Reservations:** every copy of a container gets the same request, so a request has to fit `busiest_copy`.
- **Don't add up p95s** to get a cluster's p95. Containers don't peak at the same moment, so the sum
  overstates it, by about 27% for CPU at 250 nodes.
- **CPU p50** comes from 5-minute averages, so it's close to the sustained average use, not the median of the
  raw readings.

## Known limitations

- Each size has only one test run, so how much the numbers vary between runs is unknown.
- The 3- and 6-node dumps are missing about a quarter of their CPU readings. Their CPU numbers, especially
  peaks, are less reliable, and their total-CPU graphs read low, as missing readings add nothing to the total.
- The CPU metric drops readings of exactly zero, so idle containers show fewer readings.
- Churn phases of 20–23 minutes give few 5-minute averages, so their CPU p95 is close to the highest value.
- The runs weren't set up the same way: kube-burner and OpenShift versions, churn delay and actual churn length
  differ.
