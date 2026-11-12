from flask import (Flask, render_template, request, redirect, send_file, session, flash)
from flask import jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from dotenv import load_dotenv
from functools import wraps
import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd
import sqlite3 as _sqlite3_res
import json as _json_res
import uuid
import io
from PIL import Image, ImageOps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError
import os

# ---------------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES FROM .env
# ---------------------------------------------------------------
load_dotenv()

SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH")

# ---------------------------------------------------------------
# PHASE 5 — PRODUCTION HARDENING FLAGS (env-controlled)
# ---------------------------------------------------------------
ENV = os.environ.get("FLASK_ENV", "development").lower()
IS_PRODUCTION = ENV == "production"

# Whether cookies should require HTTPS (set to "true" on real server)
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "false").lower() == "true"

# Whether CSRF protection is enforced (auto-off in dev for templates pre-Message 2)
CSRF_ENABLED = os.environ.get("CSRF_ENABLED", "false").lower() == "true"

# Rate limit storage (memory works fine for single-process dev; use Redis for real deploys)
RATELIMIT_STORAGE = os.environ.get("RATELIMIT_STORAGE", "memory://")

# Public sales-demo credentials (read-only demo account).
# These are intentionally plaintext because they are DISPLAYED
# on the /demo-login page for prospective customers.
DEMO_USER = os.environ.get("DEMO_USER")
DEMO_PASS = os.environ.get("DEMO_PASS")

# Fail fast if any required env var is missing
if not SECRET_KEY or not ADMIN_USER or not ADMIN_PASS_HASH:
    raise RuntimeError(
        "Missing required environment variables. "
        "Check that your .env file contains SECRET_KEY, "
        "ADMIN_USER, and ADMIN_PASS_HASH."
    )

# Demo credentials are required only for the public demo flow;
# warn (don't crash) so the admin app keeps working if they're absent.
if not DEMO_USER or not DEMO_PASS:
    import warnings
    warnings.warn(
        "DEMO_USER / DEMO_PASS are not set in .env — "
        "the public demo login will be disabled until they are provided.",
        RuntimeWarning,
    )

# ---------------------------------------------------------------
# APP CONFIG
# ---------------------------------------------------------------
DB_PATH = Path("database/inventory.db")

app = Flask(__name__)
app.config["RATELIMIT_ENABLED"] = True
app.secret_key = SECRET_KEY

# ---------------------------------------------------------------
# PHASE 5 — SESSION + CSRF + LIMITER CONFIG
# ---------------------------------------------------------------
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SECURE_COOKIES
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=2)

# CSRF (Flask-WTF)
app.config["WTF_CSRF_ENABLED"] = CSRF_ENABLED
app.config["WTF_CSRF_TIME_LIMIT"] = 3600     # 1 hour

# Rate limiter (Flask-Limiter)
# app.config["RATELIMIT_STORAGE_URI"] = RATELIMIT_STORAGE
# app.config["RATELIMIT_HEADERS_ENABLED"] = True     # adds X-RateLimit-* headers
# app.config["RATELIMIT_DEFAULT"] = "200 per hour"   # global default

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=RATELIMIT_STORAGE,
    default_limits=["200 per hour"],
    headers_enabled=True,
)

# Attach to app AFTER creation — this is the v4-safe pattern
limiter.init_app(app)

csrf = CSRFProtect(app)

