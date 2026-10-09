#!/usr/bin/env python3
"""
Fetch live-style departure times for a LINZ AG LINIEN stop from the public EFA API.

Uses the stateless display-monitor pattern (same as gjuttla/linz-linien-efa):
  GET .../static/XML_DM_REQUEST?outputFormat=JSON&stateless=1&type_dm=any&mode=direct&name_dm=<stopId>&limit=<n>

Stop IDs match the EFA system (e.g. Taubenmarkt = 60501160). Search stops via the website or STOPFINDER.

This is not an official “developer product”; use reasonably and respect operator terms.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any

EFA_DM = "https://www.linzag.at/static/XML_DM_REQUEST"


def fetch_departures(stop_id: str, limit: int = 20) -> dict[str, Any]:
    params = {
        "outputFormat": "JSON",
        "stateless": "1",
        "type_dm": "any",
        "mode": "direct",
        "name_dm": stop_id,
        "limit": str(limit),
    }
    url = EFA_DM + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "camera_test/linz_efa_departures (public EFA)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def simplify(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    dl = raw.get("departureList")
    if not dl:
        return out
    rows = dl if isinstance(dl, list) else [dl]
    for row in rows:
        dep = row.get("departure") if isinstance(row, dict) and "departure" in row else row
        if not isinstance(dep, dict):
            continue
        line = dep.get("servingLine") or {}
        dt = dep.get("dateTime") or {}
        out.append(
            {
                "line": line.get("number"),
                "direction": line.get("direction"),
                "countdown_minutes": dep.get("countdown"),
                "departs_at": {
                    "year": dt.get("year"),
                    "month": dt.get("month"),
                    "day": dt.get("day"),
                    "hour": dt.get("hour"),
                    "minute": dt.get("minute"),
                },
            }
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="LINZ AG EFA departure board (JSON)")
    p.add_argument("stop_id", help="EFA stop ID, e.g. 60501160 (Taubenmarkt)")
    p.add_argument("-n", "--limit", type=int, default=20, help="max departures")
    p.add_argument(
        "--raw",
        action="store_true",
        help="print full API JSON instead of simplified list",
    )
    args = p.parse_args()

    try:
        raw = fetch_departures(args.stop_id, args.limit)
    except urllib.error.HTTPError as e:
        print(e.read().decode()[:500], file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if args.raw:
        json.dump(raw, sys.stdout, indent=2)
    else:
        json.dump(simplify(raw), sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
