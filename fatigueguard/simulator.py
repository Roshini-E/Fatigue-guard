"""Camera-less demo simulator - same callbacks as FatigueEngine, scripted drowsiness arc."""
import random
import threading
import time

from .engine import LEVEL_SEVERITY, LEVELS, empty_metrics, iso_now, level_for_risk

INF = float("inf")
SCRIPT = [
    # until, risk, perclos, pitch, eyesClosed, yawn, lookingAway
    (4, 6, 2, 0, False, False, False),        # calibration
    (10, 12, 4, -2, False, False, False),     # alert, normal blinks
    (16, 34, 14, -4, False, False, False),    # caution creeping in
    (22, 58, 22, -19, False, False, False),   # head nod -> warning
    (27, 86, 38, -22, True, False, False),    # first microsleep
    (33, 92, 46, -24, True, False, False),    # second microsleep
    (40, 46, 24, -3, False, False, False),    # woke up
    (48, 16, 8, 0, False, False, False),      # recovery
    (56, 30, 12, -2, False, True, False),     # yawn
    (66, 22, 9, 0, False, False, True),       # distraction
    (INF, 10, 5, 0, False, False, False),     # steady state
]


class FatigueSimulator:
    def __init__(self, on_metrics, on_event):
        self.on_metrics, self.on_event = on_metrics, on_event
        self._running = False
        self._thread = None
        self._started = 0.0
        self._fired = set()
        self._last_blink = 0.0
        self.m = empty_metrics()
        self.m["faceDetected"] = True
        self._level = "ALERT"
        self._max_level = "ALERT"
        self._ls = {k: 0.0 for k in LEVELS}

    def start(self):
        self._started = time.perf_counter()
        self._running = True
        self._emit("CALIBRATED", "ALERT", "open-eye EAR 0.348 -> threshold 0.261 (simulated)")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def get_jpeg(self):
        return None

    def _emit(self, etype, level, desc):
        self.on_event({
            "eventType": etype, "riskLevel": level, "riskScore": self.m["risk"],
            "eventTime": iso_now(), "elapsedS": max(0.0, time.perf_counter() - self._started),
            "description": desc,
        })

    def _loop(self):
        while self._running:
            self._step()
            time.sleep(0.1)

    def _fire(self, key, cond, fn):
        if cond and key not in self._fired:
            self._fired.add(key)
            fn()

    def _step(self):
        now = time.perf_counter()
        el = now - self._started
        ph = next((p for p in SCRIPT if el < p[0]), SCRIPT[-1])
        _, risk, perclos, pitch, closed, _, away = ph
        m = self.m
        jitter = (random.random() - 0.5) * 4

        m["monitoredS"] = el
        m["faceDetected"] = True
        m["risk"] = max(0.0, min(100.0, risk + jitter * 0.4))
        m["perclos"] = max(0.0, perclos + jitter * 0.2)
        m["headPitchDeg"] = pitch + jitter * 0.6
        m["headYawDeg"] = 30 + jitter if away else jitter * 0.8
        m["ear"] = 0.18 if closed else 0.32 + random.random() * 0.03
        m["eyesClosedS"] = (el % 2 + 1.2) if closed else 0.0
        if el > 4:
            m["baselineEar"], m["thresholdEar"] = 0.348, 0.261

        if not closed and el > 4 and now - self._last_blink > 2.2:
            self._last_blink = now
            m["blinks"] += 1

        def ev(t, lv, d, counter=None):
            def f():
                if counter:
                    m[counter] += 1
                self._emit(t, lv, d)
            return f

        self._fire("b1", 4.5 < el < 10, ev("SLOW_BLINK", "CAUTION", "slow blink 0.7s"))
        self._fire("lv-caution", 14 <= el < 16, ev("LEVEL", "CAUTION", "ALERT -> CAUTION"))
        self._fire("nod", 17 <= el < 19, ev("HEAD_NOD", "WARNING", "head down -18 deg", "headNods"))
        self._fire("lv-warning", 18 <= el < 20, ev("LEVEL", "WARNING", "CAUTION -> WARNING"))
        self._fire("ms1", 22.5 <= el < 24, ev("MICROSLEEP", "CRITICAL", "eyes closed 1.8s", "microsleeps"))
        self._fire("lv-critical", 24 <= el < 26, ev("LEVEL", "CRITICAL", "WARNING -> CRITICAL"))
        self._fire("ms2", 28.5 <= el < 30, ev("MICROSLEEP", "CRITICAL", "eyes closed 2.3s", "microsleeps"))
        self._fire("yawn", 51 <= el < 53, ev("YAWN", "WARNING", "yawn detected (MAR 0.78)", "yawns"))
        self._fire("distract", 60 <= el < 62, ev("DISTRACTION", "WARNING", "looking away 34 deg", "distractions"))

        new_level = level_for_risk(m["risk"])
        self._level = new_level
        if LEVEL_SEVERITY[new_level] > LEVEL_SEVERITY[self._max_level]:
            self._max_level = new_level
        m["level"] = new_level
        self.on_metrics(dict(m))

    def tick_level_seconds(self, dt=1.0):
        self._ls[self._level] += dt

    @property
    def summary(self):
        m, ls = self.m, self._ls
        total = m["monitoredS"]
        cw = ls["CAUTION"] + ls["WARNING"] + ls["CRITICAL"]
        return {
            "monitored_s": round(total, 1), "blinks": m["blinks"], "slow_blinks": m["slowBlinks"],
            "microsleeps": m["microsleeps"], "yawns": m["yawns"], "head_nods": m["headNods"],
            "distractions": m["distractions"], "absences": m["absences"],
            "longest_closure_s": 2.3,
            "max_risk": 92 if m["microsleeps"] > 0 else round(m["risk"], 1),
            "avg_risk": round(cw / max(1, total) * 100 * 0.6, 1),
            "max_perclos": 46 if m["microsleeps"] > 0 else round(m["perclos"], 1),
            "pct_caution_or_worse": round(cw / total * 100, 1) if total > 0 else 0,
            "seconds_in_level": {k: round(ls[k], 1) for k in LEVELS},
            "peak_level": self._max_level,
        }