# ---------------------------------------------------------------
# PHASE 5 — SECURITY HEADERS
# ---------------------------------------------------------------
@app.after_request
def _add_security_headers(response):
    # Prevent clickjacking
    response.headers["X-Frame-Options"] = "DENY"

    # Prevent MIME-type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Referrer policy
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    # HSTS — only when HTTPS (production). Browsers ignore it on HTTP.
    if SECURE_COOKIES:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # Content-Security-Policy — allow self + inline (needed for existing templates)
    # Block external scripts. Allow images from self + data URIs for thumbnails.
    csp = (
        "default-src 'self'; "
        # Allow scripts from self + inline + unpkg CDN (Leaflet)
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        # Allow styles from self + inline + unpkg CDN (Leaflet)
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        # Allow images from self + data URIs + blob + OpenStreetMap tiles + Leaflet markers
        "img-src 'self' data: blob: "
            "https://*.tile.openstreetmap.org "
            "https://unpkg.com; "
        # Allow connections (fetch/XHR) from self + OpenStreetMap tiles (Leaflet loads tiles via XHR for some zoom levels)
        "connect-src 'self' https://*.tile.openstreetmap.org https://unpkg.com; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self';"
    )
    response.headers["Content-Security-Policy"] = csp

    # Prevent caching of admin pages so new data always appears
    if session.get("logged_in"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response

UPLOAD_FOLDER = "static/images/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------------------------------------------------------
# PHASE 1 — IMAGE / PHOTO UTILITIES
# ---------------------------------------------------------------

THUMBS_FOLDER = os.path.join(UPLOAD_FOLDER, "thumbs")
os.makedirs(THUMBS_FOLDER, exist_ok=True)

# Config
ALLOW_TENANT_SWITCHING = os.environ.get("ALLOW_TENANT_SWITCHING", "false").lower() == "true"
ALLOWED_EXTENSIONS      = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_MIME_FORMATS    = {"JPEG", "PNG", "WEBP"}   # Pillow-detected formats
MAX_UPLOAD_BYTES        = 8 * 1024 * 1024           # 8 MB per file
MIN_DIMENSION           = 200                       # px (width and height)
MAX_DIMENSION           = 8000                      # px
THUMB_SIZE              = (400, 400)                # grid thumbnail
MAX_PHOTOS_PER_ITEM     = 8


class PhotoValidationError(Exception):
    """Raised when an uploaded file fails validation."""
    pass


def _validate_extension(filename):
    """Whitelist-based extension check (case-insensitive)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise PhotoValidationError(
            f"File type {ext or '(none)'} is not allowed. "
            f"Use JPG, PNG, or WEBP."
        )
    return ext


def _open_and_validate_image(file_storage):
    """
    Read the uploaded file into memory, open it with Pillow, and
    verify:
      - The bytes really are an image (magic-byte check via Pillow)
      - Format is in the allowed set (rejects .bmp, .gif, .tiff, etc.)
      - Size is within MIN_DIMENSION..MAX_DIMENSION

    Returns a Pillow Image object that has been EXIF-rotated.
    Raises PhotoValidationError on any failure.
    """
    # Size cap (do this before Pillow tries to decode a huge file)
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)

    if size == 0:
        raise PhotoValidationError("Uploaded file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise PhotoValidationError(
            f"File is too large ({size // 1024} KB). "
            f"Maximum is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    # Pillow reads magic bytes when it opens — this is the real "is it an image?" check
    raw = file_storage.read()
    file_storage.stream.seek(0)

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()                # validates header without decoding pixels
    except Exception:
        raise PhotoValidationError(
            "This file is not a valid image. "
            "It may be corrupted or have the wrong extension."
        )

    # verify() closes the image — re-open for actual work
    img = Image.open(io.BytesIO(raw))

    if img.format not in ALLOWED_MIME_FORMATS:
        raise PhotoValidationError(
            f"Image format {img.format} is not allowed. "
            f"Use JPG, PNG, or WEBP."
        )

    # Reject tiny or absurdly huge images
    w, h = img.size
    if w < MIN_DIMENSION or h < MIN_DIMENSION:
        raise PhotoValidationError(
            f"Image is too small ({w}×{h}px). "
            f"Minimum is {MIN_DIMENSION}×{MIN_DIMENSION}px."
        )
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        raise PhotoValidationError(
            f"Image is too large ({w}×{h}px). "
            f"Maximum is {MAX_DIMENSION}×{MAX_DIMENSION}px."
        )

    # Fix phone-camera EXIF rotation (sideways photos)
    img = ImageOps.exif_transpose(img)

    # Convert to RGB for consistent JPEG output
    if img.mode in ("RGBA", "P", "LA"):
        # Flatten transparency onto white
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    return img


def _save_photo_files(img, original_filename):
    """
    Save the full-size photo + a cropped thumbnail.

    Returns (full_filename, thumb_filename) — both UUID-suffixed
    to guarantee uniqueness and preserve the original extension.
    """
    ext = _validate_extension(original_filename)
    # Normalize extension to .jpg for the thumbnail (always RGB JPEG)
    full_name  = f"{uuid.uuid4().hex}{ext}"
    thumb_name = f"{os.path.splitext(full_name)[0]}_thumb.jpg"

    full_path  = os.path.join(UPLOAD_FOLDER, full_name)
    thumb_path = os.path.join(THUMBS_FOLDER, thumb_name)

    # Full-size — preserve original quality, no recompression artifacts
    save_kwargs = {}
    if ext in (".jpg", ".jpeg"):
        save_kwargs.update(quality=88, optimize=True)
    elif ext == ".webp":
        save_kwargs.update(quality=88)
    img.save(full_path, **save_kwargs)

    # Thumbnail — crop to square then resize for consistent grid
    thumb = ImageOps.fit(img, THUMB_SIZE, method=Image.LANCZOS)
    thumb.save(thumb_path, "JPEG", quality=82, optimize=True)

    return full_name, thumb_name


def _delete_photo_files(full_name, thumb_name):
    """Best-effort removal of photo + thumbnail. Never raises."""
    if full_name:
        p = os.path.join(UPLOAD_FOLDER, full_name)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    if thumb_name:
        p = os.path.join(THUMBS_FOLDER, thumb_name)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------
# ENSURE DATABASE + TABLES EXIST (needed on Render free tier)
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# ENSURE DATABASE + TABLES EXIST (needed on Render free tier)
# ---------------------------------------------------------------
# Multi-tenant foundation:
#   - `tenants` table holds one row per merchant (Royal Home = id 1)
#   - `inventory.tenant_id` scopes every product to its merchant
#   - Existing rows are backfilled to tenant_id = 1 (Royal Home)
#   - Safe to re-run on every startup; idempotent migrations only
# ---------------------------------------------------------------
def ensure_database_exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ---- 1. Create `inventory` (unchanged for new installs) ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT,
            brand TEXT,
            model TEXT,
            price REAL,
            category TEXT,
            condition TEXT,
            photo TEXT,
            status TEXT DEFAULT 'Available',
            sold_date TEXT
        )
    """)

    # ---- 2. Create `reservations` (unchanged) ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER,
            customer_name TEXT,
            phone TEXT,
            email TEXT,
            message TEXT,
            reservation_date TEXT
        )
    """)

    # ---- 3. NEW: `tenants` table ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            slug          TEXT    NOT NULL UNIQUE,
            name          TEXT    NOT NULL,
            business_type TEXT    NOT NULL DEFAULT 'appliance',
            is_demo       INTEGER NOT NULL DEFAULT 0,
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT    NOT NULL
        )
    """)

    # ---- 4. NEW: seed Royal Home as tenant #1 (idempotent) ----
    cursor.execute("""
        INSERT OR IGNORE INTO tenants
            (id, slug, name, business_type, is_demo, is_active, created_at)
        VALUES
            (1, 'royal-home', 'Royal Home', 'appliance', 0, 1, ?)
    """, (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",))

    # ---- 5. NEW: seed Business Demo as tenant #2 (idempotent) ----
    # This prepares for the future "demo tenant" that shares the same
    # schema but stays completely isolated from Royal Home data.
    cursor.execute("""
        INSERT OR IGNORE INTO tenants
            (id, slug, name, business_type, is_demo, is_active, created_at)
        VALUES
            (2, 'business-demo', 'Business Demo (Clothing)', 'clothing', 1, 1, ?)
    """, (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",))

    # ---- 6. NEW: add `tenant_id` column to `inventory` (idempotent) ----
    # SQLite doesn't support "ADD COLUMN IF NOT EXISTS", so we check first.
    cursor.execute("PRAGMA table_info(inventory)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if "tenant_id" not in existing_columns:
        # DEFAULT 1 means: every pre-existing row is automatically
        # assigned to Royal Home — no data loss, no manual backfill.
        cursor.execute("""
            ALTER TABLE inventory
            ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1
        """)

    # ---- 7. NEW: index on tenant_id for fast per-merchant queries ----
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_inventory_tenant_id
        ON inventory(tenant_id)
    """)

    # ---- 9. NEW: `item_photos` table (Phase 1 — multi-photo) ----
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS item_photos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER NOT NULL,
            tenant_id    INTEGER NOT NULL DEFAULT 1,
            filename     TEXT    NOT NULL,
            thumbnail    TEXT    NOT NULL,
            is_primary   INTEGER NOT NULL DEFAULT 0,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            uploaded_at  TEXT    NOT NULL,
            FOREIGN KEY (inventory_id) REFERENCES inventory(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_item_photos_inventory_id
        ON item_photos(inventory_id)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_item_photos_tenant_id
        ON item_photos(tenant_id)
    """)

    # ---- 10. Backfill: existing inventory.photo → one item_photos row ----
    # For every inventory row whose `photo` column already holds a
    # real filename (not 'no-image.png'), create a matching item_photos
    # row marked as primary. This way old photos appear in the gallery
    # immediately and the "primary photo" concept is unified.
    cursor.execute("""
        SELECT i.id, i.photo, i.tenant_id
        FROM inventory i
        WHERE i.photo IS NOT NULL
          AND i.photo != ''
          AND i.photo != 'no-image.png'
          AND NOT EXISTS (
              SELECT 1 FROM item_photos p WHERE p.inventory_id = i.id
          )
    """)
    backfill_rows = cursor.fetchall()

    for inv_id, photo_filename, tenant_id in backfill_rows:
        # Derive thumbnail name — matches our _save_photo_files convention
        base = os.path.splitext(photo_filename)[0]
        thumb_name = f"{base}_thumb.jpg"

        thumb_exists = os.path.exists(
            os.path.join(THUMBS_FOLDER, thumb_name)
        )

        # If no thumbnail exists (old upload pre-Phase-1), try to generate one
        if not thumb_exists:
            full_path = os.path.join(UPLOAD_FOLDER, photo_filename)
            if os.path.exists(full_path):
                try:
                    with Image.open(full_path) as src:
                        src = ImageOps.exif_transpose(src)
                        if src.mode != "RGB":
                            src = src.convert("RGB")
                        thumb = ImageOps.fit(src, THUMB_SIZE, method=Image.LANCZOS)
                        thumb.save(
                            os.path.join(THUMBS_FOLDER, thumb_name),
                            "JPEG", quality=82, optimize=True
                        )
                    thumb_exists = True
                except Exception:
                    # If generation fails, skip this row — it will still
                    # work in the main inventory view via inventory.photo.
                    continue

        if not thumb_exists:
            continue

        cursor.execute("""
            INSERT INTO item_photos
                (inventory_id, tenant_id, filename, thumbnail,
                 is_primary, sort_order, uploaded_at)
            VALUES (?, ?, ?, ?, 1, 0, ?)
        """, (
            inv_id,
            tenant_id,
            photo_filename,
            thumb_name,
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
        ))

    # ---- 8. Safety net: any NULL tenant_id → 1 ----
    # (Cannot happen with the NOT NULL DEFAULT 1 above, but this is
    # harmless and protects against manual DB edits.)
    cursor.execute("""
        UPDATE inventory
        SET tenant_id = 1
        WHERE tenant_id IS NULL
    """)

    conn.commit()
    conn.close()

ensure_database_exists()

# ---------------------------------------------------------------
# PUBLIC RESERVATION ENDPOINT (used by /business-demo page)
# Stores reservations in database/reservations.db
# ---------------------------------------------------------------
import sqlite3 as _sqlite3_res
import json as _json_res

RESERVATIONS_DB = Path("database/reservations.db")

def _ensure_reservations_db():
    RESERVATIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    with _sqlite3_res.connect(RESERVATIONS_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'business_demo'
            )
            """
        )
        conn.commit()

_ensure_reservations_db()

@app.route("/api/reserve", methods=["POST"])
@limiter.limit("3 per minute")
@csrf.exempt
def api_reserve():

    data = request.get_json(silent=True) or {}

    product = (data.get("product") or "").strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not product or not name or not email or not phone:
        return jsonify({
            "success": False,
            "error": "Please fill in all fields."
        }), 400

    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({
            "success": False,
            "error": "Please enter a valid email address."
        }), 400

    try:
        with _sqlite3_res.connect(RESERVATIONS_DB) as conn:
            conn.execute(
                """
                INSERT INTO reservations
                (product, name, email, phone, created_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product,
                    name,
                    email,
                    phone,
                    datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
                    "business_demo",
                ),
            )
            conn.commit()
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": "Could not save your reservation. Please try again."
        }), 500

    return jsonify({"success": True})

# ---------------------------------------------------------------
# LOGIN REQUIRED DECORATOR
# ---------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

# ---------------------------------------------------------------
# PHASE 1.5 — TENANT HELPERS
# ---------------------------------------------------------------
def current_tenant_id():
    """
    Return the tenant_id for the current session.
    - Logged-in admin → session["tenant_id"] (defaults to 1 = Royal Home)
    - Demo user → session["tenant_id"] = 2 (never touches DB anyway)
    - Anonymous visitor → 1 (public storefront = Royal Home)
    """
    return session.get("tenant_id", 1)


def current_tenant():
    """
    Return the full tenant row for the current session.
    Useful for rendering tenant name / business type in templates.
    """
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, slug, name, business_type, is_demo FROM tenants WHERE id = ?",
        (tid,),
    ).fetchone()
    conn.close()
    return row


