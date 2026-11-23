"""SQLite persistence (same tables as the original Prisma schema)."""
import os
import sqlite3
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, os.environ.get("DATABASE_PATH", "db/custom.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS Driver (
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL UNIQUE,
  email TEXT, passwordHash TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS AccountVehicle (
  id TEXT PRIMARY KEY, driverId TEXT NOT NULL UNIQUE REFERENCES Driver(id) ON DELETE CASCADE,
  registrationNumber TEXT NOT NULL, model TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS EmergencyContact (
  id TEXT PRIMARY KEY, driverId TEXT NOT NULL REFERENCES Driver(id) ON DELETE CASCADE,
  name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT, relationship TEXT,
  priority TEXT NOT NULL DEFAULT 'Primary', emailKey TEXT, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS MonitoringSession (
  id TEXT PRIMARY KEY, driverId TEXT NOT NULL REFERENCES Driver(id) ON DELETE CASCADE,
  vehicleId TEXT, startTime TEXT NOT NULL, endTime TEXT, status TEXT NOT NULL DEFAULT 'completed',
  source TEXT NOT NULL DEFAULT '0', errorMessage TEXT, summaryJson TEXT, createdAt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_session_driver ON MonitoringSession(driverId, startTime);
CREATE TABLE IF NOT EXISTS FatigueEvent (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sessionId TEXT NOT NULL REFERENCES MonitoringSession(id) ON DELETE CASCADE,
  eventType TEXT NOT NULL, riskLevel TEXT NOT NULL, riskScore REAL NOT NULL, eventTime TEXT NOT NULL,
  elapsedS REAL NOT NULL, description TEXT);
CREATE INDEX IF NOT EXISTS idx_event_session ON FatigueEvent(sessionId, eventTime);
CREATE TABLE IF NOT EXISTS Notification (
  id TEXT PRIMARY KEY, requestId TEXT NOT NULL, driverId TEXT REFERENCES Driver(id) ON DELETE SET NULL,
  sessionId TEXT, recipient TEXT NOT NULL, contactName TEXT, role TEXT, channel TEXT NOT NULL,
  trigger TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, createdAt TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_notif_driver ON Notification(driverId, createdAt);
CREATE TABLE IF NOT EXISTS WebSession (
  tokenHash TEXT PRIMARY KEY, driverId TEXT NOT NULL REFERENCES Driver(id) ON DELETE CASCADE,
  createdAt TEXT NOT NULL, expiresAt TEXT NOT NULL);
"""

_local = threading.local()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        _local.c = c
    return c


def init_db() -> None:
    conn().executescript(SCHEMA)
    conn().commit()


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur
