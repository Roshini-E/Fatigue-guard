"""scrypt password hashing + DB-backed cookie sessions (same scheme as the original)."""
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import jsonify, request

from . import db

SESSION_COOKIE = "fg_session"
SESSION_TTL = 7 * 24 * 60 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)
    return f"{salt}:{h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":")
        cand = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(cand, bytes.fromhex(h))
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(driver_id: str) -> str:
    token = secrets.token_hex(32)
    exp = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.run("INSERT INTO WebSession (tokenHash, driverId, createdAt, expiresAt) VALUES (?,?,?,?)",
           (_hash_token(token), driver_id, db.now_iso(), exp))
    return token


def current_driver():
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.q1("SELECT * FROM WebSession WHERE tokenHash=?", (_hash_token(token),))
    if not row:
        return None
    if row["expiresAt"] < db.now_iso():
        db.run("DELETE FROM WebSession WHERE tokenHash=?", (row["tokenHash"],))
        return None
    return db.q1("SELECT * FROM Driver WHERE id=?", (row["driverId"],))


def destroy_session():
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.run("DELETE FROM WebSession WHERE tokenHash=?", (_hash_token(token),))


def json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def new_id() -> str:
    return str(uuid.uuid4())
