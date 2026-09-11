"""Receipt images on the Railway volume.

One file per receipt at <RECEIPT_STORAGE_DIR>/<client_id>/<receipt_id>.jpg.
The path is stored on the row so it survives a change of storage dir.
"""

import logging
import os

logger = logging.getLogger(__name__)

STORAGE_DIR = os.environ.get("RECEIPT_STORAGE_DIR", "/data/receipts")


def ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def save_image(client_id: int, receipt_id: int, image_bytes: bytes) -> str:
    client_dir = os.path.join(STORAGE_DIR, str(client_id))
    os.makedirs(client_dir, exist_ok=True)
    path = os.path.join(client_dir, f"{receipt_id}.jpg")
    with open(path, "wb") as f:
        f.write(image_bytes)
    logger.info("Saved receipt image %s (%s bytes)", path, len(image_bytes))
    return path


def read_image(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        logger.warning("Could not read receipt image %s: %s", path, e)
        return None


def delete_image(path: str | None) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not delete receipt image %s: %s", path, e)
