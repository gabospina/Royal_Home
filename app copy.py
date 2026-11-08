from flask import (
    Flask,
    render_template,
    request,
    redirect,
    send_file,
    session
)
from werkzeug.utils import secure_filename
import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime
import os

DB_PATH = Path("database/inventory.db")

app = Flask(__name__)

app.secret_key = "RoyalHome2026"

UPLOAD_FOLDER = "static/images/uploads"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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

    return render_template(
        "index.html",
        inventory=inventory
    )

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]

        if (
            username == "admin"
            and
            password == "RoyalHome123"
        ):

            session["logged_in"] = True

            return redirect("/dashboard")

        return render_template(
            "login.html",
            error="Invalid username or password."
        )

    return render_template(
        "login.html"
    )

@app.route("/dashboard")
def dashboard():

    if not session.get("logged_in"):

        return redirect("/login")

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE status='Available'
        ORDER BY id DESC
    """)

    inventory = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*)
        FROM inventory
        WHERE status='Available'
    """)

    available_count = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)
        FROM inventory
        WHERE status='Sold'
    """)

    sold_count = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)
        FROM inventory
    """)

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
def upload():

    product_name = request.form["product_name"]
    brand = request.form["brand"]
    model = request.form["model"]
    price = request.form["price"]
    category = request.form["category"]
    condition = request.form["condition"]

    file = request.files["photo"]

    if file.filename != "":

        filename = secure_filename(file.filename)

        filepath = os.path.join(
            app.config["UPLOAD_FOLDER"],
            filename
        )

        file.save(filepath)

        conn = sqlite3.connect(DB_PATH)

        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO inventory
            (
                product_name,
                brand,
                model,
                price,
                category,
                condition,
                photo
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_name,
            brand,
            model,
            price,
            category,
            condition,
            filename
        ))

        conn.commit()
        conn.close()

    return redirect("/dashboard")


@app.route("/delete/<path:filename>", methods=["POST"])
def delete_file(filename):

    filename = secure_filename(filename)

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM inventory
        WHERE photo = ?
        """,
        (filename,)
    )

    conn.commit()
    conn.close()

    filepath = os.path.join(
        app.config["UPLOAD_FOLDER"],
        filename
    )

    if os.path.exists(filepath):
        os.remove(filepath)

    return redirect("/dashboard")

@app.route("/edit_inventory/<int:item_id>")
def edit(item_id):
    if not session.get("logged_in"):

        return redirect("/login")

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM inventory
        WHERE id = ?
        """,
        (item_id,)
    )

    item = cursor.fetchone()

    conn.close()

    return render_template(
        "edit_inventory.html",
        item=item
    )

@app.route("/update/<int:item_id>", methods=["POST"])
def update(item_id):

    product_name = request.form["product_name"]
    brand = request.form["brand"]
    model = request.form["model"]
    price = request.form["price"]
    category = request.form["category"]
    condition = request.form["condition"]

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE inventory
        SET
            product_name=?,
            brand=?,
            model=?,
            price=?,
            category=?,
            condition=?
        WHERE id=?
        """,
        (
            product_name,
            brand,
            model,
            price,
            category,
            condition,
            item_id
        )
    )

    conn.commit()
    conn.close()

    return redirect("/dashboard")

@app.route("/inventory-list")
def inventory_list():
    if not session.get("logged_in"):
        return redirect("/login")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT *
        FROM inventory
        ORDER BY product_name
    """)
    inventory = cursor.fetchall()
    conn.close()
    return render_template(
        "inventory_list.html",
        inventory=inventory
    )

@app.route("/inventory-list-print")
def inventory_list_print():

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        ORDER BY product_name
    """)

    inventory = cursor.fetchall()

    conn.close()

    return render_template(
        "inventory_list_print.html",
        inventory=inventory
    )

@app.route("/export-excel")
def export_excel():

    conn = sqlite3.connect(DB_PATH)

    query = """
        SELECT
            product_name,
            brand,
            model,
            price,
            category,
            condition
        FROM inventory
    """

    df = pd.read_sql_query(query, conn)

    conn.close()

    file_path = "inventory_export.xlsx"

    df.to_excel(
        file_path,
        index=False
    )

    return send_file(
        file_path,
        as_attachment=True
    )

@app.route(
    "/import-excel",
    methods=["POST"]
)
def import_excel():

    file = request.files["excel_file"]

    if file:

        df = pd.read_excel(file)

        conn = sqlite3.connect(DB_PATH)

        for _, row in df.iterrows():

            conn.execute(
                """
                INSERT INTO inventory
                (
                    product_name,
                    brand,
                    model,
                    price,
                    category,
                    condition,
                    photo,
                    status
                )
                VALUES
                (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("product_name", ""),
                    row.get("brand", ""),
                    row.get("model", ""),
                    row.get("price", ""),
                    row.get("category", ""),
                    row.get("condition", ""),
                    "no-image.png",
                    "Available"
                )
            )

        conn.commit()
        conn.close()

    return redirect("/dashboard")

@app.route(
    "/mark-sold/<int:item_id>",
    methods=["POST"]
)
def mark_sold(item_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE inventory
        SET
            status='Sold',
            sold_date=?
        WHERE id=?
        """,
        (
            datetime.now().strftime(
                "%Y-%m-%d"
            ),
            item_id
        )
    )
    conn.commit()
    conn.close()
    return redirect("/dashboard")

@app.route("/sold-inventory")
def sold_inventory():
    if not session.get("logged_in"):

        return redirect("/login")
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
    return render_template(
        "sold_inventory.html",
        inventory=inventory
    )

@app.route(
    "/restore/<int:item_id>",
    methods=["POST"]
)
def restore(item_id):

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE inventory
        SET
            status='Available',
            sold_date=NULL
        WHERE id=?
        """,
        (item_id,)
    )

    conn.commit()

    conn.close()

    return redirect("/sold-inventory")

@app.route("/reserve/<int:item_id>")
def reserve(item_id):

    conn = sqlite3.connect(DB_PATH)

    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM inventory
        WHERE id=?
    """, (item_id,))

    item = cursor.fetchone()

    conn.close()

    return render_template(
        "reserve_appliance.html",
        item=item
    )

@app.route(
    "/submit-reservation/<int:item_id>",
    methods=["POST"]
)
def submit_reservation(item_id):

    customer_name = request.form["customer_name"]
    phone = request.form["phone"]
    email = request.form.get("email", "")
    message = request.form.get("message", "")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Look up the item name for the success page
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

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)