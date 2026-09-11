"""GC Tracker — multi-client receipt tracker.

One Flask service: JSON API plus the static dashboard and mobile upload page.
Adding a client is a form, not a deployment.
"""

import hmac
import logging
import os

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sqlalchemy import func, select

from db import Client, Receipt, SessionLocal, init_db, unique_slug

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


if __name__ == "__main__":
    init_db()
    logger.info("Tables ready; listening on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
