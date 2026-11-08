from flask import (Flask, render_template, request, redirect, send_file, session, flash)
from flask import jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from dotenv import load_dotenv
from functools import wraps
import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime
import pandas as pd
import sqlite3
import os

# ---------------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES FROM .env
# ---------------------------------------------------------------
load_dotenv()

SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH")

# Fail fast if any required env var is missing
if not SECRET_KEY or not ADMIN_USER or not ADMIN_PASS_HASH:
    raise RuntimeError(
        "Missing required environment variables. "
        "Check that your .env file contains SECRET_KEY, "
        "ADMIN_USER, and ADMIN_PASS_HASH."
    )


# ---------------------------------------------------------------
# APP CONFIG
# ---------------------------------------------------------------
DB_PATH = Path("database/inventory.db")

app = Flask(__name__)
app.secret_key = SECRET_KEY

UPLOAD_FOLDER = "static/images/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------------------------------------------------------------
# ENSURE DATABASE + TABLES EXIST (needed on Render free tier)
# ---------------------------------------------------------------
def ensure_database_exists():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

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

    conn.commit()
    conn.close()


ensure_database_exists()

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
# PUBLIC ROUTES
# ---------------------------------------------------------------
@app.route("/")
def home():

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Available'
        ORDER BY id DESC
        LIMIT 12
    """)

    inventory = cursor.fetchall()
    conn.close()

    return render_template("index.html", inventory=inventory)


@app.route("/login", methods=["GET", "POST"])
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

            if is_ajax:
                return jsonify({"success": True, "redirect": "/dashboard"})

            return redirect("/dashboard")

        # Wrong credentials
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


@app.route("/reserve/<int:item_id>")
def reserve(item_id):

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM inventory WHERE id=?", (item_id,))
    item = cursor.fetchone()

    conn.close()

    return render_template("reserve_appliance.html", item=item)


@app.route("/submit-reservation/<int:item_id>", methods=["POST"])
def submit_reservation(item_id):

    customer_name = request.form["customer_name"]
    phone = request.form["phone"]
    email = request.form.get("email", "")
    message = request.form.get("message", "")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT product_name FROM inventory WHERE id = ?",
        (item_id,)
    )
    row = cursor.fetchone()
    item_name = row[0] if row else "this appliance"

    cursor.execute(
        """
        INSERT INTO reservations
        (inventory_id, customer_name, phone, email, message, reservation_date)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            customer_name,
            phone,
            email,
            message,
            datetime.now().strftime("%Y-%m-%d %H:%M")
        )
    )

    conn.commit()
    conn.close()

    return render_template(
        "reservation_success.html",
        customer_name=customer_name,
        phone=phone,
        item_name=item_name
    )


# ---------------------------------------------------------------
# MANAGER ROUTES (all protected)
# ---------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Available'
        ORDER BY id DESC
    """)
    inventory = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM inventory WHERE status='Available'")
    available_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM inventory WHERE status='Sold'")
    sold_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM inventory")
    total_count = cursor.fetchone()[0]

    conn.close()

    return render_template(
        "dashboard.html",
        inventory=inventory,
        available_count=available_count,
        sold_count=sold_count,
        total_count=total_count
    )


@app.route("/upload", methods=["POST"])
@login_required
def upload():

    product_name = request.form["product_name"]
    brand = request.form["brand"]
    model = request.form["model"]
    price = request.form["price"]
    category = request.form["category"]
    condition = request.form["condition"]

    file = request.files.get("photo")

    if file and file.filename != "":

        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(filepath)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO inventory
            (product_name, brand, model, price, category, condition, photo)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (product_name, brand, model, price, category, condition, filename))

        conn.commit()
        conn.close()

    return redirect("/dashboard")


# ---------------------------------------------------------------
# DELETE — now by item id, not filename
# ---------------------------------------------------------------
@app.route("/delete/<int:item_id>", methods=["POST"])
@login_required
def delete_file(item_id):

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT photo FROM inventory WHERE id = ?", (item_id,))
    row = cursor.fetchone()

    if row is None:
        conn.close()
        return redirect("/dashboard")

    filename = row[0]

    cursor.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM inventory WHERE photo = ?", (filename,))
    still_used = cursor.fetchone()[0]

    conn.close()

    if still_used == 0 and filename and filename != "no-image.png":
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        if os.path.exists(filepath):
            os.remove(filepath)

    return redirect("/dashboard")


@app.route("/edit_inventory/<int:item_id>")
@login_required
def edit(item_id):

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM inventory WHERE id = ?", (item_id,))
    item = cursor.fetchone()

    conn.close()

    return render_template("edit_inventory.html", item=item)


@app.route("/update/<int:item_id>", methods=["POST"])
@login_required
def update(item_id):

    product_name = request.form["product_name"]
    brand = request.form["brand"]
    model = request.form["model"]
    price = request.form["price"]
    category = request.form["category"]
    condition = request.form["condition"]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE inventory
        SET product_name=?, brand=?, model=?, price=?, category=?, condition=?
        WHERE id=?
    """, (product_name, brand, model, price, category, condition, item_id))

    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/inventory-list")
