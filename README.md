# Fuel Route Optimizer

A Django REST Framework backend that plans a driving trip between two United
States locations and computes the **cost-optimal fuel stops** along the route.

Given a start and a finish location the API:

1. Geocodes both locations (Nominatim / OpenStreetMap) and verifies they are
   inside the USA.
2. Calculates the driving route with OSRM (Open Source Routing Machine).
3. Matches fuel stations from the provided OPIS price CSV to a corridor
   around the route.
4. Computes the cheapest refueling plan that never exceeds the vehicle's
   500-mile tank range (10 MPG, 50-gallon tank).
5. Returns everything a map frontend needs: route GeoJSON, station markers,
   per-stop prices/gallons/costs and the total fuel cost.

No API keys are required. The application uses the bundled dataset plus the
free Nominatim and OSRM services when a local lookup or cached result is not
available.

---

## Video Walkthrough

Watch the project demonstration and API walkthrough on Loom:

[▶️ Spotter - Django Fuel Route Optimizer API](https://www.loom.com/share/42701635e24f49eeb61dc0f3fcaa0f3b)

The recording is approximately 11 minutes long.

---

## Features

```
POST /api/route/            { "start": "New York, NY", "finish": "Chicago, IL" }
        |
        |---- Geocoding service   -> Nominatim (OpenStreetMap)
        |---- Routing service     -> OSRM (driving profile)
        |---- Fuel data service   -> provided OPIS CSV (loaded once, cached)
        |---- Fuel optimizer      -> corridor matching + greedy optimization
        |
        '---- JSON response (trip, fuel stops, costs, route GeoJSON)
```

- US-only location validation
- Driving routes with GeoJSON geometry and turn-by-turn steps
- Fuel-station corridor matching from the bundled OPIS CSV
- Cost-optimal fuel-stop planning with a 500-mile maximum tank range
- SQLite geocoding cache and bounded in-process route/plan caches
- JSON error responses with meaningful HTTP status codes

Business logic lives in `routes/services/`; Django views orchestrate the
request and response flow.

---

## Technology Stack

| Concern         | Technology                                            |
| --------------- | ----------------------------------------------------- |
| Language        | Python 3.10+ (developed and tested on Python 3.12)    |
| Framework       | Django 6.1                                            |
| API             | Django REST Framework 3.18                            |
| Database        | SQLite (via Django ORM)                               |
| Geocoding       | OpenStreetMap **Nominatim**                           |
| Routing         | **OSRM** (`router.project-osrm.org`, driving profile) |
| Fuel prices     | Provided `fuel-prices-for-be-assessment.csv`          |
| Geospatial math | `numpy` (haversine / projection, vectorized)          |
| HTTP client     | `requests`                                            |
| Configuration   | `.env` file via `python-dotenv`                       |

---

## API

### `POST /api/route/`

Request body:

```json
{
  "start": "New York, NY",
  "finish": "Chicago, IL"
}
```

`start` / `finish` accept normal US location inputs ("Chicago, IL",
"Seattle, WA", ...) as well as more specific addresses where Nominatim can
resolve them.

There is also a small self-describing `GET /api/route/` endpoint that returns
the endpoint summary and the configured vehicle assumptions.

### Example

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
     -H "Content-Type: application/json" \
     -d '{"start": "New York, NY", "finish": "Chicago, IL"}'
```

### Example response (abridged)

```json
{
  "trip": {
    "start": {
      "input": "New York, NY",
      "latitude": 40.7127281,
      "longitude": -74.0060152,
      "display_name": "New York, United States",
      "state": "New York"
    },
    "finish": {
      "input": "Chicago, IL",
      "latitude": 41.8755616,
      "longitude": -87.6244212,
      "display_name": "Chicago, South Chicago Township, Cook County, Illinois, United States",
      "state": "Illinois"
    },
    "distance_miles": 790.6,
    "duration_minutes": 891,
    "duration_text": "14h 51m"
  },

  "vehicle": {
    "max_range_miles": 500.0,
    "fuel_efficiency_mpg": 10.0,
    "fuel_capacity_gallons": 50.0
  },

  "fuel_stops": [
    {
      "sequence": 1,
      "station_id": "72445",
      "station_name": "SHEETZ #639",
      "address": "I-80 EXIT 223 & OH-193",
      "city": "Youngstown",
      "state": "OH",
      "latitude": 41.054,
      "longitude": -80.662,
      "price_per_gallon": 3.059,
      "distance_from_start_miles": 391.0,
      "distance_from_route_miles": 3.78,
      "gallons_purchased": 5.51,
      "fuel_cost": 16.86,
      "reason": "Bought only enough fuel to reach the next cheaper station ..."
    }
  ],

  "fuel_summary": {
    "total_distance_miles": 790.6,
    "total_fuel_consumed_gallons": 79.06,
    "total_fuel_purchased_gallons": 29.06,
    "total_fuel_cost": 87.72,
    "fuel_remaining_at_destination_gallons": 0.0,
    "stops_required": 2,
    "note": "The vehicle starts with a full tank (50 gallons / 500 miles). ..."
  },

  "route": {
    "geometry": {
      "type": "LineString",
      "coordinates": [[-74.006, 40.7127], [-74.01, 40.714], "..."]
    },
    "start_snapped": [-74.006, 40.7127],
    "end_snapped": [-87.624, 41.8755],
    "steps": [
      {
        "name": "I-80",
        "maneuver": "merge",
        "distance_miles": 12.4,
        "duration_seconds": 700.0
      }
    ]
  },

  "metadata": {
    "fuel_stations_in_dataset": 6624,
    "stations_in_route_bounding_box": 638,
    "stations_within_corridor": 369,
    "corridor_radius_miles": 25.0,
    "algorithm": "Greedy gas-station optimization with look-ahead ..."
  }
}
```

The route `geometry` is standard GeoJSON, and every fuel stop carries
latitude/longitude, so a frontend can render start marker, route line, stop
markers and destination marker directly.

---

## Error Handling

All errors are returned as `{"error": "<human readable message>"}` with an
appropriate status code:

| Status | Meaning                                                                                                                    |
| ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `400`  | Invalid input: missing/empty fields, identical start & finish, malformed JSON                                              |
| `404`  | Location could not be geocoded, or is not within the United States                                                         |
| `422`  | Route is longer than the vehicle range and no usable fuel station exists within reach (including gaps in station coverage) |
| `502`  | Nominatim/OSRM unreachable, timed out, or rate limiting                                                                    |
| `500`  | Unexpected server error (message only - **no stack traces are exposed**)                                                   |

Examples:

```json
{"error": "Unable to geocode the location 'Xyzzyville, ZZ'."}            // 404
{"error": "'Toronto, Canada' was found, but it is not within the United States."}  // 404
{"error": "Start and finish must be different locations."}               // 400
{"error": "No fuel station within 500 miles of route position 300 mi; ..."}  // 422
```

---

## Installation And Usage

Requires **Python 3.10+**. On Windows, use the virtual-environment Python
executable explicitly if activation is unavailable in your terminal.

```bash
# Create a virtual environment from the repository root
python -m venv venv

# Windows PowerShell
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Optional: create a .env file in the repository root and override settings.
# Defaults are defined in fuel_route_optimizer/settings.py.
```

On Windows, the equivalent commands without activation are:

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py runserver
```

### Database Setup

```bash
python manage.py migrate
```

SQLite (`db.sqlite3`) is created automatically. The only model is a
`GeocodingCache` used to remember previous Nominatim results.

### Run The Server

```bash
python manage.py runserver
```

The API is then available at `http://127.0.0.1:8000/api/route/`.

### Run Tests

```bash
python manage.py test
```

The suite (65 tests) covers the optimizer, geo math, CSV loading, geocoding
and the full endpoint. **All external HTTP calls are mocked** - tests never
touch Nominatim/OSRM.

### Configuration

The application has sensible defaults and does not require a `.env` file.
Useful overrides include:

| Setting                            | Default | Purpose                                            |
| ---------------------------------- | ------: | -------------------------------------------------- |
| `MAX_RANGE_MILES`                  |   `500` | Maximum distance on a full tank                    |
| `FUEL_EFFICIENCY_MPG`              |    `10` | Vehicle fuel efficiency                            |
| `FUEL_STATION_SEARCH_RADIUS_MILES` |    `25` | Station corridor width                             |
| `ROUTE_DENSIFY_STEP_MILES`         |   `0.5` | Route sampling resolution                          |
| `LOCAL_CITY_COORDINATES_ENABLED`   |  `True` | Use bundled coordinates for exact `City, ST` input |
| `ROUTE_CACHE_ENABLED`              |  `True` | Cache repeated OSRM routes in-process              |
| `ROUTE_CACHE_MAX_ENTRIES`          |   `128` | Maximum cached OSRM routes                         |
| `ROUTE_PLAN_CACHE_ENABLED`         |  `True` | Cache repeated successful plans in-process         |
| `ROUTE_PLAN_CACHE_MAX_ENTRIES`     |    `64` | Maximum cached complete plans                      |
| `EXTERNAL_API_TIMEOUT_SECONDS`     |    `15` | Nominatim/OSRM request timeout                     |

In production, set a strong `SECRET_KEY`, configure `DEBUG=False`, and set
explicit `ALLOWED_HOSTS` values.

### Expected latency

Using the free public services, first-time requests are primarily limited by
the two geocoding calls and one OSRM route. Exact `City, ST` inputs use the
bundled coordinate table without geocoding network calls. Repeated routes are
served from bounded in-process route and full-plan caches; the fuel CSV is
parsed only once per process. Very long or uncached routes can still take
longer depending on the shared OSRM demo server's load.

---

## How the Fuel-Stop Optimization Works

This section explains the algorithm end-to-end so the behaviour is fully
reproducible and reviewable.

### 1. Route distance and geometry

OSRM's driving profile is queried with `overview=full&geometries=geojson&steps=true`.
OSRM reports distance in meters; the API converts to miles
(`miles = meters / 1609.344`) and duration to minutes/hours. The GeoJSON
LineString geometry is returned verbatim for map rendering.

### 2. Matching fuel stations to the route (corridor)

The provided CSV has **no coordinates** - it identifies stations by
City + State. Coordinates are resolved from a bundled US city table
(`data/city_coordinates.json`, 29,744 entries built from a public US cities
dataset plus Nominatim); any city missing from the table is geocoded once at
runtime and cached in the database, so an updated CSV keeps working without
code changes.

Stations are then matched to the route:

1. The route polyline is **densified** at 0.5-mile resolution
   (`ROUTE_DENSIFY_STEP_MILES`), so long straight highway segments cannot
   "hide" nearby stations.
2. A bounding-box pre-filter discards stations far away from the route
   (cheap, vectorized).
3. Each remaining station is **projected onto the densified polyline**
   (nearest point + exact segment projection), yielding
   - position along the route (`distance_from_start_miles`), and
   - perpendicular distance off the route (`distance_from_route_miles`).
4. Stations within `FUEL_STATION_SEARCH_RADIUS_MILES` (25 mi) become
   **candidates**; the along-route distances are scaled so they sum exactly
   to the OSRM route distance.

No external API call is made per station - the matching runs entirely
against the in-memory CSV data (one CSV parse per process, cached).

### 3. The 500-mile constraint

The vehicle starts with a **full tank** = `MAX_RANGE_MILES` (500) miles of
range. Internally the optimizer tracks _remaining range in miles_; every
leg driven reduces it, every purchase increases it, and it can never exceed
500 miles. This makes the constraint trivially enforceable: **no driving
segment between consecutive stops (or between start/destination and a stop)
may exceed 500 miles.** A plan that would require more is rejected (HTTP 422) rather than violated.

### 4. How prices affect station selection (the greedy with look-ahead)

At every decision point (the start, or the station currently stopped at) the
optimizer looks at all candidates within one **full tank** ahead and applies,
in order:

1. **Destination within current fuel?** Stop - buy nothing more.
2. **A strictly cheaper station within one tank's reach?**
   Buy at the current station _only the fuel needed to reach the nearest
   such station_, then re-evaluate there.
   _(Buying more than that at a pricier station can never be optimal - the
   deferred gallons can be bought cheaper later.)_
3. **Nothing cheaper in reach, but the remainder of the trip fits in one
   tank?** Buy _exactly_ the fuel needed to finish the trip.
4. **Nothing cheaper in reach and more fuel will be needed later?**
   Fill the tank to capacity (every gallon bought at the current
   cheapest-available price displaces a gallon bought at a higher price) and
   drive to the _cheapest reachable_ station ahead.

Co-located stations (same projected position) collapse to the cheapest one.
The result is **deterministic and explainable** - each stop in the response
carries a human-readable `reason` describing why it was chosen.

This greedy strategy is the classical solution to the bounded-capacity "gas
station problem" and produces a cost-optimal plan for this problem class.

**Note on "micro-stops":** when several stations a few miles apart get
progressively cheaper, the optimizer buys a trivial amount at each and
defers the rest to the cheapest one. That is cost-optimal (the criterion is
_minimize total fuel cost_); every stop's `reason` field explains it.

### 5. Fuel consumption

`fuel_needed_gallons = segment_distance_miles / FUEL_EFFICIENCY_MPG`
(10 MPG). E.g. 100 mi -> 10 gal, 250 mi -> 25 gal, 500 mi -> 50 gal.

### 6. Fuel cost

Per stop: `fuel_cost = gallons_purchased x price_per_gallon` (the CSV's
`Retail Price`, treated as USD per gallon). The response's
`total_fuel_cost` is the sum of the per-stop costs;
`total_fuel_consumed_gallons = total_distance / 10` covers the whole trip.
Purchases are rounded to 2 decimals so the numbers in the response are
internally consistent (`fuel_cost == gallons x price`).

### 7. Trips shorter than 500 miles

**No intermediate stop is required.** The vehicle starts with a full tank,
so `total_fuel_purchased_gallons` is `0` and `total_fuel_cost` is `$0.00`
even though `total_fuel_consumed_gallons` is positive. The API distinguishes
explicitly between **fuel consumed** (the whole trip) and **fuel purchased**
(fuel actually bought at stops) and explains this in
`fuel_summary.note`.

### 8. Trips longer than 500 miles

The optimizer automatically produces as many stops as the route requires -
nothing is hardcoded. A typical New York -> Chicago plan (790.6 mi) buys a
top-up at the first cheaper station, then exactly enough fuel to finish at
the cheapest station within range.

### 9. If no valid station exists

If the remaining route cannot be covered - e.g. a gap of more than 500 miles
between usable stations - the API fails with HTTP `422` and a message that
names the route position where coverage breaks, instead of returning an
impossible plan.

---

## Assumptions

- The vehicle **starts with a full tank** (50 gallons / 500 miles).
- Efficiency is exactly **10 MPG**; max range is **500 miles** (both
  configurable in settings/`.env`).
- The CSV is the **authoritative fuel-price dataset**. `Retail Price` is
  treated as **USD per gallon**. The file contains no fuel-type column, so
  the price is used as the single available retail price for the trip.
- The CSV also contains Canadian rows; the loader keeps only rows whose
  state is a US state code.
- Duplicate records for the same truck stop (same OPIS ID, or same
  name+address with a different ID) are collapsed, keeping the **first
  occurrence** for determinism.
- Station coordinates are resolved at **city level** (the CSV has street
  intersections but no lat/lon; a free city-level lookup is used). The
  25-mile corridor is wide enough to absorb city-centroid offsets.
- Only stations within the configured corridor (25 miles of the route) are
  considered; a stop never requires a long detour.
- Detour distances to/from a station are **not** added to the fuel cost;
  purchases are computed from along-route segment distances.
- The route is a **driving route** (OSRM driving profile). Ferries/seasonal
  roads follow whatever OSRM's public server returns.
- Fuel prices are treated as static for the duration of the trip.
- Geocoding results are cached in SQLite; each distinct location string
  costs at most one Nominatim call per deployment, and outbound calls are
  spaced >= 1.1 s apart in line with Nominatim's usage policy.

---

## Project Structure

```
fuel_route_optimizer/
├── manage.py                    # Django command-line entry point
├── requirements.txt             # Python dependencies
├── LICENSE                      # MIT License
├── fuel_route_optimizer/        # Django project package
│   ├── settings.py              # all business constants from .env
│   ├── urls.py                  # routes the /api/ prefix
│   ├── test_runner.py           # robust default test discovery
│   ├── wsgi.py
│   └── asgi.py
├── routes/                      # the application
│   ├── views.py                 # thin orchestration only
│   ├── serializers.py           # request validation + response contract
│   ├── urls.py                  # POST /api/route/
│   ├── models.py                # GeocodingCache
│   ├── exceptions.py            # domain errors -> HTTP status mapping
│   ├── exception_handler.py     # {"error": ...} JSON for every failure
│   ├── services/
│   │   ├── geocoding.py         # Nominatim + DB cache
│   │   ├── routing.py           # OSRM driving route
│   │   ├── fuel_data.py         # CSV loading/validation/caching
│   │   ├── fuel_optimizer.py    # corridor matching + optimization
│   │   └── geo_utils.py         # haversine, densify, point-to-route
│   └── tests/                   # 65 tests, external APIs mocked
│       ├── test_api.py
│       ├── test_optimizer.py
│       ├── test_fuel_data.py
│       ├── test_geocoding.py
│       └── test_geo_utils.py
└── data/
    ├── fuel-prices-for-be-assessment.csv   # provided dataset
    └── city_coordinates.json               # bundled city -> (lat, lon)
```

---

## Security Notes

- `.env` is git-ignored and should never contain committed secrets.
- Error responses never include stack traces or internal details.
- User input is limited to two short location strings - validated by a DRF
  serializer; no file uploads, no user-controlled URLs (Nominatim/OSRM
  endpoints come from settings only).
- `DEBUG` and `ALLOWED_HOSTS` must be set appropriately for production; use a
  local `.env` file or deployment environment variables.
