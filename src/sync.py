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

CLOUD_API_URL = os.getenv("CLOUD_API_URL", "https://aleson-shipping.com").rstrip("/")
LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://aleson_local:aleson_local_gate@localhost:5433/aleson_db",
)
PAGE_SIZE = int(os.getenv("SYNC_PAGE_SIZE", "20000"))
# Trips that departed within this window still show in the picker, so a trip
# that is already boarding doesn't disappear from the list mid-session.
UPCOMING_GRACE = timedelta(hours=int(os.getenv("TRIP_GRACE_HOURS", "3")))
# Pre-shared key for the cloud's gate-staff export, which serves password
# hashes and so is not public. Must match GATE_SYNC_SECRET on the backend.
GATE_SYNC_SECRET = os.getenv("GATE_SYNC_SECRET", "")

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS verification_tickets (
        id INT PRIMARY KEY,
        qr_token VARCHAR(36) UNIQUE NOT NULL,
        passenger_first_name TEXT NOT NULL,
        passenger_last_name TEXT NOT NULL,
        passenger_nationality TEXT,
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
    # Nationality is required on a coast-guard passenger manifest.
    "ALTER TABLE verification_tickets "
    "ADD COLUMN IF NOT EXISTS passenger_nationality TEXT;",
)

# Columns loaded from the cloud export. boarding_status is derived after the
# COPY (from boarded_at) rather than transferred, since the cloud has no such
# column — see sync_database.
COLUMNS = (
    "id", "qr_token", "passenger_first_name", "passenger_last_name",
    "passenger_nationality", "seat_number", "status", "accommodation_class",
    "scheduled_departure", "vessel", "origin_port", "destination_port",
    "boarded_at",
)

# Gate-staff accounts, synced down so logins on the console work with no
# internet at the pier. Refreshed alongside every ticket sync (see
# sync_database) rather than on its own schedule, since both already require
# the same cloud round trip.
GATE_STAFF_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS gate_staff (
        id INT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        synced_at TIMESTAMP NOT NULL DEFAULT now()
    );
"""

GATE_STAFF_COLUMNS = ("id", "name", "email", "password_hash")

# Which cloud trip the local tables currently hold. `verification_tickets` is a
# flat ticket export with no trip column, so without this the laptop knows every
# passenger on the sailing but not which trip row they belong to — and so cannot
# report the departure back up. Single row, id = 1.
LOCAL_STATE_SQL = """
    CREATE TABLE IF NOT EXISTS local_state (
        id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        trip_id INT,
        synced_at TIMESTAMP NOT NULL DEFAULT now(),
        manifest_printed_at TIMESTAMP NULL
    );
"""


def current_trip_id() -> int | None:
    """The trip the last /sync downloaded, or None on a laptop that predates
    local_state (or has never synced)."""
    try:
        with psycopg.connect(LOCAL_DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(LOCAL_STATE_SQL)
                cur.execute("SELECT trip_id FROM local_state WHERE id = 1")
                row = cur.fetchone()
        return row[0] if row else None
    except psycopg.Error as e:
        print(f"[local_state] could not read current trip: {e}")
        return None


def fetch_gate_staff() -> list[dict]:
    if not GATE_SYNC_SECRET:
        raise SyncError(
            "GATE_SYNC_SECRET is not set on this laptop — cannot download the "
            "gate-staff roster. Set it to match the backend and retry."
        )
    with httpx.Client(timeout=30) as client:
        try:
            response = client.get(
                f"{CLOUD_API_URL}/api/v1/gate-staff",
                headers={"X-Gate-Sync-Key": GATE_SYNC_SECRET},
            )
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise SyncError(f"cloud API request failed: {e}") from e
    return response.json().get("staff", [])


class SyncError(RuntimeError):
    pass


def fetch_all_tickets(trip_id: int) -> list[dict]:
    if not GATE_SYNC_SECRET:
        raise SyncError(
            "GATE_SYNC_SECRET is not set on this laptop - cannot download the "
            "trip manifest. Set it to match the backend and retry."
        )
    tickets: list[dict] = []
    after_id = 0
    with httpx.Client(timeout=120) as client:
        while True:
            try:
                response = client.get(
                    f"{CLOUD_API_URL}/api/v1/verification/tickets",
                    params={"after_id": after_id, "limit": PAGE_SIZE, "trip_id": trip_id},
                    headers={"X-Gate-Sync-Key": GATE_SYNC_SECRET},
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
    """Download one trip's tickets and the gate-staff roster from the cloud
    API and rebuild both local tables in one transaction.
    """
    if trip_id is None:
        raise SyncError("trip_id is required")
    tickets = fetch_all_tickets(trip_id)
    staff = fetch_gate_staff()

    try:
        with psycopg.connect(LOCAL_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
                for statement in MIGRATE_STATEMENTS:
                    cur.execute(statement)

                # Re-syncing mid-boarding must not lose who already boarded:
                # this table is rebuilt by TRUNCATE + COPY, so snapshot the
                # local boarding state first and re-apply it afterwards.
                # Without this, a second Download silently resets everyone to
                # 'Not Boarded' and duplicate-scan detection stops working.
                cur.execute("""
                    CREATE TEMP TABLE boarding_snapshot ON COMMIT DROP AS
                    SELECT qr_token, boarded_at FROM verification_tickets
                    WHERE boarding_status = 'Boarded';
                """)

                cur.execute("TRUNCATE verification_tickets;")
                with cur.copy(
                    f"COPY verification_tickets ({', '.join(COLUMNS)}) FROM STDIN"
                ) as copy:
                    for t in tickets:
                        copy.write_row(tuple(t.get(col) for col in COLUMNS))

                # Boarding recorded by any gate (the cloud now carries
                # boarded_at), then anything this laptop scanned that hasn't
                # reached the cloud yet — local wins, since it is strictly
                # newer than what we just downloaded.
                cur.execute("""
                    UPDATE verification_tickets
                    SET boarding_status = 'Boarded'
                    WHERE boarded_at IS NOT NULL;
                """)
                cur.execute("""
                    UPDATE verification_tickets vt
                    SET boarding_status = 'Boarded',
                        boarded_at = COALESCE(vt.boarded_at, s.boarded_at)
                    FROM boarding_snapshot s
                    WHERE vt.qr_token = s.qr_token;
                """)
                restored = cur.rowcount

                cur.execute(GATE_STAFF_TABLE_SQL)
                cur.execute("TRUNCATE gate_staff;")
                with cur.copy(
                    f"COPY gate_staff ({', '.join(GATE_STAFF_COLUMNS)}) FROM STDIN"
                ) as copy:
                    for s in staff:
                        copy.write_row(tuple(s.get(col) for col in GATE_STAFF_COLUMNS))

                # Remember which trip is loaded, and clear any previous
                # manifest-print stamp — a fresh download is a new boarding
                # session, so the next print must report departure again.
                cur.execute(LOCAL_STATE_SQL)
                cur.execute(
                    """
                    INSERT INTO local_state (id, trip_id, synced_at, manifest_printed_at)
                    VALUES (1, %s, now(), NULL)
                    ON CONFLICT (id) DO UPDATE
                        SET trip_id = EXCLUDED.trip_id,
                            synced_at = now(),
                            manifest_printed_at = NULL
                    """,
                    (trip_id,),
                )
    except psycopg.Error as e:
        raise SyncError(f"local database load failed: {e}") from e

    return {
        "status": "success",
        "trip_id": trip_id,
        "tickets": len(tickets),
        "gate_staff": len(staff),
        "boarding_preserved": restored,
    }


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
