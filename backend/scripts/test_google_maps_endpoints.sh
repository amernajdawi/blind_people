#!/usr/bin/env bash
# Exercise Google Maps proxy routes on the local API. Loads .env from repo root or backend parent.
# Usage:  ./scripts/test_google_maps_endpoints.sh [BASE_URL]
# Example: ./scripts/test_google_maps_endpoints.sh http://127.0.0.1:8000

set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"

echo "=== GET $BASE/api/maps/status ==="
curl -sS "$BASE/api/maps/status" | python3 -m json.tool || true

echo
echo "=== POST reverse-geocode (Linz Hauptplatz area) ==="
curl -sS -X POST "$BASE/api/maps/reverse-geocode" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":48.3069,"longitude":14.2858}' | python3 -m json.tool | head -40

echo
echo "=== POST weather (same coords) ==="
curl -sS -X POST "$BASE/api/maps/weather" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":48.3069,"longitude":14.2858}' | python3 -m json.tool | head -30

echo
echo "=== POST routes WALK (short) ==="
curl -sS -X POST "$BASE/api/maps/routes/compute" \
  -H 'Content-Type: application/json' \
  -d '{
    "origin": {"address": "Hauptplatz, Linz, Austria"},
    "destination": {"address": "LINZ Donau Universität, Linz, Austria"},
    "travel_mode": "WALK",
    "include_llm_markdown": true
  }' | python3 -m json.tool | head -50

echo
echo "Done. If you see 503, set GOOGLE_MAPS_API_KEY and GOOGLE_MAPS_PLATFORM_ENABLED=true in .env and restart the API."
echo "If nearby returns empty markdown, set GOOGLE_MAPS_NEARBY_INCLUDED_TYPES in .env (see .env.example)."