@login_required
def inventory_list():

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM inventory ORDER BY product_name")
    inventory = cursor.fetchall()

    conn.close()

    total_items = len(inventory)
    total_price = 0.0
    for row in inventory:
        try:
            total_price += float(row[4])
        except (TypeError, ValueError):
            pass

    return render_template(
        "inventory_list.html",
        inventory=inventory,
        total_items=total_items,
        total_price=total_price
    )


@app.route("/inventory-list-print")
@login_required
def inventory_list_print():

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM inventory ORDER BY product_name")
    inventory = cursor.fetchall()

    conn.close()

    return render_template("inventory_list_print.html", inventory=inventory)


@app.route("/export-excel")
@login_required
def export_excel():

    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("""
        SELECT product_name, brand, model, price, category, condition
        FROM inventory
    """, conn)
    conn.close()

    # Write to a temp buffer instead of disk
    from io import BytesIO
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)

    filename = f"inventory_export_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ---------------------------------------------------------------
# IMPORT EXCEL — now with UPSERT
# Natural key: product_name + model (case-insensitive)
# ---------------------------------------------------------------
@app.route("/import-excel", methods=["POST"])
@login_required
def import_excel():

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
            continue  # skip empty rows

        # Does this item already exist?
        cursor.execute("""
            SELECT id FROM inventory
            WHERE LOWER(product_name) = LOWER(?)
            AND LOWER(model) = LOWER(?)
        """, (product_name, model))

        existing = cursor.fetchone()

        if existing:

            # UPDATE existing row (keep photo + status untouched)
            cursor.execute("""
                UPDATE inventory
                SET brand=?, price=?, category=?, condition=?
                WHERE id=?
            """, (
                row.get("brand", ""),
                row.get("price", ""),
                row.get("category", ""),
                row.get("condition", ""),
                existing[0]
            ))
            updated += 1

        else:

            cursor.execute("""
                INSERT INTO inventory
                (product_name, brand, model, price, category, condition, photo, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                product_name,
                row.get("brand", ""),
                model,
                row.get("price", ""),
                row.get("category", ""),
                row.get("condition", ""),
                "no-image.png",
                "Available"
            ))
            inserted += 1

    conn.commit()
    conn.close()

    flash(f"Imported: {inserted} new, {updated} updated.")
    return redirect("/inventory-list")


@app.route("/mark-sold/<int:item_id>", methods=["POST"])
@login_required
def mark_sold(item_id):

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE inventory
        SET status='Sold', sold_date=?
        WHERE id=?
    """, (datetime.now().strftime("%Y-%m-%d"), item_id))

    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/sold-inventory")
@login_required
def sold_inventory():

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Sold'
        ORDER BY sold_date DESC
    """)
    inventory = cursor.fetchall()

    conn.close()

    return render_template("sold_inventory.html", inventory=inventory)


@app.route("/restore/<int:item_id>", methods=["POST"])
@login_required
def restore(item_id):

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE inventory
        SET status='Available', sold_date=NULL
        WHERE id=?
    """, (item_id,))

    conn.commit()
    conn.close()

    return redirect("/sold-inventory")


# ---------------------------------------------------------------
# UPLOAD PHOTO for rows that had "no-image.png"
# ---------------------------------------------------------------
@app.route("/upload-photo/<int:item_id>")
@login_required
def upload_photo_page(item_id):

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM inventory WHERE id = ?", (item_id,))
    item = cursor.fetchone()

    conn.close()

    return render_template("upload_photo.html", item=item)


@app.route("/upload-photo/<int:item_id>", methods=["POST"])
@login_required
def upload_photo_submit(item_id):

    file = request.files.get("photo")

    if not file or file.filename == "":
        return redirect("/dashboard")

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE inventory SET photo = ? WHERE id = ?", (filename, item_id))
    conn.commit()
    conn.close()

    return redirect("/dashboard")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/.well-known/appspecific/com.chrome.devtools.json")
def _chrome_devtools():
    return "", 204

if __name__ == "__main__":
    app.run(debug=True)