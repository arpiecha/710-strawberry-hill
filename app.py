"""GC Tracker — multi-client receipt tracker.

One Flask service: JSON API plus the static dashboard and mobile upload page.
Adding a client is a form, not a deployment.
"""

import base64
import hmac
import logging
import os
from datetime import date, datetime

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS
from sqlalchemy import func, select

import storage
from claude_receipts import CATEGORIES, analyze_receipt
from db import Bill, Client, Receipt, SessionLocal, init_db, unique_slug

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
PORT = int(os.environ.get("PORT", "8080"))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = Flask(__name__, static_folder=None)
CORS(app, resources={r"/*": {"origins": "*"}}, allow_headers=["Content-Type", "X-Admin-Password"])


# --- auth ---------------------------------------------------------------

def password_ok() -> bool:
    """Accept the password from the header, or from ?key= for <img>/<a> links."""
    supplied = request.headers.get("X-Admin-Password") or request.args.get("key") or ""
    return hmac.compare_digest(supplied, ADMIN_PASSWORD)


def require_auth():
    """Return an error response if the request is not authorised, else None."""
    if not password_ok():
        return jsonify({"error": "Unauthorized"}), 401
    return None


# --- static pages -------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/add")
def add_page():
    return send_from_directory(STATIC_DIR, "add.html")