def set_tenant(tid):
    """Set the current session's tenant. Also updates session.permanent."""
    session.permanent = True
    session["tenant_id"] = int(tid)

# ---------------------------------------------------------------
# MILESTONE 1 — BUSINESS TYPE CONFIG
# ---------------------------------------------------------------
# Each tenant has a `business_type` (from the `tenants` table).
# The config below controls how the dashboard and forms render
# per business type — labels, categories, dropdown options.
#
# To add a new vertical (toys, sporting goods, auto parts, etc.),
# add a new key to BUSINESS_TYPE_CONFIG. No template changes needed.

BUSINESS_TYPE_CONFIG = {
    "appliance": {
        "name": "Appliance",
        "upload_title": "Upload New Appliance",
        "save_title": "Save Appliance",
        "edit_title": "Edit Appliance",
        "category_label": "Category",
        "categories": ["Refrigerator", "Washer", "Dryer", "Oven"],
        "model_label": "Model",
        "model_type": "text",                 # free-text input
        "model_options": [],                  # unused for text
        "condition_label": "Condition",
        "condition_placeholder": "Describe condition, age, and any defects…",
    },
    "clothing": {
        "name": "Clothing",
        "upload_title": "Upload New Clothing",
        "save_title": "Save Clothing",
        "edit_title": "Edit Clothing",
        "category_label": "Category",
        "categories": [
            "Shirt", "Pants", "Dress", "Jacket", "Shoes",
            "Accessories", "Winter Coats", "Bags",
        ],
        "model_label": "Size",
        "model_type": "select",               # dropdown
        "model_options": ["S", "M", "L", "XL", "XXL"],
        "condition_label": "Materials Quality",
        "condition_placeholder": "Describe materials quality, fabric, and any imperfections…",
    },
}

# If a tenant has no business_type or an unknown one, fall back to appliance.
DEFAULT_BUSINESS_TYPE = "appliance"


