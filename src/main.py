"""Aleson boarding-gate verification server.

Runs on the gate laptop against the locally synced copy of the cloud
database. Tablets on the laptop's hotspot POST the scanned QR token to
/verify and show the passenger details returned here.

Run:  uvicorn src.main:app --host 0.0.0.0 --port 8001
"""

import os

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .sync import SyncError, sync_database

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


TICKET_LOOKUP_SQL = """
    SELECT passenger_first_name, passenger_last_name, seat_number, status,
           vessel, origin_port, destination_port, accommodation_class,
           scheduled_departure
    FROM verification_tickets
    WHERE qr_token = %s
"""


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


@app.post("/sync")
def sync():
    """Re-download the ticket data from the cloud API into the local Postgres."""
    try:
        return sync_database()
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
     scheduled_departure) = row

    if ticket_status in ("Cancelled", "Refunded"):
        return {"status": "failed", "reason": f"Ticket is {ticket_status.lower()}"}

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
    }
