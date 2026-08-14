"""Push boarding outcomes from the local outbox up to the cloud API.

Every successful /verify scan writes a row here (see main.py) in addition to
marking the local ticket boarded, independent of `verification_tickets` (no
FK — a later /sync TRUNCATE on that table can't lose an unflushed event).
A background loop retries unsynced rows every OUTBOX_RETRY_SECONDS; the
laptop console's "Force Sync" button (POST /push_boarding) triggers an
immediate out-of-band attempt for when the pier's internet is spotty and
staff don't want to wait for the next automatic tick.
"""

import os

import httpx
import psycopg

CLOUD_API_URL = os.getenv("CLOUD_API_URL", "https://aleson-test-2.brylletan.com").rstrip("/")
LOCAL_DATABASE_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://aleson_local:aleson_local_gate@localhost:5433/aleson_db",
)
OUTBOX_RETRY_SECONDS = int(os.getenv("OUTBOX_RETRY_SECONDS", "30"))
OUTBOX_BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "500"))

CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS boarding_events (
        id SERIAL PRIMARY KEY,
        ticket_id INT NOT NULL,
        qr_token VARCHAR(36),
        boarded_at TIMESTAMP NOT NULL,
        boarded_by_fk INT,
        synced BOOLEAN NOT NULL DEFAULT false,
        attempts INT NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT now()
    );
"""


def ensure_table() -> None:
    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)


def record_boarding_event(ticket_id: int, qr_token: str, boarded_at, boarded_by_fk: int | None) -> None:
    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(
                """
                INSERT INTO boarding_events (ticket_id, qr_token, boarded_at, boarded_by_fk)
                VALUES (%s, %s, %s, %s)
                """,
                (ticket_id, qr_token, boarded_at, boarded_by_fk),
            )


def push_pending_events() -> dict:
    """Send unsynced rows to the cloud in one batch. Returns a summary dict;
    never raises — network/cloud failures just leave rows pending for the
    next attempt.
    """
    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(
                """
                SELECT id, ticket_id, boarded_at, boarded_by_fk
                FROM boarding_events
                WHERE synced = false
                ORDER BY id
                LIMIT %s
                """,
                (OUTBOX_BATCH_SIZE,),
            )
            pending = cur.fetchall()

    if not pending:
        return {"status": "ok", "pushed": 0, "pending": 0}

    ids = [row[0] for row in pending]
    events = [
        {
            "ticket_id": row[1],
            "boarded_at": row[2].isoformat(),
            "boarded_by_fk": row[3],
        }
        for row in pending
    ]

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{CLOUD_API_URL}/api/v1/verification/boarding-events",
                json={"events": events},
            )
            response.raise_for_status()
    except httpx.HTTPError as e:
        with psycopg.connect(LOCAL_DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE boarding_events SET attempts = attempts + 1, last_error = %s WHERE id = ANY(%s)",
                    (str(e), ids),
                )
        return {"status": "failed", "reason": str(e), "pushed": 0, "pending": len(ids)}

    with psycopg.connect(LOCAL_DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE boarding_events SET synced = true WHERE id = ANY(%s)", (ids,))
            cur.execute("SELECT count(*) FROM boarding_events WHERE synced = false")
            still_pending = cur.fetchone()[0]

    return {"status": "ok", "pushed": len(ids), "pending": still_pending}
