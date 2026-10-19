#!/usr/bin/env python3
"""Fetch Azure's list price of a control-plane machine in every region, for the PM page's costs.

Asks Azure's public Retail Prices API (https://prices.azure.com/api/retail/prices) for MACHINE's Linux pay-as-you-go
price per hour, without Spot or Low Priority, in every region, and writes machine-prices.json next to this script.
The PM page works out costs per core from it, for all of a management cluster's machines, so they're list prices of
one machine type: production's machines can be other types in some regions, and discounts such as reservations
aren't included. Rerun it to update the prices; the page needs no network connection.

Usage:
  python3 fetch_prices.py [--machine NAME] [--output FILE]
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "machine-prices.json")
API = "https://prices.azure.com/api/retail/prices"
# The control-plane machine the public config names (mgmt.aks.userAgentPool.vmSize in config/config.yaml)
MACHINE = "Standard_E32ds_v6"
MACHINE_CORES = 32


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--machine", default=MACHINE, help=f"the machine type to price (default: {MACHINE})")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="file to write (default: machine-prices.json next to this script)")
    return parser.parse_args()


def fetch(machine):
    """Every price item the API has for the machine, across its pages."""
    query = (f"serviceName eq 'Virtual Machines' and armSkuName eq '{machine}' and priceType eq 'Consumption'")
    url, items = f"{API}?{urllib.parse.urlencode({'$filter': query})}", []
    while url:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                page = json.load(response)
        except (OSError, ValueError) as error:
            sys.exit(f"couldn't fetch prices from {API}: {error}")
        items += page.get("Items", [])
        url = page.get("NextPageLink")
    return items, query


def hourly_prices(items):
    """{region: hourly price in USD}: Linux pay-as-you-go, without Spot or Low Priority, the newest in each region."""
    newest = {}
    for item in items:
        if ("Windows" in item["productName"] or "Spot" in item["skuName"] or "Low Priority" in item["skuName"]
                or item["unitOfMeasure"] != "1 Hour" or item["currencyCode"] != "USD" or not item["armRegionName"]):
            continue
        region = item["armRegionName"]
        if region not in newest or item["effectiveStartDate"] > newest[region]["effectiveStartDate"]:
            newest[region] = item
    return {region: newest[region]["unitPrice"] for region in sorted(newest)}


def main():
    args = parse_args()
    items, query = fetch(args.machine)
    prices = hourly_prices(items)
    if not prices:
        sys.exit(f"the API has no Linux pay-as-you-go prices for {args.machine}")
    output = {
        "description": "Azure's list price of one control-plane machine per hour in each region, written by "
                       "hack/hcp-scale-analysis/fetch_prices.py",
        "source": API,
        "filter": query,
        "machine": args.machine,
        "cores": MACHINE_CORES,
        "currency": "USD",
        "purchase_model": "Linux pay-as-you-go",
        "retrieved": dt.date.today().isoformat(),
        "hourly": prices,
    }
    target = os.path.expanduser(args.output)
    with open(target, "w") as f:
        f.write(json.dumps(output, indent=2) + "\n")
    print(f"wrote {target}: {args.machine} in {len(prices)} regions", file=sys.stderr)


if __name__ == "__main__":
    main()
