"""Download ticket data from the cloud API into the local Postgres.

Pages through the central backend's GET /api/v1/verification/tickets export
and rebuilds the local `verification_tickets` table in one transaction, so
the gate keeps verifying against the previous copy if a sync dies halfway.
Run directly (`python src/sync.py`) or through the API (`POST /sync`).
"""

import os
import sys

import httpx
import psycopg

CLOUD_API_URL = os.getenv("CLOUD_API_URL", "https://aleson-test-2.brylletan.com").rstrip("/")
LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://aleson_local:aleson_local_gate@localhost:5433/aleson_db",
)
PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "20000"))

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
        destination_port TEXT NULL
    );
"""

COLUMNS = (
    "id", "qr_token", "passenger_first_name", "passenger_last_name",
    "seat_number", "status", "accommodation_class", "scheduled_departure",
    "vessel", "origin_port", "destination_port",
)


class SyncError(RuntimeError):
    pass


def fetch_all_tickets() -> list[dict]:
    tickets: list[dict] = []
    after_id = 0
    with httpx.Client(timeout=120) as client:
        while True:
            try:
                response = client.get(
                    f"{CLOUD_API_URL}/api/v1/verification/tickets",
                    params={"after_id": after_id, "limit": PAGE_SIZE},
                )
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise SyncError(f"cloud API request failed: {e}") from e
            page = response.json().get("tickets", [])
            tickets.extend(page)
            if len(page) < PAGE_SIZE:
                return tickets
            after_id = page[-1]["id"]


def sync_database() -> dict:
    """Download all tickets from the cloud API and rebuild the local table."""
    tickets = fetch_all_tickets()

    try:
        with psycopg.connect(LOCAL_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
                cur.execute("TRUNCATE verification_tickets;")
                with cur.copy(
                    f"COPY verification_tickets ({', '.join(COLUMNS)}) FROM STDIN"
                ) as copy:
                    for t in tickets:
                        copy.write_row(tuple(t.get(col) for col in COLUMNS))
    except psycopg.Error as e:
        raise SyncError(f"local database load failed: {e}") from e

    return {"status": "success", "tickets": len(tickets)}


if __name__ == "__main__":
    try:
        result = sync_database()
    except SyncError as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Sync complete: {result['tickets']} tickets downloaded from {CLOUD_API_URL}")
