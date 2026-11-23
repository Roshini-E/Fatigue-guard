# FatigueGuard 🚗💤 — Python / OpenCV / MediaPipe edition

Same app, same screens, same behaviour as the Next.js version — the whole detection
pipeline now runs in Python.

| Layer | Original (Next.js) | This version |
| --- | --- | --- |
| Camera | browser `getUserMedia` | **OpenCV** `VideoCapture` (server side) |
| Face tracking | MediaPipe FaceMesh (JS/CDN) | **MediaPipe FaceMesh (Python)** |
| Fatigue engine | `engine.ts` | `fatigueguard/engine.py` (EAR, PERCLOS, blinks, microsleeps, yawns, head pose, risk levels) |
| Demo simulator | `simulator.ts` | `fatigueguard/simulator.py` |
| API + auth | Next.js routes + Prisma | **Flask** + SQLite (same tables) |
| Emergency e-mail | nodemailer | `smtplib` (STARTTLS :587) |
| UI | React + Tailwind + shadcn | identical dark UI (Tailwind classes), served by Flask |

## Run

```bash
python -m venv venv && venv\Scripts\activate      # Windows   (Linux/macOS: source venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env                             # (cp on Linux/macOS) – fill SMTP to send real e-mails
python app.py                                      # http://localhost:3000
```

Python 3.9–3.12 recommended. `requirements.txt` pins `mediapipe==0.10.14` because newer
releases removed the classic `mp.solutions.face_mesh` API this engine uses.
First run creates `db/custom.db` and seeds the demo data: **driver1 / demo1234**.

## Flow
1. **Login / Register** — each driver has a private account (+ optional vehicle).
2. **Contacts tab** — add/remove emergency contacts (name, email, phone, relationship, priority).
3. **Monitor tab → Start Monitoring** — OpenCV opens the camera; the live annotated feed,
   fatigue %, PERCLOS, EAR, blinks, yawns, head posture, eyes-closed time, microsleeps,
   head nods, distractions, slow blinks, absences and the event feed appear on screen.
4. **Drowsiness detected** (microsleep / head nod / CRITICAL level) → at the same moment
   the **siren rings** and the backend **e-mails every emergency contact** (background thread).
   Every attempt is stored in the Alert Log (delivered / simulated / failed).
5. **Stop & save report** — summary + events are saved in SQLite; see the **Reports** tab.

Without valid SMTP settings alerts are recorded as *simulated*. Gmail uses the account's
16-character App Password (never the regular account password). Set these values in the
project-root `.env`:

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-16-character-google-app-password
SMTP_FROM_EMAIL=FatigueGuard <you@gmail.com>
```

Port 587 uses STARTTLS; port 465 uses implicit SSL. Log in, open **Alert Log**, and use
**Send test email to my account** to verify SMTP. A result is marked delivered only if
the SMTP server accepts the recipient. `CAMERA_INDEX=1` in `.env` picks another camera.

## Layout
```
app.py                    Flask routes (auth, contacts, sessions, notifications, monitor, /video_feed)
fatigueguard/engine.py    OpenCV + MediaPipe detection engine
fatigueguard/simulator.py demo simulation (no camera)
fatigueguard/monitor.py   live session manager: DB flush every 3 s, alert thread, siren state
fatigueguard/alerts.py    e-mail every contact + Notification rows (30 s throttle)
fatigueguard/mailer.py    SMTP sender      fatigueguard/auth.py  scrypt + cookie sessions
fatigueguard/db.py        SQLite schema    scripts/seed.py       demo data
templates/ static/        the UI (same look as the original)
```