def get_business_type_config(business_type):
    """
    Return the config dict for a given business type.
    Falls back to DEFAULT_BUSINESS_TYPE if the type is missing/unknown.
    """
    key = (business_type or "").strip().lower()
    if key in BUSINESS_TYPE_CONFIG:
        return BUSINESS_TYPE_CONFIG[key]
    return BUSINESS_TYPE_CONFIG[DEFAULT_BUSINESS_TYPE]

def current_business_type_config():
    """
    Return the business type config for the current session's tenant.
    Combines tenant lookup + business type config in one call.
    """
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT business_type FROM tenants WHERE id = ?", (tid,)
    ).fetchone()
    conn.close()

    business_type = row[0] if row else DEFAULT_BUSINESS_TYPE
    return get_business_type_config(business_type)

# ---------------------------------------------------------------
# PHASE 2 — FILTER / SORT / PAGINATION HELPERS
# ---------------------------------------------------------------
def _build_inventory_query(base_select, tid, args):
    """
    Build a WHERE clause and params list from the request args.

    Supported query params:
      q           — search across product_name, brand, model (substring, case-insensitive)
      category    — exact match
      status      — 'Available' or 'Sold'
      min_price   — float, inclusive
      max_price   — float, inclusive
      sort        — column key (see VALID_SORTS below)
      dir         — 'asc' or 'desc'

    Returns (where_sql, params) where where_sql is e.g.
      " WHERE tenant_id = ? AND status = ? ORDER BY price ASC"
    """
    where_clauses = ["tenant_id = ?"]
    params = [tid]

    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where_clauses.append(
            "(LOWER(product_name) LIKE LOWER(?) "
            "OR LOWER(brand) LIKE LOWER(?) "
            "OR LOWER(model) LIKE LOWER(?))"
        )
        params.extend([like, like, like])

    category = (args.get("category") or "").strip()
    if category:
        where_clauses.append("category = ?")
        params.append(category)

    status = (args.get("status") or "").strip()
    if status in ("Available", "Sold"):
        where_clauses.append("status = ?")
        params.append(status)

    min_price = (args.get("min_price") or "").strip()
    if min_price:
        try:
            where_clauses.append("price >= ?")
            params.append(float(min_price))
        except ValueError:
            pass

    max_price = (args.get("max_price") or "").strip()
    if max_price:
        try:
            where_clauses.append("price <= ?")
            params.append(float(max_price))
        except ValueError:
            pass

    VALID_SORTS = {
        "name":  "product_name",
        "brand": "brand",
        "model": "model",
        "price": "price",
        "category": "category",
        "status": "status",
        "id": "id",
    }
    sort_key = VALID_SORTS.get((args.get("sort") or "id").lower(), "id")
    direction = "DESC" if (args.get("dir") or "desc").lower() == "desc" else "ASC"

    order_sql = f" ORDER BY {sort_key} {direction}"
    where_sql = " WHERE " + " AND ".join(where_clauses) + order_sql

    return where_sql, params


