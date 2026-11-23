"""FatigueGuard detection engine - OpenCV + MediaPipe FaceMesh.

Faithful Python port of the original browser engine (src/lib/fatigue/engine.ts):
EAR + per-driver calibration, blink / slow-blink / microsleep detection, PERCLOS
over a rolling 60 s window, yawns (MAR), head pitch/yaw posture, head nods,
distractions, absences and the 4-level risk model.
"""
import math
import os
import threading
import time
from datetime import datetime, timezone

import cv2

LEVELS = ["ALERT", "CAUTION", "WARNING", "CRITICAL"]
LEVEL_SEVERITY = {"ALERT": 0, "CAUTION": 1, "WARNING": 2, "CRITICAL": 3}

# MediaPipe FaceMesh landmark indices (same as the original)
L_EYE = [33, 160, 158, 133, 153, 144]
R_EYE = [362, 385, 387, 263, 373, 380]
NOSE_TIP, CHIN, FOREHEAD = 1, 152, 10
L_EYE_OUTER, R_EYE_OUTER = 33, 263
CHEEK_L, CHEEK_R = 234, 454          # face width reference for yaw
MOUTH_TOP, MOUTH_BOTTOM, MOUTH_L, MOUTH_R = 13, 14, 61, 291
MOUTH_CONTOUR = [61, 291, 13, 14]

HUD_BGR = (36, 191, 251)             # #fbbf24 amber, same HUD colour as the web UI


def empty_metrics():
    return {
        "risk": 0.0, "level": "ALERT", "perclos": 0.0, "ear": 0.0, "baselineEar": 0.0,
        "thresholdEar": 0.0, "eyesClosedS": 0.0, "blinks": 0, "slowBlinks": 0,
        "microsleeps": 0, "yawns": 0, "headNods": 0, "distractions": 0, "absences": 0,
        "headPitchDeg": 0.0, "headYawDeg": 0.0, "monitoredS": 0.0, "faceDetected": False,
        "mar": 0.0, "marThreshold": 0.0, "yawnStatus": "CALIBRATING", "lastYawnAt": None,
        "captureAt": None, "processedAt": None, "processingMs": 0.0, "frameSeq": 0,
    }


