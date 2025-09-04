from __future__ import annotations
import os, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from flask import (
    Flask, g, render_template, request, redirect, url_for,
    flash, jsonify, send_from_directory, session
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR  = os.path.abspath(os.path.dirname(__file__))
DB_PATH   = os.path.join(BASE_DIR, "app.db")
MEDIA_DIR = os.path.join(BASE_DIR, "media")

# ---- Timezones ----
try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    raise RuntimeError("Time zone data not found. In your venv: pip install tzdata")

app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path="/static")
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret"),
    TEMPLATES_AUTO_RELOAD=True,
    SEND_FILE_MAX_AGE_DEFAULT=0,
)

ALLOWED_EXT = {".png",".jpg",".jpeg",".gif",".webp",".mp4",".webm",".ogg",".m4v",".mov"}

# ---------------- DB ----------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        need_init = not os.path.exists(DB_PATH)
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row

        # Ensure settings infra exists on EVERY startup (works for old DBs too)
        _ensure_settings_table(db)
        _ensure_display_default(db)
        _ensure_admin_pin(db)

        if need_init:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS waitlist (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              name        TEXT    NOT NULL,
              phone       TEXT    NOT NULL,
              seats       INTEGER NOT NULL,
              notes       TEXT,
              status      TEXT    NOT NULL DEFAULT 'WAITING',
              requesttime TEXT    NOT NULL DEFAULT (datetime('now'))  -- UTC
            );
            CREATE INDEX IF NOT EXISTS idx_waitlist_status  ON waitlist(status);
            CREATE INDEX IF NOT EXISTS idx_waitlist_request ON waitlist(requesttime);
            """)
            db.commit()

        _ensure_lifecycle_columns(db)
    return db

def _ensure_settings_table(db: sqlite3.Connection):
    db.execute("""
      CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
      );
    """)
    db.commit()

def _ensure_display_default(db: sqlite3.Connection):
    db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('display_mode','open')")
    db.commit()

def _ensure_admin_pin(db: sqlite3.Connection):
    # If no admin_pin_hash set, initialize from env or default 123456
    row = db.execute("SELECT value FROM settings WHERE key='admin_pin_hash'").fetchone()
    if not row or not row["value"]:
        pin = os.getenv("ADMIN_PIN", "123456").strip()
        h   = generate_password_hash(pin)
        db.execute("INSERT INTO settings(key,value) VALUES('admin_pin_hash', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (h,))
        db.commit()

def _ensure_lifecycle_columns(db: sqlite3.Connection):
    for coldef in [
        "requested_at TEXT",
        "assigned_at  TEXT",
        "seated_at    TEXT",
        "deleted_at   TEXT"
    ]:
        try:
            db.execute(f"ALTER TABLE waitlist ADD COLUMN {coldef}")
            db.commit()
        except sqlite3.OperationalError:
            pass

@app.teardown_appcontext
def close_db(_):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

# ------------- Settings helpers -------------
def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default

def set_setting(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    db.commit()

# ------------- Auth helpers -------------
def is_admin() -> bool:
    return bool(session.get("is_admin"))

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("Admin login required.", "error")
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

# make flags available in templates
@app.context_processor
def inject_globals():
    try:
        mode = get_setting("display_mode", "open")
    except Exception:
        mode = "open"
    return {"display_mode": mode, "is_admin": is_admin()}

# ---------------- Utils ----------------
def normalize_phone(p: str) -> str:
    return "".join(ch for ch in (p or "") if ch.isdigit() or ch == "+")

def fetch_media_files():
    items = []
    if os.path.isdir(MEDIA_DIR):
        for fn in sorted(os.listdir(MEDIA_DIR)):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in ALLOWED_EXT:
                items.append(fn)
    return items

def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def utc_str_to_et(ts: str) -> str:
    if not ts: return ""
    dt_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")

@app.template_filter("est")
def jinja_est(ts: str) -> str:
    return utc_str_to_et(ts)

# ---------------- Pages ----------------
@app.get("/")
def home():
    rows = []
    if get_setting("display_mode", "open") == "waitlist":
        rows = get_db().execute("""
          SELECT * FROM waitlist
          WHERE status IN ('WAITING','ASSIGNING') AND deleted_at IS NULL
          ORDER BY datetime(requesttime) ASC
          LIMIT 100
        """).fetchall()
    return render_template("home.html",
                           rows=rows,
                           media_files=fetch_media_files())

@app.get("/add_waitlist")
def add_waitlist_form():
    return render_template("add_waitlist.html")

@app.post("/add_waitlist")
def add_waitlist_submit():
    name  = (request.form.get("name")  or "").strip()
    phone = normalize_phone(request.form.get("phone") or "")
    notes = (request.form.get("notes") or "").strip()
    seats = request.form.get("seats")
    try:
        seats = int(seats)
    except Exception:
        seats = 0
    if not name or not phone or seats <= 0:
        flash("Enter name, valid phone, and seats (>0).", "error")
        return redirect(url_for("add_waitlist_form"))

    now_utc = utc_now_str()
    db = get_db()
    with closing(db.cursor()) as cur:
        cur.execute("""
            INSERT INTO waitlist (name, phone, seats, notes, status, requesttime, requested_at)
            VALUES (?, ?, ?, ?, 'WAITING', ?, ?)
        """, (name, phone, seats, notes, now_utc, now_utc))
    db.commit()
    flash("Party added to waitlist.", "success")
    return redirect(url_for("home"))

# -------- Admin-only pages --------
@app.get("/waitlist")
@admin_required
def waitlist_admin():
    rows = get_db().execute("""
      SELECT * FROM waitlist
      WHERE deleted_at IS NULL
      ORDER BY datetime(requesttime) ASC
    """).fetchall()
    return render_template("waitlist_admin.html", rows=rows)

# --- Media Manager page (admin) ---
@app.get("/media/manage")
@admin_required
def media_manage():
    return render_template("media_manager.html", media_files=fetch_media_files())

@app.post("/waitlist/<int:item_id>/assign")
@admin_required
def waitlist_assign(item_id: int):
    now_utc = utc_now_str()
    db = get_db()
    db.execute("""
        UPDATE waitlist
        SET status='ASSIGNING',
            assigned_at = ?
        WHERE id=? AND deleted_at IS NULL
    """, (now_utc, item_id))
    db.commit()
    return redirect(url_for("waitlist_admin"))

@app.post("/waitlist/<int:item_id>/seated")
@admin_required
def waitlist_seated(item_id: int):
    now_utc = utc_now_str()
    db = get_db()
    db.execute("""
        UPDATE waitlist
        SET status='SEATED',
            seated_at = COALESCE(seated_at, ?)
        WHERE id=? AND deleted_at IS NULL
    """, (now_utc, item_id))
    db.commit()
    return redirect(url_for("waitlist_admin"))

@app.post("/waitlist/<int:item_id>/delete")
@admin_required
def waitlist_delete(item_id: int):
    now_utc = utc_now_str()
    db = get_db()
    db.execute("""
        UPDATE waitlist
        SET deleted_at = ?
        WHERE id=? AND deleted_at IS NULL
    """, (now_utc, item_id))
    db.commit()
    flash("Entry removed.", "success")
    return redirect(url_for("waitlist_admin"))

# ---- Display mode controls (Admin-only) ----
@app.post("/display/start")
@admin_required
def display_start():
    set_setting("display_mode", "waitlist")
    flash("Display started. Home now shows the waitlist.", "success")
    return redirect(url_for("waitlist_admin"))

@app.post("/display/stop")
@admin_required
def display_stop():
    set_setting("display_mode", "open")
    flash("Display stopped. Home shows 'COME ON IN'.", "success")
    return redirect(url_for("waitlist_admin"))

# ---- Admin auth ----
@app.get("/admin/login")
def admin_login():
    return render_template("admin_login.html", next=request.args.get("next") or "")

@app.post("/admin/login")
def admin_login_post():
    pin = (request.form.get("pin") or "").strip()
    next_url = request.form.get("next") or url_for("waitlist_admin")
    row = get_db().execute("SELECT value FROM settings WHERE key='admin_pin_hash'").fetchone()
    if row and check_password_hash(row["value"], pin):
        session["is_admin"] = True
        flash("Logged in as admin.", "success")
        return redirect(next_url)
    flash("Invalid PIN.", "error")
    return redirect(url_for("admin_login", next=next_url))

@app.post("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out.", "success")
    return redirect(url_for("home"))

# --------------- JSON for live panel ---------------
@app.get("/api/waitlist")
def api_waitlist():
    rows = get_db().execute("""
      SELECT id, name, phone, seats, status, requesttime, assigned_at
      FROM waitlist
      WHERE status IN ('WAITING','ASSIGNING') AND deleted_at IS NULL
      ORDER BY datetime(requesttime) ASC
      LIMIT 100
    """).fetchall()
    out = []
    for r in rows:
        start_ts = r["assigned_at"] if (r["status"] == "ASSIGNING" and r["assigned_at"]) else r["requesttime"]
        d = dict(r)
        d["timer_start_utc"] = (start_ts or r["requesttime"]) + "Z"
        d["requesttime_et"]  = utc_str_to_et(r["requesttime"])
        out.append(d)
    return jsonify(out)

# --------------- Media ----------------
@app.get("/media/<path:filename>")
def media(filename: str):
    return send_from_directory(MEDIA_DIR, filename, as_attachment=False)

@app.post("/upload")
@admin_required
def upload():
    f = request.files.get("file")
    if not f or f.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("media_manage"))
    filename = secure_filename(os.path.basename(f.filename))
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        flash("Unsupported file type.", "error")
        return redirect(url_for("media_manage"))
    os.makedirs(MEDIA_DIR, exist_ok=True)
    f.save(os.path.join(MEDIA_DIR, filename))
    flash("File uploaded.", "success")
    return redirect(url_for("media_manage"))

# ---- Media delete (Admin-only) ----
def _safe_media_path(filename: str) -> Path:
    # Only allow plain filenames (no dirs) and known extensions
    name = os.path.basename(filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("Unsupported file type")
    return Path(MEDIA_DIR) / name

@app.post("/media/delete")
@admin_required
def media_delete():
    filename = (request.form.get("filename") or "").strip()
    try:
        p = _safe_media_path(filename)
    except Exception:
        flash("Invalid filename.", "error")
        return redirect(url_for("media_manage"))

    try:
        if p.exists():
            p.unlink()
            flash(f"Deleted {p.name}.", "success")
        else:
            flash("File not found.", "error")
    except Exception as e:
        flash(f"Could not delete: {e}", "error")

    return redirect(url_for("media_manage"))

# --------------- Health ---------------
@app.get("/status")
def status_api():
    row = get_db().execute("SELECT COUNT(*) AS c FROM waitlist WHERE deleted_at IS NULL").fetchone()
    return jsonify({
        "ok": True,
        "waitlist_count": row["c"],
        "media_count": len(fetch_media_files()),
        "display_mode": get_setting("display_mode","open"),
        "is_admin": is_admin()
    })

if __name__ == "__main__":
    app.run(debug=True)