def _parse_pagination(args, default_per_page=25):
    """
    Return (page, per_page, offset) from query params.
    page is clamped to >= 1. per_page is clamped to one of [10, 25, 50, 100].
    """
    try:
        per_page = int(args.get("per_page", default_per_page))
    except (TypeError, ValueError):
        per_page = default_per_page
    if per_page not in (10, 25, 50, 100):
        per_page = default_per_page

    try:
        page = int(args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    return page, per_page, offset


# ---------------------------------------------------------------
# PHASE 1.5 — TENANT SWITCHER (admin-only, for testing isolation)
# ---------------------------------------------------------------
@app.route("/admin/switch-tenant/<int:tid>", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def admin_switch_tenant(tid):
    conn = sqlite3.connect(DB_PATH)
    exists = conn.execute(
        "SELECT id FROM tenants WHERE id = ? AND is_active = 1", (tid,)
    ).fetchone()
    conn.close()

    if not exists:
        flash(f"Tenant {tid} does not exist or is inactive.")
        return redirect("/dashboard")

    set_tenant(tid)
    flash(f"Now viewing as tenant {tid} ({current_tenant()[2]}).")
    return redirect("/dashboard")


# ---------------------------------------------------------------
# PUBLIC ROUTES
# ---------------------------------------------------------------
@app.route("/")
def home():
    # Decision 2 = A: / always shows tenant 1 (Royal Home) storefront
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Available'
          AND tenant_id = 1
        ORDER BY id DESC
        LIMIT 12
    """)
    inventory = cursor.fetchall()
    conn.close()
    return render_template("index.html", inventory=inventory)


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if request.method == "POST":
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if (
            username == ADMIN_USER
            and check_password_hash(ADMIN_PASS_HASH, password)
        ):
            session["logged_in"] = True
            set_tenant(1)                          # ← NEW: always tenant 1
            if is_ajax:
                return jsonify({"success": True, "redirect": "/dashboard"})
            return redirect("/dashboard")

        if is_ajax:
            return jsonify({
                "success": False,
                "error": "Invalid username or password."
            }), 401

        return render_template(
            "login.html",
            error="Invalid username or password."
        )

    return render_template("login.html")


@app.route("/reserve_appliance/<int:item_id>")
def reserve(item_id):
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM inventory WHERE id=? AND tenant_id=1",
        (item_id,),
    )
    item = cursor.fetchone()

    conn.close()

    if not item:
        return redirect("/")

    return render_template("reserve_appliance.html", item=item)


@app.route("/reserve_appliance/submit-reservation/<int:item_id>", methods=["POST"])
def submit_reservation(item_id):

    customer_name = request.form["customer_name"]
    phone = request.form["phone"]
    email = request.form.get("email", "")
    message = request.form.get("message", "")

    # Service options
    wants_delivery = "delivery" in request.form
    wants_installation = "installation" in request.form
    wants_haul_away = "haul_away" in request.form

    service_lines = []
    if wants_delivery:
        service_lines.append("Delivery")
    if wants_installation:
        service_lines.append("Installation")
    if wants_haul_away:
        service_lines.append("Haul away old appliance")

    selected_services = ", ".join(service_lines) if service_lines else "Pickup only (no extra services)"

    # Append services to the message so the manager sees it
    full_message = (
        f"SERVICES REQUESTED: {selected_services}\n\n"
        f"CUSTOMER MESSAGE:\n{message or '(none)'}"
    )

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Look up item name for the success page
    cursor.execute(
        "SELECT product_name FROM inventory WHERE id = ?",
        (item_id,)
    )
    row = cursor.fetchone()
    item_name = row[0] if row else "this appliance"

    cursor.execute(
        """
        INSERT INTO reservations
        (
            inventory_id,
            customer_name,
            phone,
            email,
            message,
            reservation_date
        )
        VALUES
        (?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            customer_name,
            phone,
            email,
            full_message,
            datetime.now().strftime("%Y-%m-%d %H:%M")
        )
    )

    conn.commit()
    conn.close()

    return render_template(
        "reservation_success.html",
        customer_name=customer_name,
        phone=phone,
        item_name=item_name,
        services=selected_services
    )

# ---------------------------------------------------------------
# MANAGER ROUTES (all protected)
# ---------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    tid = current_tenant_id()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Available' AND tenant_id = ?
        ORDER BY id DESC
    """, (tid,))
    inventory = cursor.fetchall()

    cursor.execute(
        "SELECT COUNT(*) FROM inventory WHERE status='Available' AND tenant_id = ?",
        (tid,),
    )
    available_count = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM inventory WHERE status='Sold' AND tenant_id = ?",
        (tid,),
    )
    sold_count = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM inventory WHERE tenant_id = ?", (tid,)
    )
    total_count = cursor.fetchone()[0]

    cursor.execute(
        "SELECT id, slug, name, is_demo FROM tenants WHERE is_active = 1 ORDER BY id"
    )
    tenants = cursor.fetchall()

    conn.close()

    tenant_name = current_tenant()[2]
    bt = current_business_type_config()           # ← NEW

    return render_template(
        "dashboard.html",
        inventory=inventory,
        available_count=available_count,
        sold_count=sold_count,
        total_count=total_count,
        tenants=tenants,
        current_tid=tid,
        allow_tenant_switching=ALLOW_TENANT_SWITCHING,
        tenant_name=tenant_name,
        bt=bt,                                     # ← NEW
    )


@app.route("/upload", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def upload():
    tid = current_tenant_id()
    bt = current_business_type_config()

    product_name = request.form.get("product_name", "").strip()
    brand        = request.form.get("brand", "").strip()
    model        = request.form.get("model", "").strip()
    price_raw    = request.form.get("price", "").strip()
    category     = request.form.get("category", "").strip()
    condition    = request.form.get("condition", "").strip()

    if not product_name:
        flash("Product name is required.")
        return redirect("/dashboard")

    try:
        price = float(price_raw) if price_raw else 0.0
    except ValueError:
        flash("Price must be a number.")
        return redirect("/dashboard")

    # ---- Validate category against the tenant's allowed list ----
    if category not in bt["categories"]:
        flash(
            f"Invalid category. Please choose one of: "
            f"{', '.join(bt['categories'])}."
        )
        return redirect("/dashboard")

    # ---- Validate model per business type ----
    if bt["model_type"] == "select":
        if model not in bt["model_options"]:
            flash(
                f"Invalid {bt['model_label'].lower()}. Please choose one of: "
                f"{', '.join(bt['model_options'])}."
            )
            return redirect("/dashboard")

    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("At least one photo is required.")
        return redirect("/dashboard")
    if len(files) > MAX_PHOTOS_PER_ITEM:
        flash(f"Maximum {MAX_PHOTOS_PER_ITEM} photos per item.")
        return redirect("/dashboard")

    validated = []
    try:
        for f in files:
            img = _open_and_validate_image(f)
            validated.append((f.filename, img))
    except PhotoValidationError as exc:
        flash(str(exc))
        return redirect("/dashboard")

    saved_photos = []
    try:
        for orig_name, img in validated:
            full_name, thumb_name = _save_photo_files(img, orig_name)
            saved_photos.append((full_name, thumb_name))
    except Exception:
        for full_name, thumb_name in saved_photos:
            _delete_photo_files(full_name, thumb_name)
        flash("Could not save photo. Please try again.")
        return redirect("/dashboard")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        primary_filename = saved_photos[0][0]

        cursor.execute("""
            INSERT INTO inventory
            (product_name, brand, model, price, category, condition, photo, tenant_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (product_name, brand, model, price, category, condition,
              primary_filename, tid))

        inv_id = cursor.lastrowid

        for idx, (full_name, thumb_name) in enumerate(saved_photos):
            cursor.execute("""
                INSERT INTO item_photos
                (inventory_id, tenant_id, filename, thumbnail,
                 is_primary, sort_order, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                inv_id, tid, full_name, thumb_name,
                1 if idx == 0 else 0, idx,
                datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
            ))

        conn.commit()
        conn.close()
    except Exception:
        for full_name, thumb_name in saved_photos:
            _delete_photo_files(full_name, thumb_name)
        flash("Could not save product. Please try again.")
        return redirect("/dashboard")

    flash(f"Product added with {len(saved_photos)} photo(s).")
    return redirect("/dashboard")

# ---------------------------------------------------------------
# DELETE — now by item id, not filename
# ---------------------------------------------------------------
@app.route("/delete/<int:item_id>", methods=["POST"])
@login_required
def delete_file(item_id):
    tid = current_tenant_id()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT photo FROM inventory WHERE id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    row = cursor.fetchone()

    if row is None:
        conn.close()
        flash("Item not found.")
        return redirect("/dashboard")

    cursor.execute(
        "DELETE FROM item_photos WHERE inventory_id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    cursor.execute(
        "DELETE FROM inventory WHERE id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    conn.commit()
    conn.close()

    flash("Product deleted.")
    return redirect("/dashboard")


@app.route("/edit_inventory/<int:item_id>")
@login_required
def edit(item_id):
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM inventory WHERE id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    item = cursor.fetchone()

    if not item:
        conn.close()
        flash("Item not found.")
        return redirect("/dashboard")

    cursor.execute("""
        SELECT id, filename, thumbnail, is_primary, sort_order
        FROM item_photos
        WHERE inventory_id = ? AND tenant_id = ?
        ORDER BY is_primary DESC, sort_order ASC, id ASC
    """, (item_id, tid))
    photos = cursor.fetchall()

    conn.close()

    bt = current_business_type_config()           # ← NEW

    return render_template(
        "edit_inventory.html",
        item=item,
        photos=photos,
        max_photos=MAX_PHOTOS_PER_ITEM,
        bt=bt,                                     # ← NEW
    )


@app.route("/update/<int:item_id>", methods=["POST"])
@login_required
def update(item_id):
    tid = current_tenant_id()
    bt = current_business_type_config()

    product_name = request.form.get("product_name", "").strip()
    brand        = request.form.get("brand", "").strip()
    model        = request.form.get("model", "").strip()
    price_raw    = request.form.get("price", "").strip()
    category     = request.form.get("category", "").strip()
    condition    = request.form.get("condition", "").strip()

    try:
        price = float(price_raw) if price_raw else 0.0
    except ValueError:
        flash("Price must be a number.")
        return redirect(f"/edit_inventory/{item_id}")

    # ---- Validate category against tenant's allowed list ----
    if category not in bt["categories"]:
        flash(
            f"Invalid category. Please choose one of: "
            f"{', '.join(bt['categories'])}."
        )
        return redirect(f"/edit_inventory/{item_id}")

    # ---- Validate model per business type ----
    if bt["model_type"] == "select":
        if model not in bt["model_options"]:
            flash(
                f"Invalid {bt['model_label'].lower()}. Please choose one of: "
                f"{', '.join(bt['model_options'])}."
            )
            return redirect(f"/edit_inventory/{item_id}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE inventory
        SET product_name=?, brand=?, model=?, price=?, category=?, condition=?
        WHERE id=? AND tenant_id=?
    """, (product_name, brand, model, price, category, condition, item_id, tid))

    conn.commit()
    conn.close()

    flash(f"{bt['name']} updated.")
    return redirect("/dashboard")


@app.route("/inventory-list")
@login_required
def inventory_list():
    tid = current_tenant_id()

    page, per_page, offset = _parse_pagination(request.args)
    where_sql, params = _build_inventory_query(
        "SELECT * FROM inventory", tid, request.args
    )

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ---- Total count (for pagination math + "X items" display) ----
    count_sql = "SELECT COUNT(*) FROM inventory" + where_sql.split(" ORDER BY ")[0]
    cursor.execute(count_sql, params)
    total_items = cursor.fetchone()[0]

    # ---- Total price (across ALL matching rows, not just the page) ----
    sum_sql = "SELECT COALESCE(SUM(price), 0) FROM inventory" + where_sql.split(" ORDER BY ")[0]
    cursor.execute(sum_sql, params)
    total_price = cursor.fetchone()[0]

    # ---- Page of rows ----
    page_sql = "SELECT * FROM inventory" + where_sql + " LIMIT ? OFFSET ?"
    cursor.execute(page_sql, params + [per_page, offset])
    inventory = cursor.fetchall()

    # ---- Category list (for the filter dropdown) ----
    cursor.execute(
        "SELECT DISTINCT category FROM inventory "
        "WHERE tenant_id = ? AND category IS NOT NULL AND category != '' "
        "ORDER BY category",
        (tid,),
    )
    categories = [row[0] for row in cursor.fetchall()]

    conn.close()

    total_pages = max(1, (total_items + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages

    # Compute page-number window: show up to 5 numbers around the current page
    page_numbers = []
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)
    for p in range(start_page, end_page + 1):
        page_numbers.append(p)

    return render_template(
        "inventory_list.html",
        inventory=inventory,
        total_items=total_items,
        total_price=total_price,
        categories=categories,
        # filter/sort state (so the template can render the form and preserve it)
        filters=request.args.to_dict(),
        current_page=page,
        per_page=per_page,
        total_pages=total_pages,
        page_numbers=page_numbers,
        has_prev=(page > 1),
        has_next=(page < total_pages),
        tenant_name=current_tenant()[2],
        allow_tenant_switching=ALLOW_TENANT_SWITCHING,
        tenants=[],
        current_tid=tid,
    )


@app.route("/inventory-list/bulk", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def inventory_list_bulk():
    """
    Handle bulk actions from the inventory list.

    Form fields:
      bulk_action — "mark-sold" or "delete"
      item_ids    — one or more inventory IDs (repeated field)

    Only acts on items belonging to the current tenant.
    """
    tid = current_tenant_id()
    action = (request.form.get("bulk_action") or "").strip()
    raw_ids = request.form.getlist("item_ids")

    # ---- Validate action ----
    if action not in ("mark-sold", "delete"):
        flash("Unknown bulk action.")
        return redirect("/inventory-list")

    # ---- Parse IDs (must be integers) ----
    item_ids = []
    for raw in raw_ids:
        try:
            item_ids.append(int(raw))
        except ValueError:
            continue

    if not item_ids:
        flash("No items selected.")
        return redirect("/inventory-list")

    # ---- Build safe placeholders for the IN clause ----
    placeholders = ",".join("?" * len(item_ids))

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # ---- Only operate on rows in the current tenant ----
    cursor.execute(
        f"SELECT id FROM inventory WHERE tenant_id = ? AND id IN ({placeholders})",
        (tid, *item_ids),
    )
    valid_ids = [row[0] for row in cursor.fetchall()]

    if not valid_ids:
        conn.close()
        flash("No valid items found for your tenant.")
        return redirect("/inventory-list")

    valid_placeholders = ",".join("?" * len(valid_ids))

    try:
        if action == "mark-sold":
            cursor.execute(
                f"""
                UPDATE inventory
                SET status = 'Sold', sold_date = ?
                WHERE tenant_id = ? AND id IN ({valid_placeholders})
                """,
                (datetime.now().strftime("%Y-%m-%d"), tid, *valid_ids),
            )
            changed = cursor.rowcount

            # Move photos' "ownership" continues — nothing else needed
            conn.commit()
            flash(f"Marked {changed} item(s) as Sold.")

        elif action == "delete":
            # Delete item_photos first (no cascade in SQLite by default)
            cursor.execute(
                f"DELETE FROM item_photos WHERE tenant_id = ? AND inventory_id IN ({valid_placeholders})",
                (tid, *valid_ids),
            )

            cursor.execute(
                f"DELETE FROM inventory WHERE tenant_id = ? AND id IN ({valid_placeholders})",
                (tid, *valid_ids),
            )
            changed = cursor.rowcount
            conn.commit()

            flash(f"Deleted {changed} item(s).")

    except Exception as exc:
        conn.rollback()
        conn.close()
        flash(f"Bulk action failed: {exc}")
        return redirect("/inventory-list")

    conn.close()

    # ---- Preserve current filters so the user returns to their view ----
    return_to = request.form.get("return_to") or "/inventory-list"
    if not return_to.startswith("/inventory-list"):
        return_to = "/inventory-list"
    return redirect(return_to)


@app.route("/inventory-list-print")
@login_required
def inventory_list_print():
    tid = current_tenant_id()

    where_sql, params = _build_inventory_query(
        "SELECT * FROM inventory", tid, request.args
    )

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM inventory" + where_sql, params)
    inventory = cursor.fetchall()
    conn.close()

    return render_template(
        "inventory_list_print.html",
        inventory=inventory,
        filters=request.args.to_dict(),
        tenant_name=current_tenant()[2],
    )


@app.route("/export-excel")
@login_required
def export_excel():
    tid = current_tenant_id()

    where_sql, params = _build_inventory_query(
        "SELECT * FROM inventory", tid, request.args
    )
    # Export doesn't need ORDER BY or LIMIT — strip them
    where_no_order = where_sql.split(" ORDER BY ")[0]

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT product_name, brand, model, price, category, condition, status "
        "FROM inventory" + where_no_order,
        conn,
        params=params,
    )
    conn.close()

    from io import BytesIO
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)

    # Filename reflects filter state so downloads are distinguishable
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    if request.args:
        filename = f"inventory_filtered_{ts}.xlsx"
    else:
        filename = f"inventory_export_{ts}.xlsx"

    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

# ---------------------------------------------------------------
# IMPORT EXCEL — now with UPSERT
# Natural key: product_name + model (case-insensitive)
# ---------------------------------------------------------------
@app.route("/import-excel", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def import_excel():
    tid = current_tenant_id()
    file = request.files.get("excel_file")

    if not file or file.filename == "":
        return redirect("/dashboard")

    df = pd.read_excel(file)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    inserted = 0
    updated = 0

    for _, row in df.iterrows():
        product_name = str(row.get("product_name", "")).strip()
        model = str(row.get("model", "")).strip()

        if not product_name:
            continue

        cursor.execute("""
            SELECT id FROM inventory
            WHERE LOWER(product_name) = LOWER(?)
              AND LOWER(model) = LOWER(?)
              AND tenant_id = ?
        """, (product_name, model, tid))

        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE inventory
                SET brand=?, price=?, category=?, condition=?
                WHERE id=? AND tenant_id=?
            """, (
                row.get("brand", ""),
                row.get("price", ""),
                row.get("category", ""),
                row.get("condition", ""),
                existing[0],
                tid,
            ))
            updated += 1
        else:
            cursor.execute("""
                INSERT INTO inventory
                (product_name, brand, model, price, category, condition, photo, status, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                product_name,
                row.get("brand", ""),
                model,
                row.get("price", ""),
                row.get("category", ""),
                row.get("condition", ""),
                "no-image.png",
                "Available",
                tid,
            ))
            inserted += 1

    conn.commit()
    conn.close()

    flash(f"Imported: {inserted} new, {updated} updated.")
    return redirect("/inventory-list")


# These three were restored
@app.route("/mark-sold/<int:item_id>", methods=["POST"])
@login_required
def mark_sold(item_id):
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE inventory
        SET status='Sold', sold_date=?
        WHERE id=? AND tenant_id=?
    """, (datetime.now().strftime("%Y-%m-%d"), item_id, tid))
    conn.commit()
    conn.close()
    return redirect("/dashboard")


@app.route("/sold-inventory")
@login_required
def sold_inventory():
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Sold' AND tenant_id = ?
        ORDER BY sold_date DESC
    """, (tid,))
    inventory = cursor.fetchall()
    conn.close()
    return render_template("sold_inventory.html", inventory=inventory)


