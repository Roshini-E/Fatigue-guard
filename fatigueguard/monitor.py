"""Live monitoring session manager (one per logged-in driver).

Owns the engine (camera or simulator), buffers/persists events to the database every
3 s, dispatches the emergency e-mail in a background thread the instant drowsiness is
detected, and exposes the live state that the dashboard polls.
"""
import json
import os
import random
import string
import threading
import time
from datetime import datetime

from . import db
from .alerts import dispatch_alert
from .engine import FatigueEngine, empty_metrics
from .simulator import FatigueSimulator

VALID_TYPES = {"MICROSLEEP", "HEAD_NOD", "YAWN", "DISTRACTION", "LEVEL", "CALIBRATED", "ABSENCE", "SLOW_BLINK"}

_sessions = {}
_sessions_lock = threading.Lock()


def get(driver_id):
    return _sessions.get(driver_id)


class MonitorSession:
    def __init__(self, driver: dict):
        self.driver = driver
        self.lock = threading.RLock()
        self.phase = "idle"            # idle | starting | live
        self.mode = None               # camera | simulated
        self.error = ""
        self.session_id = ""
        self.engine = None
        self.metrics = empty_metrics()
        self.events = []               # newest first, max 120
        self.event_seq = 0
        self.buffer = []
        self.muted = False
        self.siren_active = False
        self.alarm_active = False
        self._alarm_recovery_since = None
        self._alarm_recovery_s = max(0.0, float(os.environ.get("ALARM_RECOVERY_S", "0.35")))
        self.state_seq = 0
        self.metrics_updated_at = None
        self.alert_info = None
        self.alert_seq = 0
        self.last_alert_at = 0.0
        self._bg = None
        self._bg_stop = threading.Event()
        self._start_thread = None
        self._start_generation = 0

    # ------------------------------------------------------------ start / stop
    def _make_session_id(self):
        d = datetime.now()
        return f"{d:%Y%m%d-%H%M%S}-{self.driver['username']}-web"

    def start(self, mode: str):
        with self.lock:
            if self.phase != "idle":
                return
            self.phase, self.mode, self.error = "starting", mode, ""
            self._start_generation += 1
            generation = self._start_generation
            sid = self._make_session_id()
            self.session_id = sid
            self._reset_live_state()
            self._start_thread = threading.Thread(
                target=self._start_worker, args=(mode, sid, generation),
                name=f"fg-start-{self.driver['id'][:8]}", daemon=True)
            self._start_thread.start()

    def _start_worker(self, mode, sid, generation):
        # The caller reserves the session id before starting this worker so stop or
        # logout can invalidate startup while camera/model initialization is running.
        with self.lock:
            if generation != self._start_generation or self.phase != "starting":
                return
            sid = self.session_id
        eng = None
        try:
            db.run("UPDATE MonitoringSession SET status='completed', endTime=? WHERE driverId=? AND status='active'",
                   (db.now_iso(), self.driver["id"]))
            veh = db.q1("SELECT id FROM AccountVehicle WHERE driverId=?", (self.driver["id"],))
            db.run("INSERT OR REPLACE INTO MonitoringSession (id,driverId,vehicleId,startTime,status,source,createdAt) VALUES (?,?,?,?,?,?,?)",
                   (sid, self.driver["id"], veh["id"] if veh else None, db.now_iso(), "active", "1", db.now_iso()))
            with self.lock:
                cancelled_before_engine = generation != self._start_generation or self.phase != "starting"
            if cancelled_before_engine:
                db.run("UPDATE MonitoringSession SET status='completed',endTime=? WHERE id=?", (db.now_iso(), sid))
                return
            if mode == "camera":
                eng = FatigueEngine(self._on_metrics, self._on_event, int(os.environ.get("CAMERA_INDEX", 0)))
            else:
                eng = FatigueSimulator(self._on_metrics, self._on_event)
            eng.start()
            bg = threading.Thread(target=self._background, daemon=True)
            self._bg_stop.clear()
            with self.lock:
                if generation != self._start_generation or self.phase != "starting":
                    cancelled = True
                else:
                    cancelled = False
                    self.engine = eng
                    self.phase = "live"
                    self._bg = bg
                    bg.start()
            if cancelled:
                eng.stop()
                db.run("UPDATE MonitoringSession SET status='completed',endTime=? WHERE id=?", (db.now_iso(), sid))
                return
        except Exception as exc:
            msg = str(exc) or "Could not start the camera."
            if eng:
                eng.stop()
            try:
                db.run("UPDATE MonitoringSession SET status='failed', endTime=?, errorMessage=? WHERE id=?",
                       (db.now_iso(), msg[:300], sid))
            except Exception:
                pass
            with self.lock:
                if generation == self._start_generation:
                    self.engine, self.session_id = None, ""
                    self.phase, self.mode, self.error = "idle", None, (
                        msg if mode == "camera" else "Could not start the simulation.")
            return

    def _reset_live_state(self):
        self.metrics = empty_metrics()
        self.events, self.buffer = [], []
        self.alert_info, self.siren_active, self.alarm_active = None, False, False
        self._alarm_recovery_since = None
        self.state_seq = 0
        self.metrics_updated_at = None
        self.last_alert_at = 0.0

    def _background(self):
        n = 0
        while not self._bg_stop.wait(1.0):
            n += 1
            eng = self.engine
            if eng:
                eng.tick_level_seconds(1.0)
            if n % 3 == 0:
                self._flush()

    def _flush(self):
        with self.lock:
            batch, self.buffer = self.buffer, []
            sid = self.session_id
        if not batch or not sid:
            return
        rows = []
        for e in batch:
            if e["eventType"] not in VALID_TYPES:
                continue
            rows.append((sid, e["eventType"], e.get("riskLevel") or "ALERT",
                         max(0.0, min(100.0, float(e.get("riskScore") or 0))), e["eventTime"],
                         float(e.get("elapsedS") or 0), (e.get("description") or "")[:200] or None))
        try:
            c = db.conn()
            c.executemany("INSERT INTO FatigueEvent (sessionId,eventType,riskLevel,riskScore,eventTime,elapsedS,description)"
                          " VALUES (?,?,?,?,?,?,?)", rows)
            c.commit()
        except Exception as exc:
            print("event flush failed:", exc)

    def stop(self):
        with self.lock:
            if self.phase == "idle":
                return
            if self.phase == "starting":
                self._start_generation += 1
                sid = self.session_id
                self.session_id = ""
                self.phase, self.mode = "idle", None
                self.error = "Monitoring start cancelled."
                eng = None
                starting = True
            else:
                starting = False
                eng, sid = self.engine, self.session_id
        if starting:
            if sid:
                db.run("UPDATE MonitoringSession SET status='completed',endTime=? WHERE id=? AND status='active'",
                       (db.now_iso(), sid))
            return
        self._bg_stop.set()
        if self._bg:
            self._bg.join(timeout=3)
        summary = {}
        if eng:
            summary = eng.summary
            eng.stop()
        if sid:
            self._flush()
            row = db.q1("SELECT startTime FROM MonitoringSession WHERE id=?", (sid,))
            enriched = {**summary, "session_id": sid, "driver_id": self.driver["id"],
                        "started_at": row["startTime"] if row else None, "ended_at": db.now_iso(), "schema": "web-1"}
            db.run("UPDATE MonitoringSession SET status='completed', endTime=?, summaryJson=?, errorMessage=NULL WHERE id=?",
                   (db.now_iso(), json.dumps(enriched), sid))
        with self.lock:
            self.engine, self.session_id = None, ""
            self.metrics = empty_metrics()
            self.siren_active, self.alarm_active, self.alert_info = False, False, None
            self._alarm_recovery_since = None
            self.phase, self.mode = "idle", None

    # ------------------------------------------------------------ engine callbacks
    def _on_metrics(self, m):
        with self.lock:
            self.metrics = dict(m)
            self.state_seq += 1
            self.metrics_updated_at = db.now_iso()
            if m.get("level") == "CRITICAL":
                self.alarm_active = True
                self._alarm_recovery_since = None
                self.siren_active = self.alarm_active and not self.muted
            elif self.alarm_active:
                now = time.monotonic()
                if self._alarm_recovery_since is None:
                    self._alarm_recovery_since = now
                elif now - self._alarm_recovery_since >= self._alarm_recovery_s:
                    self.alarm_active = False
                    self._alarm_recovery_since = None
            if not self.alarm_active:
                self._alarm_recovery_since = None
            self.siren_active = self.alarm_active and not self.muted

    def _on_event(self, e):
        with self.lock:
            self.event_seq += 1
            self.events.insert(0, {**e, "key": f"{e['eventTime']}-{e['eventType']}-{self.event_seq}"})
            del self.events[120:]
            if self.session_id:
                self.buffer.append(e)
            drowsy = e["eventType"] in ("MICROSLEEP", "HEAD_NOD") or (e["eventType"] == "LEVEL" and e["riskLevel"] == "CRITICAL")
            sid = self.session_id
        if drowsy and sid:
            self._handle_drowsiness(e, sid)

    def _handle_drowsiness(self, e, sid):
        with self.lock:
            now = time.time()
            if now - self.last_alert_at < 20:
                return
            self.last_alert_at = now
            trigger = f"{e['eventType']}: {e['description']}"
            self.alert_info = {"trigger": trigger, "notified": []}
            self.alert_seq += 1
            seq = self.alert_seq
        # e-mail dispatch runs in the background at the same moment as the siren
        threading.Thread(target=self._dispatch, args=(trigger, e["riskScore"], sid, seq), daemon=True).start()

    def _dispatch(self, trigger, risk, sid, seq):
        data = {}
        try:
            data = dispatch_alert(self.driver, sid, trigger, risk)
            rows = data.get("notifications", [])
        except Exception as exc:
            print("alert dispatch failed:", exc)
            rows = []
        with self.lock:
            self.alert_info = {"trigger": trigger, "notified": [
                {"contactName": r.get("contactName"), "recipient": r["recipient"], "status": r["status"], "detail": r["detail"]}
                for r in rows], "done": True, "throttled": bool(data.get("throttled")) if rows == [] else False}
            self.alert_seq = max(self.alert_seq, seq)

    # ------------------------------------------------------------ controls
    def set_muted(self, muted: bool):
        with self.lock:
            self.muted = bool(muted)
            self.siren_active = self.alarm_active and not self.muted

    def stop_siren(self):
        with self.lock:
            self.muted = True
            self.siren_active = False

    def state(self):
        with self.lock:
            return {
                "phase": self.phase, "mode": self.mode, "error": self.error,
                "metrics": dict(self.metrics), "events": list(self.events), "muted": self.muted,
                "sirenActive": self.siren_active, "alarmActive": self.alarm_active,
                "alertInfo": self.alert_info, "alertSeq": self.alert_seq, "sessionId": self.session_id,
                "stateSeq": self.state_seq, "metricsUpdatedAt": self.metrics_updated_at,
            }

    def jpeg(self):
        eng = self.engine
        return eng.get_jpeg() if eng else None


def for_driver(driver: dict) -> MonitorSession:
    with _sessions_lock:
        s = _sessions.get(driver["id"])
        if s is None:
            s = _sessions[driver["id"]] = MonitorSession(driver)
        return s
