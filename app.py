#!/usr/bin/env python3
"""Flask web interface for the ACSM to DRM-free EPUB/PDF converter."""

import os
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from functools import wraps
from pathlib import Path

from authlib.integrations.flask_client import OAuth
from flask import (
    Flask, jsonify, make_response, render_template, request,
    send_from_directory, session, redirect, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from converter import convert_pipeline

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

# Railway (like any reverse proxy) terminates TLS in front of gunicorn.
# Without this, url_for(_external=True) builds http:// URLs and Google
# rejects the OAuth redirect.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# -- Google OAuth config ----------------------------------------------------
#
# Set these in your Railway service variables:
#   SECRET_KEY           - fixed value, so sessions survive restarts
#   GOOGLE_CLIENT_ID     - Google Cloud Console -> Credentials
#   GOOGLE_CLIENT_SECRET - Google Cloud Console -> Credentials
#   ALLOWED_EMAIL        - the only account permitted to sign in
#
# In Google Cloud Console -> Credentials -> OAuth 2.0 Client, add:
#   https://<your-app>.up.railway.app/auth/google/callback
#
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_EMAIL        = os.environ.get("ALLOWED_EMAIL", "")

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def _base_url():
    """Public base URL of this deployment, for the OAuth redirect URI.

    APP_BASE_URL wins if set; otherwise Railway injects the public domain.
    """
    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    if base:
        return base
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        return f"https://{domain}"
    return ""


# -- Paths ------------------------------------------------------------------
#
# Everything mutable lives under DATA_DIR so a single mounted Railway
# volume at /app/data persists books and the Adobe device registration.

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR   = Path(os.environ.get("DATA_DIR", SCRIPT_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
COVER_DIR  = DATA_DIR / "covers"

for _d in (UPLOAD_DIR, OUTPUT_DIR, COVER_DIR):
    _d.mkdir(parents=True, exist_ok=True)

BOOK_SUFFIXES = (".epub", ".pdf")

TOTAL_STEPS = 6

STEP_LABELS = {
    1: "Checking tools...",
    2: "Detecting format...",
    3: "Registering Adobe device...",
    4: "Downloading ebook...",
    5: "Removing DRM...",
    6: "Verifying output...",
}

active_jobs = {}
_active_jobs_lock = threading.Lock()


# -- Auth helpers -----------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login")
def login():
    # If OAuth is not configured, show a helpful error instead of a blank page.
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        error = (
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET, and ALLOWED_EMAIL in your Railway "
            "service variables."
        )
        return render_template("login.html", error=error)
    return render_template("login.html", error=None)


@app.route("/login/google")
def login_google():
    # Prefer an explicit base URL over url_for(), which can produce http://
    # behind a reverse proxy.
    base = _base_url()
    if base:
        redirect_uri = f"{base}/auth/google/callback"
    else:
        redirect_uri = url_for("auth_callback", _external=True, _scheme="https")
    print(f"[DEBUG] OAuth redirect_uri = {redirect_uri}", flush=True)
    return oauth.google.authorize_redirect(redirect_uri)


# Both callback paths are served so that whichever redirect URI is already
# registered in Google Cloud Console keeps working.
@app.route("/auth/google/callback")
@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        return render_template("login.html", error=f"OAuth error: {e}")

    user_info = token.get("userinfo")
    if not user_info:
        return render_template(
            "login.html", error="Could not retrieve user info from Google.")

    email   = user_info.get("email", "").lower().strip()
    allowed = ALLOWED_EMAIL.lower().strip()

    if not allowed:
        return render_template(
            "login.html",
            error="ALLOWED_EMAIL is not set. Add it to your Railway "
                  "service variables.",
        )

    if email != allowed:
        return render_template(
            "login.html",
            error=f"Access denied: {email} is not authorised to use this app.",
        )

    session["authenticated"] = True
    session["user_email"]    = email
    session["user_name"]     = user_info.get("name", email)
    session["user_picture"]  = user_info.get("picture", "")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -- Cover extraction -------------------------------------------------------

def extract_epub_cover(epub_path):
    """Pull the cover image out of an EPUB's ZIP container."""
    for ext in (".jpg", ".jpeg", ".png"):
        existing = COVER_DIR / f"{epub_path.stem}{ext}"
        if existing.exists():
            return existing.name
    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            cover_name = _find_cover_in_opf(zf) or _find_cover_by_name(zf)
            if cover_name:
                data = zf.read(cover_name)
                ext = Path(cover_name).suffix or ".jpg"
                cover_out = COVER_DIR / f"{epub_path.stem}{ext}"
                cover_out.write_bytes(data)
                return cover_out.name
    except Exception:
        pass
    return None


def _find_cover_in_opf(zf):
    opf_path = None
    for name in zf.namelist():
        if name.endswith(".opf"):
            opf_path = name
            break
    if not opf_path:
        return None
    opf_xml = zf.read(opf_path).decode("utf-8", errors="replace")
    root = ET.fromstring(opf_xml)
    cover_id = None
    for meta in root.iter():
        if meta.tag.endswith("}meta") or meta.tag == "meta":
            if meta.get("name") == "cover":
                cover_id = meta.get("content")
                break
    if not cover_id:
        for item in root.iter():
            if item.tag.endswith("}item") or item.tag == "item":
                if "cover-image" in (item.get("properties") or ""):
                    href = item.get("href")
                    if href:
                        opf_dir = str(Path(opf_path).parent)
                        return href if opf_dir == "." else f"{opf_dir}/{href}"
        return None
    for item in root.iter():
        if item.tag.endswith("}item") or item.tag == "item":
            if item.get("id") == cover_id:
                href = item.get("href")
                if href:
                    opf_dir = str(Path(opf_path).parent)
                    return href if opf_dir == "." else f"{opf_dir}/{href}"
    return None


def _find_cover_by_name(zf):
    for name in zf.namelist():
        lower = name.lower()
        if "cover" in lower and any(lower.endswith(ext)
                                    for ext in (".jpg", ".jpeg", ".png")):
            return name
    return None


def extract_pdf_cover(pdf_path):
    """Render the PDF's first page as the cover thumbnail."""
    cover_out = COVER_DIR / f"{pdf_path.stem}.jpg"
    if cover_out.exists():
        return cover_out.name
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        if len(doc) > 0:
            page = doc[0]
            mat  = fitz.Matrix(1.5, 1.5)
            pix  = page.get_pixmap(matrix=mat)
            pix.save(str(cover_out))
            doc.close()
            return cover_out.name
        doc.close()
    except ImportError:
        pass
    except Exception:
        pass
    return None


def extract_cover(path):
    """Dispatch cover extraction on file type."""
    if path.suffix.lower() == ".pdf":
        return extract_pdf_cover(path)
    return extract_epub_cover(path)


# -- Library helpers --------------------------------------------------------

def get_books():
    """List converted books, newest first, across both formats.

    A single book stem may hold both an EPUB and a PDF; they are grouped
    into one entry with one file row each.
    """
    if not OUTPUT_DIR.exists():
        return [], 0
    books = OrderedDict()
    total_files = 0
    for f in sorted(OUTPUT_DIR.iterdir(),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix.lower() not in BOOK_SUFFIXES:
            continue
        stem = f.stem
        if not stem:
            continue
        if stem not in books:
            books[stem] = {"stem": stem, "files": [], "cover": None,
                           "formats": []}
        ext = f.suffix[1:].upper()
        size_mb = f.stat().st_size / (1024 * 1024)
        books[stem]["files"].append({
            "name": f.name,
            "size": f"{size_mb:.1f} MB",
            "ext":  ext,
        })
        if ext not in books[stem]["formats"]:
            books[stem]["formats"].append(ext)
        total_files += 1
        if not books[stem]["cover"]:
            cover = extract_cover(f)
            if cover:
                books[stem]["cover"] = cover
    # Stable order for the library's format badges and filter attribute,
    # independent of which file happened to be written last.
    for b in books.values():
        b["formats"].sort()
    return list(books.values()), total_files


def _prune_old_jobs():
    cutoff = time.time() - 7200
    with _active_jobs_lock:
        stale = [
            jid for jid, job in active_jobs.items()
            if job["status"] in ("done", "error") and job["start_time"] < cutoff
        ]
        for jid in stale:
            del active_jobs[jid]


def _purge_stem(stem):
    """Delete every artefact belonging to one book stem."""
    deleted = []
    for f in list(OUTPUT_DIR.iterdir()):
        if f.stem == stem and f.suffix.lower() in BOOK_SUFFIXES:
            try:
                f.unlink(missing_ok=True)
                deleted.append(f.name)
            except Exception:
                pass
    for d in (UPLOAD_DIR, COVER_DIR):
        if not d.exists():
            continue
        for f in list(d.iterdir()):
            if f.stem == stem:
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
    return deleted


# -- Conversion worker ------------------------------------------------------

def run_conversion_job(job_id, acsm_path, output_dir, requested_format=None):
    with _active_jobs_lock:
        job = active_jobs[job_id]

    print(f"[JOB] {job_id} started: acsm={acsm_path}, "
          f"format={requested_format or 'auto'}", flush=True)
    try:
        job["current_step"]  = 1
        job["current_label"] = STEP_LABELS[1]

        for step, message in convert_pipeline(str(acsm_path), str(output_dir),
                                              requested_format):
            print(f"[JOB] {job_id} step={step} message={message}", flush=True)
            if step == "done":
                job["steps"].append({"step": "done", "message": message})
                job["status"]       = "done"
                job["done_message"] = message
            else:
                step_num   = int(step)
                is_warning = step_num == 6 and (
                    "image-only" in message.lower() or "unusual" in message.lower()
                )
                job["steps"].append({
                    "step": step_num, "message": message, "warning": is_warning,
                })
                next_step = step_num + 1
                if next_step <= TOTAL_STEPS:
                    job["current_step"]  = next_step
                    job["current_label"] = STEP_LABELS.get(next_step, "")
    except RuntimeError as e:
        print(f"[JOB] {job_id} RuntimeError: {e}", flush=True)
        job["status"] = "error"
        job["error"]  = str(e)
    except Exception as e:
        print(f"[JOB] {job_id} Exception: {e}\n{traceback.format_exc()}",
              flush=True)
        job["status"] = "error"
        job["error"]  = f"Unexpected error: {e}"


# -- Routes -----------------------------------------------------------------

@app.route("/")
@login_required
def index():
    books, total_files = get_books()
    resp = make_response(render_template(
        "index.html",
        books=books,
        user_name=session.get("user_name", ""),
        user_picture=session.get("user_picture", ""),
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/library")
@login_required
def library():
    books, total_files = get_books()
    resp = make_response(render_template(
        "library.html",
        books=books,
        total_files=total_files,
        user_name=session.get("user_name", ""),
        user_picture=session.get("user_picture", ""),
    ))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file provided"}), 400
    if not file.filename.endswith(".acsm"):
        return jsonify({"error": "Only .acsm files are accepted"}), 400
    filename  = Path(file.filename).name
    save_path = UPLOAD_DIR / filename
    file.save(save_path)
    return jsonify({"filename": filename})


@app.route("/start-convert/<filename>", methods=["POST"])
@login_required
def start_convert(filename):
    _prune_old_jobs()
    filename  = Path(filename).name
    acsm_path = UPLOAD_DIR / filename
    if not acsm_path.exists():
        return jsonify({"error": "File not found"}), 404

    # The format picker in the UI posts the user's choice; it is validated
    # against the ACSM token before any network call is made.
    payload = request.get_json(silent=True) or {}
    requested_format = (payload.get("format") or request.form.get("format")
                        or "").lower().strip() or None
    if requested_format and requested_format not in ("epub", "pdf"):
        return jsonify({"error": f"Unsupported format: {requested_format}"}), 400

    job_id = f"{filename}_{int(time.time())}"
    with _active_jobs_lock:
        active_jobs[job_id] = {
            "filename":      filename,
            "format":        requested_format,
            "status":        "running",
            "steps":         [],
            "current_step":  0,
            "current_label": "",
            "error":         None,
            "done_message":  None,
            "start_time":    time.time(),
        }

    t = threading.Thread(
        target=run_conversion_job,
        args=(job_id, acsm_path, OUTPUT_DIR, requested_format),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/job-status/<job_id>")
@login_required
def job_status(job_id):
    with _active_jobs_lock:
        if job_id not in active_jobs:
            return jsonify({"error": "Job not found"}), 404
        job = active_jobs[job_id]

    return jsonify({
        "status":        job["status"],
        "steps":         job["steps"],
        "current_step":  job["current_step"],
        "current_label": job["current_label"],
        "error":         job["error"],
        "done_message":  job["done_message"],
        "elapsed":       round(time.time() - job["start_time"]),
    })


@app.route("/download/<filename>")
@login_required
def download(filename):
    filename  = Path(filename).name
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


@app.route("/delete/<stem>", methods=["POST"])
@login_required
def delete_book(stem):
    """Delete one book: every format of it, plus its cover and upload."""
    stem = Path(stem).stem
    if not stem:
        return jsonify({"error": "Invalid stem"}), 400
    deleted = _purge_stem(stem)
    return jsonify({"status": "deleted", "deleted": deleted})


@app.route("/delete-all", methods=["POST"])
@login_required
def delete_all():
    """Empty the library."""
    deleted = []
    for f in list(OUTPUT_DIR.iterdir()):
        if f.suffix.lower() in BOOK_SUFFIXES:
            try:
                name = f.name
                f.unlink()
                deleted.append(name)
            except Exception:
                pass

    for d in (UPLOAD_DIR, COVER_DIR):
        if not d.exists():
            continue
        for f in list(d.iterdir()):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    return jsonify({"status": "deleted", "deleted": deleted})


@app.route("/cover/<filename>")
@login_required
def cover(filename):
    filename = Path(filename).name
    return send_from_directory(COVER_DIR, filename)


@app.route("/debug-status")
@login_required
def debug_status():
    import shutil
    jobs_summary = {}
    with _active_jobs_lock:
        for jid, job in active_jobs.items():
            jobs_summary[jid] = {
                "status":       job["status"],
                "format":       job.get("format"),
                "steps_count":  len(job["steps"]),
                "current_step": job["current_step"],
                "error":        job["error"],
                "elapsed":      round(time.time() - job["start_time"]),
            }
    upload_files = [f.name for f in UPLOAD_DIR.iterdir()] if UPLOAD_DIR.exists() else []
    output_files = [f.name for f in OUTPUT_DIR.iterdir()] if OUTPUT_DIR.exists() else []
    return jsonify({
        "active_jobs":          jobs_summary,
        "data_dir":             str(DATA_DIR),
        "upload_files":         upload_files,
        "output_files":         output_files,
        "acsmdownloader_found": shutil.which("acsmdownloader")
                                or str(SCRIPT_DIR / "libgourou/utils/acsmdownloader"),
        "libgourou_exists":     (SCRIPT_DIR / "libgourou" / "utils" / "acsmdownloader").exists(),
        "base_url":             _base_url(),
        "logged_in_as":         session.get("user_email", "unknown"),
    })


if __name__ == "__main__":
    _port_env = os.environ.get("PORT", "8080")
    if not _port_env.isdigit():
        print(f"WARN: PORT='{_port_env}' is not numeric, defaulting to 8080", file=sys.stderr)
        _port_env = "8080"
    app.run(debug=False, host="0.0.0.0", port=int(_port_env), threaded=True)