@app.route("/restore/<int:item_id>", methods=["POST"])
@login_required
def restore(item_id):
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE inventory
        SET status='Available', sold_date=NULL
        WHERE id=? AND tenant_id=?
    """, (item_id, tid))
    conn.commit()
    conn.close()
    return redirect("/sold-inventory")

# ---------------------------------------------------------------
# UPLOAD PHOTO for rows that had "no-image.png"
# ---------------------------------------------------------------
@app.route("/upload-photo/<int:item_id>")
@login_required
def upload_photo_page(item_id):
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM inventory WHERE id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    item = cursor.fetchone()
    conn.close()

    if not item:
        flash("Item not found.")
        return redirect("/dashboard")

    return render_template("upload_photo.html", item=item)


@app.route("/upload-photo/<int:item_id>", methods=["POST"])
@login_required
def upload_photo_submit(item_id):
    tid = current_tenant_id()

    file = request.files.get("photo")
    if not file or file.filename == "":
        return redirect("/dashboard")

    try:
        img = _open_and_validate_image(file)
        full_name, thumb_name = _save_photo_files(img, file.filename)
    except PhotoValidationError as exc:
        flash(str(exc))
        return redirect(f"/upload-photo/{item_id}")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Confirm item belongs to this tenant
    cursor.execute(
        "SELECT id FROM inventory WHERE id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    if not cursor.fetchone():
        conn.close()
        _delete_photo_files(full_name, thumb_name)
        flash("Item not found.")
        return redirect("/dashboard")

    cursor.execute(
        "UPDATE inventory SET photo = ? WHERE id = ? AND tenant_id = ?",
        (full_name, item_id, tid),
    )

    cursor.execute(
        "SELECT MAX(sort_order) FROM item_photos WHERE inventory_id = ?",
        (item_id,),
    )
    next_order = (cursor.fetchone()[0] or 0) + 1

    cursor.execute("""
        INSERT INTO item_photos
        (inventory_id, tenant_id, filename, thumbnail,
         is_primary, sort_order, uploaded_at)
        VALUES (?, ?, ?, ?, 0, ?, ?)
    """, (
        item_id, tid, full_name, thumb_name, next_order,
        datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
    ))

    conn.commit()
    conn.close()

    flash("Photo uploaded.")
    return redirect("/dashboard")

# ---------------------------------------------------------------
# PHASE 1 — PHOTO MANAGEMENT ROUTES
# ---------------------------------------------------------------
@app.route("/photo/<int:photo_id>/delete", methods=["POST"])
@login_required
def photo_delete(photo_id):
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT inventory_id, filename, thumbnail, is_primary
        FROM item_photos WHERE id = ? AND tenant_id = ?
    """, (photo_id, tid))
    row = cursor.fetchone()

    if not row:
        conn.close()
        flash("Photo not found.")
        return redirect("/dashboard")

    inv_id, filename, thumbnail, was_primary = row

    cursor.execute(
        "SELECT COUNT(*) FROM item_photos WHERE inventory_id = ? AND tenant_id = ?",
        (inv_id, tid),
    )
    remaining = cursor.fetchone()[0]

    if remaining <= 1:
        conn.close()
        flash("Cannot delete the last photo. Upload a new one first.")
        return redirect(f"/edit_inventory/{inv_id}")

    cursor.execute(
        "DELETE FROM item_photos WHERE id = ? AND tenant_id = ?",
        (photo_id, tid),
    )

    if was_primary:
        cursor.execute("""
            SELECT id, filename FROM item_photos
            WHERE inventory_id = ? AND tenant_id = ?
            ORDER BY sort_order ASC, id ASC
            LIMIT 1
        """, (inv_id, tid))
        new_primary = cursor.fetchone()
        if new_primary:
            cursor.execute(
                "UPDATE item_photos SET is_primary = 1 WHERE id = ? AND tenant_id = ?",
                (new_primary[0], tid),
            )
            cursor.execute(
                "UPDATE inventory SET photo = ? WHERE id = ? AND tenant_id = ?",
                (new_primary[1], inv_id, tid),
            )

    conn.commit()
    conn.close()
    _delete_photo_files(filename, thumbnail)

    flash("Photo deleted.")
    return redirect(f"/edit_inventory/{inv_id}")