@app.route("/c/<slug>")
def client_page(slug: str):
    """Per-job dashboard URL. The page reads the slug out of the path."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/auth-check")
def auth_check():
    """Lets a page verify a password without fetching any data."""
    return jsonify({"ok": password_ok()})


# --- clients ------------------------------------------------------------

@app.route("/clients", methods=["GET"])
def list_clients():
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        counts = dict(
            session.execute(
                select(Receipt.client_id, func.count(Receipt.id)).group_by(Receipt.client_id)
            ).all()
        )
        clients = session.scalars(select(Client).order_by(Client.name)).all()
        return jsonify([c.to_dict(receipt_count=counts.get(c.id, 0)) for c in clients])


@app.route("/clients", methods=["POST"])
def create_client():
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    with SessionLocal() as session:
        client = Client(
            name=name,
            slug=unique_slug(session, name),
            address=(data.get("address") or "").strip() or None,
            notes=(data.get("notes") or "").strip() or None,
        )
        session.add(client)
        session.commit()
        logger.info("Created client %s (%s)", client.id, client.slug)
        return jsonify(client.to_dict(receipt_count=0)), 201


@app.route("/clients/<int:client_id>", methods=["GET"])
def get_client(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if client is None:
            return jsonify({"error": "Client not found"}), 404
        count = session.scalar(
            select(func.count(Receipt.id)).where(Receipt.client_id == client_id)
        )
        return jsonify(client.to_dict(receipt_count=count or 0))


@app.route("/clients/<int:client_id>", methods=["PATCH"])
def update_client(client_id: int):
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if client is None:
            return jsonify({"error": "Client not found"}), 404

        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                return jsonify({"error": "Name cannot be empty"}), 400
            if name != client.name:
                client.name = name
                client.slug = unique_slug(session, name, exclude_id=client.id)
        if "address" in data:
            client.address = (data.get("address") or "").strip() or None
        if "notes" in data:
            client.notes = (data.get("notes") or "").strip() or None
        if "start_date" in data:
            raw = (data.get("start_date") or "").strip()
            if not raw:
                client.start_date = None
            else:
                try:
                    client.start_date = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    return jsonify({"error": "Start date must be YYYY-MM-DD"}), 400

        session.commit()
        return jsonify(client.to_dict())


@app.route("/clients/<int:client_id>", methods=["DELETE"])
def delete_client(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        client = session.get(Client, client_id)
        if client is None:
            return jsonify({"error": "Client not found"}), 404
        session.delete(client)  # receipts cascade
        session.commit()
        logger.info("Deleted client %s", client_id)
        return jsonify({"success": True})


# --- receipts -----------------------------------------------------------

def first_word(text: str) -> str:
    parts = (text or "").strip().lower().split()
    return parts[0] if parts else ""


def find_duplicate(session, client_id: int, store: str, amount: float) -> Receipt | None:
    """Same client, same amount, same first word of the store name.

    Carried over from the original bot, which found that matching the whole
    store string was too strict (receipts render it inconsistently) and that
    the date was unreliable. Scoped per client here, and the amount is
    compared signed so a return never collides with a purchase.
    """
    word = first_word(store)
    if not word:
        return None
    candidates = session.scalars(
        select(Receipt).where(Receipt.client_id == client_id)
    ).all()
    for r in candidates:
        if abs(float(r.amount) - amount) < 0.01 and first_word(r.store) == word:
            return r
    return None


@app.route("/analyze", methods=["POST"])
def analyze_endpoint():
    if (err := require_auth()):
        return err
    if "photo" not in request.files:
        return jsonify({"error": "No photo provided"}), 400
    file = request.files["photo"]
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({"error": "Empty photo"}), 400
    mime_type = file.content_type or "image/jpeg"
    try:
        receipt = analyze_receipt(image_bytes, mime_type)
        return jsonify({"success": True, "receipt": receipt})
    except Exception as e:
        logger.exception("Analyze failed")
        return jsonify({"error": f"Could not read receipt: {e}"}), 500


@app.route("/save", methods=["POST"])
def save_endpoint():
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}

    try:
        client_id = int(data.get("client_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "client_id is required"}), 400

    store = (data.get("store") or "").strip()
    if not store:
        return jsonify({"error": "Store is required"}), 400

    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Amount must be a number"}), 400

    rtype = (data.get("type") or "purchase").strip().lower()
    if rtype not in ("purchase", "return"):
        rtype = "purchase"
    # Returns are stored negative so summing the column gives net spend.
    amount = -abs(amount) if rtype == "return" else abs(amount)

    category = (data.get("category") or "MISC").strip()
    if category not in CATEGORIES:
        category = "MISC"

    raw_date = (data.get("date") or "").strip()
    try:
        rdate = datetime.strptime(raw_date, "%Y-%m-%d").date() if raw_date else date.today()
    except ValueError:
        rdate = date.today()

    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404

        if not data.get("force"):
            dup = find_duplicate(session, client_id, store, amount)
            if dup is not None:
                return jsonify({
                    "success": False,
                    "duplicate": True,
                    "error": f"Looks like a duplicate — {dup.store} for ${abs(float(dup.amount)):.2f} "
                             f"is already logged on {dup.date.isoformat()}.",
                })

        receipt = Receipt(
            client_id=client_id,
            date=rdate,
            store=store,
            category=category,
            type=rtype,
            amount=amount,
            items=(data.get("items") or "").strip() or None,
            notes=(data.get("notes") or "").strip() or None,
            source=(data.get("source") or "dashboard").strip()[:32],
        )
        session.add(receipt)
        session.flush()  # need the id before naming the image file

        image_b64 = data.get("image_base64")
        if image_b64:
            try:
                receipt.image_path = storage.save_image(
                    client_id, receipt.id, base64.b64decode(image_b64)
                )
            except Exception:
                logger.exception("Could not save receipt image; saving the row anyway")

        session.commit()
        logger.info("Saved receipt %s for client %s", receipt.id, client_id)
        return jsonify({"success": True, "id": receipt.id, "receipt": receipt.to_dict()})


@app.route("/clients/<int:client_id>/receipts", methods=["GET"])
def list_receipts(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        receipts = session.scalars(
            select(Receipt)
            .where(Receipt.client_id == client_id)
            .order_by(Receipt.date.desc(), Receipt.id.desc())
        ).all()
        return jsonify([r.to_dict() for r in receipts])


@app.route("/receipts/<int:receipt_id>/image", methods=["GET"])
def receipt_image(receipt_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return jsonify({"error": "Receipt not found"}), 404
        if not receipt.image_path:
            return jsonify({"error": "No photo was saved for this receipt"}), 404
        image = storage.read_image(receipt.image_path)
        if image is None:
            return jsonify({"error": "Photo is missing from storage"}), 404
        return Response(image, mimetype="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.route("/receipts/<int:receipt_id>", methods=["DELETE"])
def delete_receipt(receipt_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        receipt = session.get(Receipt, receipt_id)
        if receipt is None:
            return jsonify({"error": "Receipt not found"}), 404
        storage.delete_image(receipt.image_path)
        session.delete(receipt)
        session.commit()
        logger.info("Deleted receipt %s", receipt_id)
        return jsonify({"success": True})


# --- bills --------------------------------------------------------------

@app.route("/clients/<int:client_id>/bills", methods=["GET"])
def list_bills(client_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        bills = session.scalars(
            select(Bill).where(Bill.client_id == client_id)
        ).all()
        return jsonify(sorted((b.to_dict() for b in bills), key=lambda b: b["days_away"]))


@app.route("/clients/<int:client_id>/bills", methods=["POST"])
def create_bill(client_id: int):
    if (err := require_auth()):
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    try:
        due_day = int(data.get("due_day"))
    except (TypeError, ValueError):
        return jsonify({"error": "Due day must be a number"}), 400
    if not 1 <= due_day <= 31:
        return jsonify({"error": "Due day must be between 1 and 31"}), 400

    amount = data.get("amount")
    try:
        amount = round(float(amount), 2) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amount = None

    with SessionLocal() as session:
        if session.get(Client, client_id) is None:
            return jsonify({"error": "Client not found"}), 404
        bill = Bill(client_id=client_id, name=name, due_day=due_day, amount=amount,
                    notes=(data.get("notes") or "").strip() or None)
        session.add(bill)
        session.commit()
        return jsonify(bill.to_dict()), 201


@app.route("/bills/<int:bill_id>", methods=["DELETE"])
def delete_bill(bill_id: int):
    if (err := require_auth()):
        return err
    with SessionLocal() as session:
        bill = session.get(Bill, bill_id)
        if bill is None:
            return jsonify({"error": "Bill not found"}), 404
        session.delete(bill)
        session.commit()
        return jsonify({"success": True})


if __name__ == "__main__":
    init_db()
    storage.ensure_storage()
    logger.info("Tables and storage ready; listening on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
