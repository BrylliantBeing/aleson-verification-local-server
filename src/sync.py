"""Download one trip's ticket data from the cloud API into local Postgres.

Before a boarding session the gate laptop downloads just the upcoming trip
being boarded (not the whole ticket history) via the central backend's
GET /api/v1/verification/tickets?trip_id=... export, and rebuilds the local
`verification_tickets` table in one transaction, so the gate keeps verifying
against the previous copy if a sync dies halfway. The list of trips to pick
from comes from GET /api/v1/trips.

Run directly (`python -m src.sync <trip_id>`) or through the API (`POST /sync`).
"""

import os
import sys
from datetime import datetime, timedelta

import httpx
import psycopg

CLOUD_API_URL = os.getenv("CLOUD_API_URL", "https://aleson-test-2.brylletan.com").rstrip("/")
LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://aleson_local:aleson_local_gate@localhost:5433/aleson_db",
)
PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "20000"))
# Trips that departed within this window still show in the picker, so a trip
# that is already boarding doesn't disappear from the list mid-session.
UPCOMING_GRACE = timedelta(hours=int(os.getenv("TRIP_GRACE_HOURS", "3")))

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS verification_tickets (
        id INT PRIMARY KEY,
        qr_token VARCHAR(36) UNIQUE NOT NULL,
        passenger_first_name TEXT NOT NULL,
        passenger_last_name TEXT NOT NULL,
        seat_number TEXT NOT NULL,
        status TEXT NOT NULL,
        accommodation_class TEXT,
        scheduled_departure TIMESTAMP NULL,
        vessel TEXT,
        origin_port TEXT NULL,
        destination_port TEXT NULL,
        boarding_status TEXT NOT NULL DEFAULT 'Not Boarded',
        boarded_at TIMESTAMP NULL
    );
"""

# Migrations for tables created before the boarding columns existed. Run one
# statement per execute() — psycopg only runs a single command per call.
MIGRATE_STATEMENTS = (
    "ALTER TABLE verification_tickets "
    "ADD COLUMN IF NOT EXISTS boarding_status TEXT NOT NULL DEFAULT 'Not Boarded';",
    "ALTER TABLE verification_tickets "
    "ADD COLUMN IF NOT EXISTS boarded_at TIMESTAMP NULL;",
)

# Columns loaded from the cloud export. boarding_status / boarded_at are
# intentionally omitted so they take their table defaults on load.
COLUMNS = (
    "id", "qr_token", "passenger_first_name", "passenger_last_name",
    "seat_number", "status", "accommodation_class", "scheduled_departure",
    "vessel", "origin_port", "destination_port",
)


class SyncError(RuntimeError):
    pass


def fetch_all_tickets(trip_id: int) -> list[dict]:
    tickets: list[dict] = []
    after_id = 0
    with httpx.Client(timeout=120) as client:
        while True:
            try:
                response = client.get(
                    f"{CLOUD_API_URL}/api/v1/verification/tickets",
                    params={"after_id": after_id, "limit": PAGE_SIZE, "trip_id": trip_id},
                )
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise SyncError(f"cloud API request failed: {e}") from e
            page = response.json().get("tickets", [])
            tickets.extend(page)
            if len(page) < PAGE_SIZE:
                return tickets
            after_id = page[-1]["id"]


def fetch_upcoming_trips() -> list[dict]:
    """Trips available to board, closest departure first.

    Pulls the cloud trip list, drops cancelled trips and anything that
    departed more than UPCOMING_GRACE ago, and sorts ascending by departure.
    """
    with httpx.Client(timeout=60) as client:
        try:
            response = client.get(f"{CLOUD_API_URL}/api/v1/trips")
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise SyncError(f"cloud API request failed: {e}") from e

    cutoff = datetime.now() - UPCOMING_GRACE
    upcoming: list[tuple[datetime, dict]] = []
    for trip in response.json():
        if (trip.get("status") or "").lower() == "cancelled":
            continue
        departure_raw = trip.get("scheduled_departure")
        if not departure_raw:
            continue
        try:
            departure = datetime.fromisoformat(departure_raw)
        except ValueError:
            continue
        if departure < cutoff:
            continue
        upcoming.append((departure, trip))

    upcoming.sort(key=lambda pair: pair[0])
    return [trip for _, trip in upcoming]


def sync_database(trip_id: int) -> dict:
    """Download one trip's tickets from the cloud API and rebuild the table."""
    if trip_id is None:
        raise SyncError("trip_id is required")
    tickets = fetch_all_tickets(trip_id)

    try:
        with psycopg.connect(LOCAL_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
                for statement in MIGRATE_STATEMENTS:
                    cur.execute(statement)
                cur.execute("TRUNCATE verification_tickets;")
                with cur.copy(
                    f"COPY verification_tickets ({', '.join(COLUMNS)}) FROM STDIN"
                ) as copy:
                    for t in tickets:
                        copy.write_row(tuple(t.get(col) for col in COLUMNS))
    except psycopg.Error as e:
        raise SyncError(f"local database load failed: {e}") from e

    return {"status": "success", "trip_id": trip_id, "tickets": len(tickets)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.sync <trip_id>", file=sys.stderr)
        sys.exit(2)
    try:
        trip_id = int(sys.argv[1])
    except ValueError:
        print(f"Invalid trip_id: {sys.argv[1]!r}", file=sys.stderr)
        sys.exit(2)
    try:
        result = sync_database(trip_id)
    except SyncError as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Sync complete: {result['tickets']} tickets for trip {trip_id} "
        f"downloaded from {CLOUD_API_URL}"
    )
