"""Seed the database from scripts/fg_export.json (demo account: driver1 / demo1234)."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fatigueguard import db  # noqa: E402
from fatigueguard.auth import hash_password  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))


def parse_date(s):
    """The original desktop app wrote naive IST timestamps; store them as UTC ISO."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace(" ", "T")[:19]).replace(tzinfo=IST)
    except ValueError:
        return None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def seed(reset=True):
    db.init_db()
    data = json.load(open(os.path.join(HERE, "fg_export.json"), encoding="utf-8"))
    c = db.conn()
    if reset:
        for t in ["Notification", "WebSession", "FatigueEvent", "MonitoringSession", "EmergencyContact", "AccountVehicle", "Driver"]:
            c.execute(f"DELETE FROM {t}")
    now = db.now_iso()
    pw = hash_password("demo1234")
    for d in data["drivers"]:
        c.execute("INSERT OR REPLACE INTO Driver (id,username,name,email,passwordHash,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?)",
                  (d["driver_id"], d["name"], d["name"], None, pw, now, now))
    for v in data["accountVehicles"]:
        c.execute("INSERT OR REPLACE INTO AccountVehicle (id,driverId,registrationNumber,model,createdAt,updatedAt) VALUES (?,?,?,?,?,?)",
                  (v["vehicle_id"], v["driver_id"], v["registration_number"], v["vehicle_model"], now, now))
    for e in data["emergencyContacts"]:
        c.execute("INSERT OR REPLACE INTO EmergencyContact (id,driverId,name,email,phone,relationship,priority,emailKey,createdAt,updatedAt)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (e["contact_id"], e["driver_id"], e["contact_name"], e["contact_email"], None, e["relationship"],
                   e.get("priority") or "Primary", e["email_key"], now, now))
    ids = set()
    for s in data["sessions"]:
        start = parse_date(s["startTime"])
        if not start:
            continue
        ids.add(s["id"])
        c.execute("INSERT OR REPLACE INTO MonitoringSession (id,driverId,vehicleId,startTime,endTime,status,source,errorMessage,summaryJson,createdAt)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (s["id"], s["driverId"], s.get("vehicleId") or None, start, parse_date(s["endTime"]),
                   s["status"], s["source"], s["errorMessage"], s["summaryJson"], now))
    n = 0
    for e in data["events"]:
        t = parse_date(e["eventTime"])
        if not t or e["sessionId"] not in ids:
            continue
        c.execute("INSERT INTO FatigueEvent (sessionId,eventType,riskLevel,riskScore,eventTime,elapsedS,description) VALUES (?,?,?,?,?,?,?)",
                  (e["sessionId"], e["eventType"], e["riskLevel"], e.get("riskScore") or 0, t, e.get("elapsedS") or 0, e["description"]))
        n += 1
    c.commit()
    print(f"drivers: {len(data['drivers'])}, vehicles: {len(data['accountVehicles'])}, contacts: {len(data['emergencyContacts'])},"
          f" sessions: {len(ids)}, events: {n}")


if __name__ == "__main__":
    seed()