def level_for_risk(risk: float) -> str:
    if risk >= 78:
        return "CRITICAL"
    if risk >= 52:
        return "WARNING"
    if risk >= 28:
        return "CAUTION"
    return "ALERT"


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _ear_of(pts, idx):
    p = [pts[i] for i in idx]
    vertical = (_dist(p[1], p[5]) + _dist(p[2], p[4])) / 2
    horizontal = _dist(p[0], p[3])
    return 0.0 if horizontal == 0 else vertical / horizontal


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class FatigueEngine:
    """Reads the webcam with OpenCV, runs MediaPipe FaceMesh, computes all metrics."""

    def __init__(self, on_metrics, on_event, camera_index=0):
        self.on_metrics = on_metrics
        self.on_event = on_event
        self.camera_index = camera_index

        self._cap = None
        self._mesh = None
        self._thread = None
        self._capture_thread = None
        self._running = False
        self._lock = threading.Lock()
        self._jpeg = None
        self._frame = None
        self._frame_seq = 0
        self._frame_capture_at = None
        self._processed_frame_seq = 0

        # calibration
        self._ear_samples, self._pitch_samples = [], []
        self._mar_samples = []
        self._calibrated = False
        self._baseline_ear = self._threshold_ear = self._baseline_pitch = 0.0
        self._baseline_mar = 0.0

        # blink state
        self._eyes_closed = False
        self._closed_at = 0.0
        self._eyes_closed_s = 0.0

        # nod / yawn / distraction state
        self._nod_start = self._yawn_start = self._distract_start = None
        self._yawn_latched = False
        self._yawn_release_start = None
        self._last_yawn_at = None
        self._yawn_min_duration = max(0.5, float(os.environ.get("YAWN_MIN_DURATION_S", "1.0")))
        self._yawn_release_duration = max(0.2, float(os.environ.get("YAWN_RELEASE_DURATION_S", "0.45")))
        self._last_nod = self._last_yawn = self._last_distract = self._last_absence = 0.0
        self._no_face_since = None

        self._closure_window = []      # (t, closed)
        self._recent = []              # (weight, t)
        self._risk_ema = 0.0
        self._level = "ALERT"
        self._max_level = "ALERT"

        self._started_at = 0.0
        self._last_tick = 0.0
        self.m = empty_metrics()
        self._max_risk = self._risk_sum = self._risk_count = 0.0
        self._max_perclos = self._max_closure = 0.0
        self._level_seconds = {k: 0.0 for k in LEVELS}

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        import platform
        import mediapipe as mp

        if not hasattr(mp, "solutions"):
            raise RuntimeError(
                "Installed MediaPipe has no 'solutions' API. Run: pip install mediapipe==0.10.14"
            )
        backend = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.camera_index, backend)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(
                "Could not open the camera. Close Zoom/Teams (or any other app using the camera) and try again."
            )
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(os.environ.get("CAMERA_WIDTH", "640")))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(os.environ.get("CAMERA_HEIGHT", "480")))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        self._mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=False,
            min_detection_confidence=0.5, min_tracking_confidence=0.5,
        )
        self._running = True
        self._started_at = self._last_tick = time.perf_counter()
        self._capture_thread = threading.Thread(target=self._capture_loop, name="fg-camera-capture", daemon=True)
        self._capture_thread.start()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        # Releasing the device also unblocks a pending VideoCapture.read on some backends.
        if self._cap:
            self._cap.release()
        if self._capture_thread and self._capture_thread is not threading.current_thread():
            self._capture_thread.join(timeout=2)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        try:
            if self._mesh:
                self._mesh.close()
        except Exception:
            pass
        self._mesh = self._cap = None

    def get_jpeg(self):
        with self._lock:
            return self._jpeg

    # ------------------------------------------------------------------ main loop
    def _capture_loop(self):
        while self._running:
            try:
                ok, frame = self._cap.read()
            except cv2.error:
                if self._running:
                    time.sleep(0.02)
                continue
            captured = time.perf_counter()
            if not ok:
                time.sleep(0.02)
                continue
            with self._lock:
                self._frame = frame
                self._frame_seq += 1
                self._frame_capture_at = captured

    def _loop(self):
        period = 1 / max(8, min(30, int(os.environ.get("PROCESSING_FPS", "20"))))
        while self._running:
            t0 = time.perf_counter()
            with self._lock:
                if self._frame is None or self._frame_seq == self._processed_frame_seq:
                    frame = None
                else:
                    frame = self._frame.copy()
                    seq, captured = self._frame_seq, self._frame_capture_at
                    self._processed_frame_seq = seq
            if frame is None:
                time.sleep(0.005)
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            res = self._mesh.process(rgb)
            pts = None
            if res.multi_face_landmarks:
                lm = res.multi_face_landmarks[0].landmark
                pts = [(p.x, p.y) for p in lm]
            try:
                self._process_frame(pts)
                self.m["frameSeq"] = seq
                self.m["captureAt"] = datetime.fromtimestamp(
                    time.time() - max(0.0, time.perf_counter() - captured), timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                self.m["processingMs"] = round((time.perf_counter() - t0) * 1000, 1)
                self.m["processedAt"] = iso_now()
                self._push()
            except Exception as exc:  # never kill the camera loop
                import logging
                logging.getLogger(__name__).exception("Fatigue frame processing failed")
            self._draw_hud(frame, pts)
            ok, buf = cv2.imencode(".jpg", cv2.flip(frame, 1), [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                with self._lock:
                    self._jpeg = buf.tobytes()
            time.sleep(max(0.0, period - (time.perf_counter() - t0)))

    def _draw_hud(self, frame, pts):
        if not pts:
            return
        h, w = frame.shape[:2]
        for i in range(0, len(pts), 2):
            cv2.circle(frame, (int(pts[i][0] * w), int(pts[i][1] * h)), 1, HUD_BGR, -1)
        for contour in (L_EYE, R_EYE, MOUTH_CONTOUR):
            poly = [[int(pts[i][0] * w), int(pts[i][1] * h)] for i in contour]
            cv2.polylines(frame, [__import__("numpy").array(poly, dtype="int32")], True, HUD_BGR, 2)

    # ------------------------------------------------------------------ helpers
    def _emit(self, event_type, risk_level, description, t=None):
        t = time.perf_counter() if t is None else t
        self.on_event({
            "eventType": event_type, "riskLevel": risk_level, "riskScore": self._risk_ema,
            "eventTime": iso_now(), "elapsedS": max(0.0, t - self._started_at),
            "description": description,
        })

    def _add_pressure(self, weight):
        self._recent.append((weight, time.perf_counter()))

    def _prune_window(self, now):
        cutoff = now - 60.0
        while len(self._closure_window) > 2 and self._closure_window[0][0] < cutoff:
            self._closure_window.pop(0)

    # ------------------------------------------------------------------ per frame
    def _process_frame(self, pts):
        now = time.perf_counter()
        dt = min(0.3, now - self._last_tick)
        self._last_tick = now
        elapsed = max(0.0, now - self._started_at)
        m = self.m
        m["monitoredS"] = elapsed

        if pts is None:
            m["faceDetected"] = False
            m["eyesClosedS"] = 0.0
            m["mar"] = 0.0
            self._eyes_closed_s = 0.0
            self._closure_window.append((now, False))
            self._prune_window(now)
            self._update_risk(dt)
            self._update_yawn(0.0, now)
            if self._no_face_since is None:
                self._no_face_since = now
            if (now - self._no_face_since > 5 and now - self._last_absence > 30 and self._calibrated):
                self._last_absence = now
                m["absences"] += 1
                self._emit("ABSENCE", "CAUTION", "No face detected for 5s+ — check camera angle", now)
            self._push()
            return

        m["faceDetected"] = True
        self._no_face_since = None

        ear = (_ear_of(pts, L_EYE) + _ear_of(pts, R_EYE)) / 2
        m["ear"] = ear

        # head pose proxies
        eye_mid_y = (pts[L_EYE_OUTER][1] + pts[R_EYE_OUTER][1]) / 2
        eye_mid_x = (pts[L_EYE_OUTER][0] + pts[R_EYE_OUTER][0]) / 2
        face_h = _dist(pts[FOREHEAD], pts[CHIN]) or 1e-6
        face_w = _dist(pts[CHEEK_L], pts[CHEEK_R]) or 1e-6
        pitch_ratio = (pts[NOSE_TIP][1] - eye_mid_y) / face_h
        yaw_ratio = (pts[NOSE_TIP][0] - eye_mid_x) / face_w

        # MAR uses the inner upper/lower lip (13/14) normalized by mouth width (61/291).
        mar = _dist(pts[MOUTH_TOP], pts[MOUTH_BOTTOM]) / (_dist(pts[MOUTH_L], pts[MOUTH_R]) or 1e-6)
        m["mar"] = mar

        # ---- calibration (first ~3 s of tracked face)
        if not self._calibrated:
            self._ear_samples.append(ear)
            self._pitch_samples.append(pitch_ratio)
            self._mar_samples.append(mar)
            if elapsed >= 3 or len(self._ear_samples) >= 45:
                s = sorted(self._ear_samples)
                self._baseline_ear = s[len(s) // 2]
                self._threshold_ear = self._baseline_ear * 0.75
                ps = sorted(self._pitch_samples)
                self._baseline_pitch = ps[len(ps) // 2]
                # Use a low percentile so brief speech/open-mouth frames do not raise
                # the neutral baseline. The enter/exit thresholds below add hysteresis.
                ms = sorted(self._mar_samples)
                self._baseline_mar = ms[max(0, int((len(ms) - 1) * 0.25))] if ms else 0.0
                self._calibrated = True
                m["baselineEar"] = self._baseline_ear
                m["thresholdEar"] = self._threshold_ear
                self._emit("CALIBRATED", "ALERT",
                           f"open-eye EAR {self._baseline_ear:.3f} -> threshold {self._threshold_ear:.3f}")

        # ---- eye closure / blinks
        closed = self._calibrated and ear < self._threshold_ear
        self._closure_window.append((now, closed))
        self._prune_window(now)

        if closed and not self._eyes_closed:
            self._eyes_closed = True
            self._closed_at = now
        elif not closed and self._eyes_closed:
            self._eyes_closed = False
            closed_s = now - self._closed_at
            if closed_s >= 1.5:
                m["microsleeps"] += 1
                self._add_pressure(25)
                self._risk_ema = max(self._risk_ema, 80)
                self._emit("MICROSLEEP", "CRITICAL", f"eyes closed {closed_s:.1f}s")
            elif closed_s >= 0.4:
                m["slowBlinks"] += 1
                self._add_pressure(6)
                self._emit("SLOW_BLINK", "CAUTION", f"slow blink {closed_s:.1f}s")
            else:
                m["blinks"] += 1
        self._eyes_closed_s = (now - self._closed_at) if self._eyes_closed else 0.0
        m["eyesClosedS"] = self._eyes_closed_s
        self._max_closure = max(self._max_closure, self._eyes_closed_s)

        # PERCLOS over rolling 60 s window
        closed_time = span = 0.0
        w = self._closure_window
        for i in range(1, len(w)):
            d = w[i][0] - w[i - 1][0]
            span += d
            if w[i][1]:
                closed_time += d
        m["perclos"] = (closed_time / span) * 100 if span > 2 else 0.0
        self._max_perclos = max(self._max_perclos, m["perclos"])

        # ---- head posture
        m["headPitchDeg"] = (pitch_ratio - self._baseline_pitch) * 250 if self._calibrated else 0.0  # negative = head down
        m["headYawDeg"] = yaw_ratio * 400 if self._calibrated else 0.0

        if self._calibrated:
            # head nod: pitch < -15 deg sustained 0.8 s
            if m["headPitchDeg"] < -15:
                if self._nod_start is None:
                    self._nod_start = now
                elif now - self._nod_start > 0.8 and now - self._last_nod > 3:
                    self._last_nod = now
                    m["headNods"] += 1
                    self._add_pressure(15)
                    self._risk_ema = max(self._risk_ema, 55)
                    self._emit("HEAD_NOD", "WARNING", f"head down {round(m['headPitchDeg'])} deg")
            else:
                self._nod_start = None

            # distraction: |yaw| > 25 deg sustained 1.2 s
            if abs(m["headYawDeg"]) > 25:
                if self._distract_start is None:
                    self._distract_start = now
                elif now - self._distract_start > 1.2 and now - self._last_distract > 5:
                    self._last_distract = now
                    m["distractions"] += 1
                    self._add_pressure(6)
                    self._emit("DISTRACTION", "WARNING", f"looking away {round(m['headYawDeg'])} deg")
            else:
                self._distract_start = None

        self._update_yawn(mar, now)

        self._update_risk(dt)
        self._push()

    def _update_yawn(self, mar, now):
        # A neutral-face calibrated threshold adapts per driver. Hysteresis and a
        # sustained-open requirement filter speech and short mouth movements.
        enter = max(0.28, self._baseline_mar + 0.20, self._baseline_mar * 2.2)
        leave = max(0.12, enter * 0.62)
        self.m["marThreshold"] = round(enter, 3)
        if not self._calibrated:
            self.m["yawnStatus"] = "CALIBRATING"
            return
        if not self._yawn_latched:
            if mar >= enter:
                if self._yawn_start is None:
                    self._yawn_start = now
                self.m["yawnStatus"] = "OPENING"
                if now - self._yawn_start >= self._yawn_min_duration:
                    self._yawn_latched = True
                    self._yawn_release_start = None
                    self._last_yawn_at = iso_now()
                    self.m["lastYawnAt"] = self._last_yawn_at
                    self.m["yawns"] += 1
                    self.m["yawnStatus"] = "YAWNING"
                    self._add_pressure(8)
                    self._emit("YAWN", "WARNING", f"sustained yawn (MAR {mar:.2f}, {now - self._yawn_start:.1f}s)", now)
            else:
                self._yawn_start = None
                self.m["yawnStatus"] = "CLOSED"
            return

        if mar <= leave:
            if self._yawn_release_start is None:
                self._yawn_release_start = now
            self.m["yawnStatus"] = "RECOVERING"
            if now - self._yawn_release_start >= self._yawn_release_duration:
                self._yawn_latched = False
                self._yawn_start = self._yawn_release_start = None
                self.m["yawnStatus"] = "CLOSED"
        else:
            self._yawn_release_start = None
            self.m["yawnStatus"] = "YAWNING"

    def _update_risk(self, dt):
        now = time.perf_counter()
        self._recent = [(wt, at) for wt, at in self._recent if now - at < 60]
        event_pressure = sum(wt * (1 - (now - at) / 60) for wt, at in self._recent)
        event_pressure = min(40.0, event_pressure)

        closure_pressure = min(1.0, self._eyes_closed_s / 2.5) * 55
        perclos_pressure = min(1.0, max(0.0, self.m["perclos"]) / 25) * 35

        clamped = max(0.0, min(100.0, closure_pressure + perclos_pressure + event_pressure))
        alpha = 0.3 if clamped > self._risk_ema else 0.05
        self._risk_ema += (clamped - self._risk_ema) * min(1.0, alpha * dt * 15)
        if self._risk_ema < 0.5:
            self._risk_ema = 0.0

        self.m["risk"] = self._risk_ema
        self._max_risk = max(self._max_risk, self._risk_ema)
        self._risk_sum += self._risk_ema * dt
        self._risk_count += dt

        new_level = level_for_risk(self._risk_ema)
        if LEVEL_SEVERITY[new_level] > LEVEL_SEVERITY[self._max_level]:
            self._max_level = new_level
        if LEVEL_SEVERITY[new_level] > LEVEL_SEVERITY[self._level]:
            self._emit("LEVEL", new_level, f"{self._level} -> {new_level}")
            self._level = new_level
        elif LEVEL_SEVERITY[new_level] < LEVEL_SEVERITY[self._level]:
            self._level = new_level
        self.m["level"] = new_level

    def _push(self):
        self.on_metrics(dict(self.m))

    # ------------------------------------------------------------------ reporting
    def tick_level_seconds(self, dt=1.0):
        self._level_seconds[self._level] += dt

    @property
    def summary(self):
        m, ls = self.m, self._level_seconds
        cw = ls["CAUTION"] + ls["WARNING"] + ls["CRITICAL"]
        return {
            "monitored_s": round(m["monitoredS"], 1),
            "blinks": m["blinks"], "slow_blinks": m["slowBlinks"], "microsleeps": m["microsleeps"],
            "yawns": m["yawns"], "head_nods": m["headNods"], "distractions": m["distractions"],
            "absences": m["absences"],
            "longest_closure_s": round(self._max_closure, 2),
            "max_risk": round(self._max_risk, 1),
            "avg_risk": round(self._risk_sum / self._risk_count, 1) if self._risk_count > 0 else 0,
            "max_perclos": round(self._max_perclos, 1),
            "pct_caution_or_worse": round(cw / m["monitoredS"] * 100, 1) if m["monitoredS"] > 0 else 0,
            "seconds_in_level": {k: round(ls[k], 1) for k in LEVELS},
            "peak_level": self._max_level,
        }
