# aleson-verification-local-server

FastAPI server for boarding-gate ticket verification. Runs on the gate
laptop with a local Postgres copy of the cloud ticket data, so scanning
keeps working with no internet at the pier.

Both the database and the server run in Docker — `docker compose up`
starts everything.

## One-time setup

```powershell
# builds the app image, starts Postgres (5433) + the API (8001)
docker compose up -d --build
```

`db` is Postgres 17 (host port 5433, container `aleson-verification-db`).
`api` is the FastAPI server (host port 8001, container
`aleson-verification-api`), which waits for the DB to be healthy before
starting.

## Before each boarding session

1. **Download the trip you're boarding** (needs internet to the central
   backend). Open the gate web page in a browser on the laptop:

   ```
   http://localhost:8001/
   ```

   It lists the upcoming trips (closest departure first). Click **Download**
   on the trip you're boarding — that pulls only that trip's tickets
   (`GET /api/v1/verification/tickets?trip_id=…`) into the local
   `verification_tickets` table and shows live boarding progress ("X of Y
   boarded") as scans come in.

   Command-line equivalents (need a trip id — get one from `GET /trips`):

   ```powershell
   # while the stack is up:
   curl.exe -X POST http://localhost:8001/sync -H "Content-Type: application/json" -d "{\"trip_id\": 123}"
   # or run the sync script inside the container:
   docker compose exec api python -m src.sync 123
   ```

   The table is rebuilt in a single transaction, so a failed sync leaves the
   previous copy intact. Re-downloading a trip resets its boarding marks back
   to "Not Boarded".

2. **Start the hotspot** (Windows: Settings → Network & internet → Mobile
   hotspot) and connect the tablets to it. Find the laptop's hotspot IP with
   `ipconfig` (usually `192.168.137.1`) — the tablets point at
   `http://<that-ip>:8001`.

### Running the server without Docker (dev)

The project root is also a Python venv, so you can run the app directly:

```powershell
.\Scripts\pip.exe install -r requirements.txt
.\Scripts\python.exe -m uvicorn src.main:app --host 0.0.0.0 --port 8001
```

In that case `LOCAL_DATABASE_URL` defaults to `localhost:5433` (the
published DB port) instead of the in-network `db:5432`.

## API

| Endpoint | Body | Response |
|---|---|---|
| `GET /` | — | HTML gate page: pick a trip, download it, watch boarding progress |
| `GET /health` | — | `{"status": "ok", "tickets": N}` |
| `GET /trips` | — | `{"status": "success", "trips": [{id, origin, destination, vessel_name, scheduled_departure, ticket_count, ...}]}` — upcoming trips, closest first |
| `GET /trip_status` | — | `{"status": "ok", "total", "boarded", "vessel", "route", "departure"}` — progress for the loaded trip |
| `POST /sync` | `{"trip_id": N}` | `{"status": "success", "trip_id": N, "tickets": N}` (missing trip_id → `{"status": "failed", "reason"}`) |
| `POST /verify` | `{"qr_token": "<scanned string>"}` | success: `{"status": "success", "passenger_name", "route", "vessel", "seat_number", "accommodation_class", "departure", "ticket_status", "already_boarded", "boarded_at"}` — the ticket is flipped to `Boarded` on the first scan; `already_boarded` is true on repeat scans · not found / cancelled / refunded: `{"status": "failed", "reason"}` |

The local `verification_tickets` table adds two columns beyond the cloud
export: `boarding_status` (`Not Boarded` → `Boarded`) and `boarded_at`
(scan time), used for progress and duplicate-scan warnings.

Environment overrides (set in `docker-compose.yml`, all optional):
`CLOUD_API_URL` (central backend, default
`https://aleson-test-2.brylletan.com`), `LOCAL_DATABASE_URL`,
`SYNC_PAGE_SIZE`, `TRIP_GRACE_HOURS` (how long after departure a trip stays
in the picker, default 3).