@app.route("/photo/<int:photo_id>/primary", methods=["POST"])
@login_required
def photo_set_primary(photo_id):
    tid = current_tenant_id()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT inventory_id, filename FROM item_photos WHERE id = ? AND tenant_id = ?",
        (photo_id, tid),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        flash("Photo not found.")
        return redirect("/dashboard")

    inv_id, filename = row

    cursor.execute(
        "UPDATE item_photos SET is_primary = 0 WHERE inventory_id = ? AND tenant_id = ?",
        (inv_id, tid),
    )
    cursor.execute(
        "UPDATE item_photos SET is_primary = 1 WHERE id = ? AND tenant_id = ?",
        (photo_id, tid),
    )
    cursor.execute(
        "UPDATE inventory SET photo = ? WHERE id = ? AND tenant_id = ?",
        (filename, inv_id, tid),
    )

    conn.commit()
    conn.close()

    flash("Primary photo updated.")
    return redirect(f"/edit_inventory/{inv_id}")


@app.route("/photo/upload/<int:item_id>", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def photo_add_to_item(item_id):
    tid = current_tenant_id()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM item_photos WHERE inventory_id = ? AND tenant_id = ?",
        (item_id, tid),
    )
    existing_count = cursor.fetchone()[0]
    conn.close()

    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        flash("No files selected.")
        return redirect(f"/edit_inventory/{item_id}")

    if existing_count + len(files) > MAX_PHOTOS_PER_ITEM:
        flash(f"Maximum {MAX_PHOTOS_PER_ITEM} photos per item.")
        return redirect(f"/edit_inventory/{item_id}")

    validated = []
    try:
        for f in files:
            img = _open_and_validate_image(f)
            validated.append((f.filename, img))
    except PhotoValidationError as exc:
        flash(str(exc))
        return redirect(f"/edit_inventory/{item_id}")

    saved = []
    try:
        for orig_name, img in validated:
            full_name, thumb_name = _save_photo_files(img, orig_name)
            saved.append((full_name, thumb_name))
    except Exception:
        for full_name, thumb_name in saved:
            _delete_photo_files(full_name, thumb_name)
        flash("Could not save photo. Please try again.")
        return redirect(f"/edit_inventory/{item_id}")

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT MAX(sort_order) FROM item_photos WHERE inventory_id = ?",
            (item_id,),
        )
        max_order = cursor.fetchone()[0] or 0

        cursor.execute(
            "SELECT COUNT(*) FROM item_photos WHERE inventory_id = ? AND is_primary = 1",
            (item_id,),
        )
        has_primary = cursor.fetchone()[0] > 0

        for idx, (full_name, thumb_name) in enumerate(saved):
            cursor.execute("""
                INSERT INTO item_photos
                (inventory_id, tenant_id, filename, thumbnail,
                 is_primary, sort_order, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                item_id, tid, full_name, thumb_name,
                0 if has_primary else (1 if idx == 0 else 0),
                max_order + idx + 1,
                datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z",
            ))

            if not has_primary and idx == 0:
                cursor.execute(
                    "UPDATE inventory SET photo = ? WHERE id = ? AND tenant_id = ?",
                    (full_name, item_id, tid),
                )

        conn.commit()
        conn.close()
    except Exception:
        for full_name, thumb_name in saved:
            _delete_photo_files(full_name, thumb_name)
        flash("Could not save photos. Please try again.")
        return redirect(f"/edit_inventory/{item_id}")

    flash(f"{len(saved)} photo(s) added.")
    return redirect(f"/edit_inventory/{item_id}")



@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/.well-known/appspecific/com.chrome.devtools.json")
def _chrome_devtools():
    return "", 204

@app.route("/business-demo")
def business_demo():
    return render_template(
        "business_demo.html"
    )

@app.route("/demo-login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def demo_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if (
            DEMO_USER
            and DEMO_PASS
            and username == DEMO_USER
            and password == DEMO_PASS
        ):
            session.permanent = True
            session["demo_logged_in"] = True
            set_tenant(2)                          # ← NEW: always tenant 2
            return redirect("/demo-dashboard")

        return render_template(
            "demo_login.html",
            error="Invalid username or password.",
            demo_user=DEMO_USER,
            demo_pass=DEMO_PASS,
        )

    return render_template(
        "demo_login.html",
        demo_user=DEMO_USER,
        demo_pass=DEMO_PASS,
    )

@app.route("/demo-dashboard")
def demo_dashboard():

    if not session.get("demo_logged_in"):

        return redirect("/business-demo")

    return render_template(
        "demo_dashboard.html"
    )

@app.route("/demo-logout")
def demo_logout():
    session.pop("demo_logged_in", None)
    # If an admin is logged in, restore their tenant context
    if session.get("logged_in"):
        set_tenant(1)
    else:
        session.pop("tenant_id", None)
    return redirect("/business-demo")

# ---------------------------------------------------------------
# PHASE 5 — CSRF ERROR HANDLER
# ---------------------------------------------------------------
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    flash("Your session expired or the form was tampered with. Please try again.")
    return redirect(request.referrer or "/")


# ---------------------------------------------------------------
# PHASE 5 — RATE LIMIT ERROR HANDLER
# ---------------------------------------------------------------
@app.errorhandler(429)
def ratelimit_handler(e):
    return (
        render_template("429.html") if os.path.exists("templates/429.html")
        else f"<h1>Too Many Requests</h1><p>Please slow down. Try again in a minute.</p>"
    ), 429


if __name__ == "__main__":
    # Debug mode only when FLASK_ENV is "development" AND FLASK_DEBUG is "1"
    debug_mode = (ENV == "development" and
                  os.environ.get("FLASK_DEBUG", "1") == "1")

    app.run(
        host="0.0.0.0" if IS_PRODUCTION else "127.0.0.1",
        port=int(os.environ.get("PORT", 5000)),
        debug=debug_mode,
    )