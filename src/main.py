"""Aleson boarding-gate verification server.

Runs on the gate laptop against the locally synced copy of one upcoming
trip's tickets. The agent opens the web page served at `/` to pick the trip
and download it; tablets on the laptop's hotspot POST the scanned QR token to
/verify, which shows the passenger details and marks the ticket as boarded.

Run:  uvicorn src.main:app --host 0.0.0.0 --port 8001
"""

import os

import psycopg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .sync import SyncError, fetch_upcoming_trips, sync_database
from .web import INDEX_HTML

LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://aleson_local:aleson_local_gate@localhost:5433/aleson_db",
)

app = FastAPI(title="Aleson Verification Local Server")

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


TICKET_LOOKUP_SQL = """
    SELECT passenger_first_name, passenger_last_name, seat_number, status,
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
                        count(*) FILTER (WHERE boarding_status = 'Boarded') AS boarded,
                        min(vessel), min(origin_port), min(destination_port),
                        min(scheduled_departure)
                    FROM verification_tickets;
                """)
                total, boarded, vessel, origin, destination, departure = cur.fetchone()
    except Exception as e:
        return {"status": "error", "detail": str(e)}

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


@app.post("/sync")
def sync(req: SyncRequest):
    """Download one trip's tickets from the cloud API into the local Postgres."""
    if req.trip_id is None:
        return {"status": "failed", "reason": "trip_id is required"}
    try:
        return sync_database(req.trip_id)
    except SyncError as e:
        return {"status": "failed", "reason": str(e)}


@app.post("/verify")
def verify(req: VerifyRequest):
    token = req.qr_token.strip()
    if not token:
        return {"status": "failed", "reason": "Empty scan"}

    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(TICKET_LOOKUP_SQL, (token,))
            row = cur.fetchone()

            if row is None:
                return {"status": "failed", "reason": "Ticket not found"}

            (first_name, last_name, seat_number, ticket_status,
             vessel, origin_port, destination_port, accommodation_class,
             scheduled_departure, boarding_status, boarded_at) = row

            if ticket_status in ("Cancelled", "Refunded"):
                return {"status": "failed", "reason": f"Ticket is {ticket_status.lower()}"}

            already_boarded = boarding_status == "Boarded"
            if not already_boarded:
                cur.execute(MARK_BOARDED_SQL, (token,))
                boarded_at = cur.fetchone()[0]

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
