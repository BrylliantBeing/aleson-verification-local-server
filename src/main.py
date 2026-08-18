"""Aleson boarding-gate verification server.

Runs on the gate laptop against the locally synced copy of one upcoming
trip's tickets. The agent opens the web page served at `/` to pick the trip
and download it; tablets on the laptop's hotspot POST the scanned QR token to
/verify, which shows the passenger details and marks the ticket as boarded.

Run:  uvicorn src.main:app --host 0.0.0.0 --port 8001
"""

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime

import bcrypt
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .outbox import (
    OUTBOX_RETRY_SECONDS,
    ensure_table as ensure_outbox_table,
    push_pending_events,
    record_boarding_event,
    record_trip_departure,
)
from .sync import SyncError, current_trip_id, fetch_upcoming_trips, sync_database
from .web import INDEX_HTML

LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://aleson_local:aleson_local_gate@localhost:5433/aleson_db",
)


async def _boarding_event_retry_loop():
    """Retries the local outbox against the cloud every OUTBOX_RETRY_SECONDS.
    Runs the blocking push in a thread so it never stalls request handling.
    """
    while True:
        await asyncio.sleep(OUTBOX_RETRY_SECONDS)
        try:
            await asyncio.to_thread(push_pending_events)
        except Exception as e:
            print(f"[outbox] retry loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_boarding_event_retry_loop())
    yield
    task.cancel()


app = FastAPI(title="Aleson Verification Local Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class VerifyRequest(BaseModel):
    qr_token: str


class SyncRequest(BaseModel):
    trip_id: int | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


# Gate-staff sessions, in-memory only (single-process console, restarted
# fresh each boarding session). {token: {"staff_id", "name", "expires_at"}}.
# The same token doubles as the "pairing code" the operator types into the
# tablet app's settings, so it's kept short enough to enter by hand.
SESSIONS: dict[str, dict] = {}
SESSION_TTL_SECONDS = 12 * 3600
PAIRING_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _generate_pairing_code() -> str:
    return "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(8))


def require_session(authorization: str | None = Header(default=None)) -> dict:
    """Guards the actual boarding action (/verify) behind a logged-in gate
    staffer, so every scan is attributable. /sync and /trips stay open
    (same as before) since gate-staff accounts themselves only exist locally
    once a sync has pulled them down — gating sync too would be a
    chicken-and-egg lockout on a freshly provisioned laptop.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not logged in")
    token = authorization.split(" ", 1)[1].strip()
    session = SESSIONS.get(token)
    if not session or session["expires_at"] < time.time():
        SESSIONS.pop(token, None)
        raise HTTPException(status_code=401, detail="Session expired, log in again")
    return session


TICKET_LOOKUP_SQL = """
    SELECT id, passenger_first_name, passenger_last_name, seat_number, status,
           vessel, origin_port, destination_port, accommodation_class,
           scheduled_departure, boarding_status, boarded_at
    FROM verification_tickets
    WHERE qr_token = %s
"""

MARK_BOARDED_SQL = """
    UPDATE verification_tickets
    SET boarding_status = 'Boarded', boarded_at = now()
    WHERE qr_token = %s
    RETURNING boarded_at
"""


@app.get("/", response_class=HTMLResponse)
def index():
    """Laptop-facing page to pick an upcoming trip and download it."""
    return INDEX_HTML


@app.get("/health")
def health():
    try:
        with psycopg.connect(LOCAL_DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM verification_tickets;")
                tickets = cur.fetchone()[0]
        return {"status": "ok", "tickets": tickets}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/trips")
def trips():
    """Upcoming trips from the cloud, closest departure first (for the picker)."""
    try:
        return {"status": "success", "trips": fetch_upcoming_trips()}
    except SyncError as e:
        return {"status": "failed", "reason": str(e)}


@app.get("/trip_status")
def trip_status():
    """Boarding progress for the trip currently loaded in the local DB."""
    try:
        with psycopg.connect(LOCAL_DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        count(*) FILTER (
                            WHERE status NOT IN ('Cancelled', 'Refunded')
                        ) AS total,
                        count(*) FILTER (WHERE boarding_status = 'Boarded') AS boarded
                    FROM verification_tickets;
                """)
                total, boarded = cur.fetchone()
                # The local table holds a single trip, so one row describes it.
                # Take a representative row rather than aggregating per column
                # (independent min()s would mix ports across trips).
                cur.execute("""
                    SELECT vessel, origin_port, destination_port, scheduled_departure
                    FROM verification_tickets
                    WHERE status NOT IN ('Cancelled', 'Refunded')
                    LIMIT 1;
                """)
                meta = cur.fetchone()
    except Exception as e:
        return {"status": "error", "detail": str(e)}

    vessel, origin, destination, departure = meta if meta else (None, None, None, None)

    route = (
        f"{origin} → {destination}"
        if origin and destination
        else None
    )
    return {
        "status": "ok",
        "total": total or 0,
        "boarded": boarded or 0,
        "vessel": vessel,
        "route": route,
        "departure": departure.isoformat() if departure else None,
    }


def _mark_departed_on_manifest(staff: dict) -> dict:
    """Printing the manifest is the last thing that happens before a vessel
    casts off, so it is what sets the trip Departed in the cloud.

    Recorded locally first and pushed by the outbox, never inline: the pier is
    routinely offline, and the manifest must print regardless. The cloud
    endpoint only promotes a Scheduled/Boarding trip and never re-stamps
    actual_departure, so a reprint is harmless.
    """
    trip_id = current_trip_id()
    if trip_id is None:
        # A laptop that has not synced since this feature shipped has no trip id
        # to report against. The manifest still prints; departure stays manual.
        print("[manifest] no trip id in local_state — departure not reported")
        return {"reported": False, "reason": "no synced trip on this laptop"}

    departed_at = datetime.now()
    try:
        with psycopg.connect(LOCAL_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE local_state SET manifest_printed_at = "
                    "COALESCE(manifest_printed_at, %s) WHERE id = 1",
                    (departed_at,),
                )
        record_trip_departure(trip_id, departed_at, staff["staff_id"])
    except Exception as e:
        # Never let this stop the manifest — the paper list is the point.
        print(f"[manifest] could not queue departure for trip {trip_id}: {e}")
        return {"reported": False, "reason": str(e)}

    print(f"[manifest] trip {trip_id} marked Departed by {staff['name']} "
          f"(staff_id={staff['staff_id']}); queued for the cloud")
    return {"reported": True, "trip_id": trip_id, "departed_at": departed_at.isoformat()}


@app.get("/manifest")
def manifest(staff: dict = Depends(require_session), mark_departed: bool = True):
    """Full passenger list for the trip currently loaded in the local DB, for
    the gate laptop to export/print before departure. Guarded like /verify
    since it's the same kind of boarding-relevant record.

    Exporting also reports the trip as Departed to the cloud (see
    _mark_departed_on_manifest) — the operator no longer has to remember to flip
    the status by hand in the admin dashboard afterwards. Pass
    `?mark_departed=false` to take a copy of the list without doing that.
    """
    with psycopg.connect(LOCAL_DATABASE_URL, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT passenger_first_name, passenger_last_name, seat_number,
                       accommodation_class, boarding_status, boarded_at,
                       vessel, origin_port, destination_port, scheduled_departure,
                       passenger_nationality
                FROM verification_tickets
                WHERE status NOT IN ('Cancelled', 'Refunded')
                ORDER BY seat_number;
            """)
            rows = cur.fetchall()

    print(f"[manifest] exported by {staff['name']} (staff_id={staff['staff_id']}), {len(rows)} passengers")

    departure_report = (
        _mark_departed_on_manifest(staff)
        if mark_departed
        else {"reported": False, "reason": "mark_departed=false"}
    )

    vessel, origin, destination, departure = (None, None, None, None)
    if rows:
        vessel, origin, destination, departure = rows[0][6], rows[0][7], rows[0][8], rows[0][9]

    return {
        "status": "ok",
        "vessel": vessel,
        "route": f"{origin} → {destination}" if origin and destination else None,
        "departure": departure.isoformat() if departure else None,
        "exported_by": staff["name"],
        "departure_reported": departure_report,
        "passengers": [
            {
                "passenger_name": f"{r[0]} {r[1]}",
                "seat_number": r[2],
                "accommodation_class": r[3],
                "boarding_status": r[4],
                "boarded_at": r[5].isoformat() if r[5] else None,
                "nationality": r[10],
            }
            for r in rows
        ],
    }


@app.post("/login")
def login(req: LoginRequest):
    """Authenticate a gate staffer against the locally synced roster (works
    offline — see sync.fetch_gate_staff) and issue a pairing-code session.
    """
    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, password_hash FROM gate_staff WHERE lower(email) = lower(%s)",
                (req.email.strip(),),
            )
            row = cur.fetchone()

    if row is None or not bcrypt.checkpw(req.password.encode(), row[2].encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    staff_id, name, _hash = row
    token = _generate_pairing_code()
    while token in SESSIONS:
        token = _generate_pairing_code()
    SESSIONS[token] = {
        "staff_id": staff_id,
        "name": name,
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    }
    return {"token": token, "staff_id": staff_id, "name": name}


@app.post("/sync")
def sync(req: SyncRequest):
    """Download one trip's tickets from the cloud API into the local Postgres."""
    if req.trip_id is None:
        return {"status": "failed", "reason": "trip_id is required"}
    try:
        return sync_database(req.trip_id)
    except SyncError as e:
        return {"status": "failed", "reason": str(e)}


@app.post("/push_boarding")
def push_boarding():
    """Manual 'Force Sync' — attempt to flush the boarding-events outbox to
    the cloud right now, instead of waiting for the next automatic retry.
    """
    return push_pending_events()


@app.get("/outbox_status")
def outbox_status():
    """How much still has to reach the cloud: boarded scans, and any departure
    reported by printing the manifest."""
    try:
        ensure_outbox_table()
        with psycopg.connect(LOCAL_DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM boarding_events WHERE synced = false")
                pending = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM trip_departure_events WHERE synced = false")
                departures_pending = cur.fetchone()[0]
        return {
            "status": "ok",
            "pending": pending,
            "departures_pending": departures_pending,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/verify")
def verify(req: VerifyRequest, staff: dict = Depends(require_session)):
    token = req.qr_token.strip()
    if not token:
        return {"status": "failed", "reason": "Empty scan"}

    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(TICKET_LOOKUP_SQL, (token,))
            row = cur.fetchone()

            if row is None:
                return {"status": "failed", "reason": "Ticket not found"}

            (ticket_id, first_name, last_name, seat_number, ticket_status,
             vessel, origin_port, destination_port, accommodation_class,
             scheduled_departure, boarding_status, boarded_at) = row

            if ticket_status in ("Cancelled", "Refunded"):
                return {"status": "failed", "reason": f"Ticket is {ticket_status.lower()}"}

            already_boarded = boarding_status == "Boarded"
            if not already_boarded:
                cur.execute(MARK_BOARDED_SQL, (token,))
                boarded_at = cur.fetchone()[0]
                print(f"[verify] {first_name} {last_name} boarded by {staff['name']} (staff_id={staff['staff_id']})")
                record_boarding_event(ticket_id, token, boarded_at, staff["staff_id"])

    route = (
        f"{origin_port} → {destination_port}"
        if origin_port and destination_port
        else "Unscheduled trip"
    )

    return {
        "status": "success",
        "passenger_name": f"{first_name} {last_name}",
        "route": route,
        "vessel": vessel,
        "seat_number": seat_number,
        "accommodation_class": accommodation_class,
        "departure": scheduled_departure.isoformat() if scheduled_departure else None,
        "ticket_status": ticket_status,
        "already_boarded": already_boarded,
        "boarded_at": boarded_at.isoformat() if boarded_at else None,
    }
