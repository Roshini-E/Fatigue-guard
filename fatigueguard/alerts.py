"""Emergency alert dispatch - emails every emergency contact and records each attempt."""
import re
import uuid
from datetime import datetime, timedelta, timezone

from . import db
from .mailer import send_fatigue_alert


def display_name(driver: dict) -> str:
    return driver["username"] if re.fullmatch(r"[0-9a-fA-F]{32}", driver["name"]) else driver["name"]


def _notif(**kw):
    nid = str(uuid.uuid4())
    row = {"id": nid, "createdAt": db.now_iso(), "role": None, "contactName": None, **kw}
    db.run(
        "INSERT INTO Notification (id,requestId,driverId,sessionId,recipient,contactName,role,channel,trigger,status,detail,createdAt)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["requestId"], row["driverId"], row["sessionId"], row["recipient"], row["contactName"],
         row["role"], row["channel"], row["trigger"], row["status"], row["detail"], row["createdAt"]))
    return row


def dispatch_alert(driver: dict, session_id: str, trigger: str, risk_score: float) -> dict:
    trigger = str(trigger)[:160]
    risk_score = max(0.0, min(100.0, float(risk_score or 0)))
    request_id = str(uuid.uuid4())

    contacts = db.q("SELECT * FROM EmergencyContact WHERE driverId=? ORDER BY priority ASC, createdAt ASC", (driver["id"],))
    if not contacts:
        n = _notif(requestId=request_id, driverId=driver["id"], sessionId=session_id, recipient="—",
                   channel="EMAIL", trigger=trigger, status="failed",
                   detail="No emergency contacts configured. Add one in the Contacts tab to receive alerts.")
        return {"requestId": request_id, "notifications": [n]}

    veh = db.q1("SELECT * FROM AccountVehicle WHERE driverId=?", (driver["id"],))
    vehicle_label = veh["registrationNumber"] if veh else None

    # throttle: at most one alert batch per 30 seconds
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    recent = db.q1("SELECT requestId FROM Notification WHERE driverId=? AND createdAt>=? LIMIT 1", (driver["id"], cutoff))
    if recent:
        return {"requestId": recent["requestId"], "throttled": True}

    out = []
    for c in contacts:
        res = send_fatigue_alert(
            {"name": c["name"], "email": c["email"], "relationship": c["relationship"], "priority": c["priority"]},
            display_name(driver), vehicle_label, trigger, risk_score, session_id)
        out.append(_notif(requestId=request_id, driverId=driver["id"], sessionId=session_id,
                          recipient=c["email"], contactName=c["name"], role=c["priority"],
                          channel="EMAIL", trigger=trigger, status=res["status"], detail=res["detail"]))
    return {"requestId": request_id, "notifications": out}
