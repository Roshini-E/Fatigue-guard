"""FatigueGuard - driver drowsiness monitoring (Flask + OpenCV + MediaPipe).

Run:  python app.py      ->  http://localhost:3000
"""
import json
import os
import re
import time
import uuid

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from flask import Flask, Response, jsonify, render_template, request  # noqa: E402

from fatigueguard import auth, db, mailer, monitor  # noqa: E402
from fatigueguard.auth import json_error  # noqa: E402

app = Flask(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def init():
    db.init_db()
    if db.q1("SELECT COUNT(*) AS n FROM Driver")["n"] == 0:
        from scripts.seed import seed  # first run: demo data (driver1 / demo1234)
        seed(reset=False)


def need_driver():
    d = auth.current_driver()
    return d, (None if d else json_error("Not authenticated", 401))


def driver_public(d):
    return {"id": d["id"], "username": d["username"], "name": d["name"], "email": d["email"]}


def set_cookie(resp, token):
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL, httponly=True, samesite="Lax", path="/")
    return resp


@app.get("/")
def home():
    return render_template("index.html")


# ---------------------------------------------------------------- auth
@app.post("/api/auth/login")
def login():
    body = request.get_json(silent=True)
    if not body:
        return json_error("Invalid request body")
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    if not username or not password:
        return json_error("Username and password are required")
    d = db.q1("SELECT * FROM Driver WHERE name=? OR email=? LIMIT 1", (username, username))
    if not d or not auth.verify_password(password, d["passwordHash"]):
        return json_error("Invalid username or password", 401)
    return set_cookie(jsonify({"driver": driver_public(d)}), auth.create_session(d["id"]))


@app.post("/api/auth/register")
def register():
    body = request.get_json(silent=True)
    if not body:
        return json_error("Invalid request body")
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    email = str(body["email"]).strip() if body.get("email") else None
    if len(username) < 3:
        return json_error("Username must be at least 3 characters")
    if len(password) < 6:
        return json_error("Password must be at least 6 characters")
    if email and not EMAIL_RE.match(email):
        return json_error("Invalid email address")
    if db.q1("SELECT id FROM Driver WHERE name=? OR (email IS NOT NULL AND email=?)", (username, email or "")):
        return json_error("Username or email already registered", 409)
    did, now = str(uuid.uuid4()), db.now_iso()
    db.run("INSERT INTO Driver (id,username,name,email,passwordHash,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?)",
           (did, username, username, email, auth.hash_password(password), now, now))
    reg = str(body.get("registrationNumber") or "").strip()
    if reg:
        db.run("INSERT INTO AccountVehicle (id,driverId,registrationNumber,model,createdAt,updatedAt) VALUES (?,?,?,?,?,?)",
               (str(uuid.uuid4()), did, reg.upper(), str(body.get("vehicleModel") or "car").strip() or "car", now, now))
    return set_cookie(jsonify({"driver": {"id": did, "username": username, "email": email}}), auth.create_session(did))


