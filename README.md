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

1. **Download the ticket data** (needs internet to the central backend —
   pulls `GET /api/v1/verification/tickets` page by page into the local
   `verification_tickets` table):

   ```powershell
   # while the stack is up:
   curl.exe -X POST http://localhost:8001/sync
   # or run the sync script inside the container:
   docker compose exec api python -m src.sync
   ```

   The table is rebuilt in a single transaction, so a failed sync leaves the
   previous copy intact.

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
| `GET /health` | — | `{"status": "ok", "tickets": N}` |
| `POST /sync` | — | `{"status": "success", "tickets": N}` |
| `POST /verify` | `{"qr_token": "<scanned string>"}` | success: `{"status": "success", "passenger_name", "route", "vessel", "seat_number", "accommodation_class", "departure", "ticket_status"}` · not found / cancelled / refunded: `{"status": "failed", "reason"}` |

Environment overrides (set in `docker-compose.yml`, all optional):
`CLOUD_API_URL` (central backend, default
`https://aleson-test-2.brylletan.com`), `LOCAL_DATABASE_URL`,
`SYNC_PAGE_SIZE`.