@app.post("/api/auth/logout")
def logout():
    d = auth.current_driver()
    if d:
        s = monitor.get(d["id"])
        if s:
            s.stop()
    auth.destroy_session()
    resp = jsonify({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def me():
    d = auth.current_driver()
    if not d:
        return jsonify({"driver": None})
    v = db.q1("SELECT * FROM AccountVehicle WHERE driverId=?", (d["id"],))
    return jsonify({"driver": driver_public(d),
                    "vehicle": {"registrationNumber": v["registrationNumber"], "model": v["model"]} if v else None})


# ---------------------------------------------------------------- contacts
@app.get("/api/contacts/list")
def contacts_list():
    d, err = need_driver()
    if err:
        return err
    return jsonify({"contacts": db.q("SELECT * FROM EmergencyContact WHERE driverId=? ORDER BY priority ASC, createdAt ASC", (d["id"],))})


@app.post("/api/contacts/add")
def contacts_add():
    d, err = need_driver()
    if err:
        return err
    body = request.get_json(silent=True)
    if not body:
        return json_error("Invalid request body")
    name, email = str(body.get("name") or "").strip(), str(body.get("email") or "").strip()
    phone = str(body["phone"]).strip() if body.get("phone") else None
    rel = str(body["relationship"]).strip() if body.get("relationship") else None
    priority = "Secondary" if body.get("priority") == "Secondary" else "Primary"
    if not name:
        return json_error("Contact name is required")
    if not EMAIL_RE.match(email):
        return json_error("A valid email is required")
    if db.q1("SELECT COUNT(*) AS n FROM EmergencyContact WHERE driverId=?", (d["id"],))["n"] >= 10:
        return json_error("Maximum 10 emergency contacts reached")
    cid, now = str(uuid.uuid4()), db.now_iso()
    db.run("INSERT INTO EmergencyContact (id,driverId,name,email,phone,relationship,priority,createdAt,updatedAt) VALUES (?,?,?,?,?,?,?,?,?)",
           (cid, d["id"], name, email, phone, rel, priority, now, now))
    return jsonify({"contact": db.q1("SELECT * FROM EmergencyContact WHERE id=?", (cid,))})


@app.post("/api/contacts/update")
def contacts_update():
    d, err = need_driver()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    cid = str(body.get("id") or "")
    name, email = str(body.get("name") or "").strip(), str(body.get("email") or "").strip()
    if not cid or not name or not EMAIL_RE.match(email):
        return json_error("Contact id, name, and a valid email are required")
    contact = db.q1("SELECT id FROM EmergencyContact WHERE id=? AND driverId=?", (cid, d["id"]))
    if not contact:
        return json_error("Contact not found", 404)
    priority = "Secondary" if body.get("priority") == "Secondary" else "Primary"
    db.run("UPDATE EmergencyContact SET name=?,email=?,phone=?,relationship=?,priority=?,updatedAt=? WHERE id=? AND driverId=?",
           (name, email, str(body.get("phone") or "").strip() or None,
            str(body.get("relationship") or "").strip() or None, priority, db.now_iso(), cid, d["id"]))
    return jsonify({"contact": db.q1("SELECT * FROM EmergencyContact WHERE id=? AND driverId=?", (cid, d["id"]))})


@app.post("/api/contacts/delete")
def contacts_delete():
    d, err = need_driver()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    cid = str(body["id"]) if body.get("id") else None
    if not cid:
        return json_error("Contact id is required")
    c = db.q1("SELECT driverId FROM EmergencyContact WHERE id=?", (cid,))
    if not c or c["driverId"] != d["id"]:
        return json_error("Contact not found", 404)
    db.run("DELETE FROM EmergencyContact WHERE id=?", (cid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- sessions / reports / alert log
def _summary(raw):
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


@app.get("/api/sessions/list")
def sessions_list():
    d, err = need_driver()
    if err:
        return err
    rows = db.q("SELECT s.*, (SELECT COUNT(*) FROM FatigueEvent e WHERE e.sessionId=s.id) AS eventCount"
                " FROM MonitoringSession s WHERE driverId=? ORDER BY startTime DESC LIMIT 50", (d["id"],))
    return jsonify({"sessions": [{
        "id": s["id"], "startTime": s["startTime"], "endTime": s["endTime"], "status": s["status"],
        "source": s["source"], "errorMessage": s["errorMessage"], "eventCount": s["eventCount"],
        "summary": _summary(s["summaryJson"])} for s in rows]})


@app.get("/api/sessions/detail")
def sessions_detail():
    d, err = need_driver()
    if err:
        return err
    sid = request.args.get("id")
    if not sid:
        return json_error("Session id is required")
    s = db.q1("SELECT * FROM MonitoringSession WHERE id=?", (sid,))
    if not s or s["driverId"] != d["id"]:
        return json_error("Session not found", 404)
    events = db.q("SELECT eventType,riskLevel,riskScore,eventTime,elapsedS,description FROM FatigueEvent"
                  " WHERE sessionId=? ORDER BY eventTime ASC, id ASC", (sid,))
    return jsonify({"session": {"id": s["id"], "startTime": s["startTime"], "endTime": s["endTime"],
                                "status": s["status"], "errorMessage": s["errorMessage"],
                                "summary": _summary(s["summaryJson"]), "driverName": d["name"]},
                    "events": events})


@app.get("/api/notifications")
def notifications():
    d, err = need_driver()
    if err:
        return err
    return jsonify({"notifications": db.q("SELECT * FROM Notification WHERE driverId=? ORDER BY createdAt DESC LIMIT 50", (d["id"],))})


@app.get("/api/email/status")
def email_status():
    d, err = need_driver()
    if err:
        return err
    return jsonify(mailer.configuration_status())


@app.post("/api/email/test")
def email_test():
    d, err = need_driver()
    if err:
        return err
    recipient = str(d.get("email") or "").strip()
    if not recipient or not EMAIL_RE.match(recipient):
        return json_error("Add a valid email address to your driver profile before sending a test email")
    result = mailer.send_test_email(recipient)
    return (jsonify(result), 200 if result["status"] == "delivered" else 400)


# ---------------------------------------------------------------- live monitoring (camera / OpenCV / MediaPipe)
@app.post("/api/monitor/start")
def monitor_start():
    d, err = need_driver()
    if err:
        return err
    mode = (request.get_json(silent=True) or {}).get("mode")
    s = monitor.for_driver(d)
    s.start("simulated" if mode == "simulated" else "camera")
    return jsonify(s.state())


@app.post("/api/monitor/stop")
def monitor_stop():
    d, err = need_driver()
    if err:
        return err
    s = monitor.for_driver(d)
    s.stop()
    return jsonify({"ok": True})


@app.get("/api/monitor/state")
def monitor_state():
    d, err = need_driver()
    if err:
        return err
    state = monitor.for_driver(d).state()
    state["backendResponseAt"] = db.now_iso()
    return jsonify(state)


@app.post("/api/monitor/mute")
def monitor_mute():
    d, err = need_driver()
    if err:
        return err
    monitor.for_driver(d).set_muted(bool((request.get_json(silent=True) or {}).get("muted")))
    return jsonify({"ok": True})


@app.post("/api/monitor/stop-siren")
def monitor_stop_siren():
    d, err = need_driver()
    if err:
        return err
    monitor.for_driver(d).stop_siren()
    return jsonify({"ok": True})


@app.get("/video_feed")
def video_feed():
    d, err = need_driver()
    if err:
        return err
    s = monitor.for_driver(d)

    def gen():
        last = None
        while s.phase in ("starting", "live") and s.mode == "camera":
            frame = s.jpeg()
            if frame is not None and frame is not last:
                last = frame
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            time.sleep(0.03)

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


init()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), threaded=True, debug=False)
