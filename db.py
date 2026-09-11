"""Database models and setup.

One Postgres database holds every client and every receipt, so adding a new
client is a row rather than a new deployment.
"""

import os
import re
from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]

# Railway hands out postgres:// URLs; SQLAlchemy 2 wants postgresql://.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    receipts: Mapped[list["Receipt"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )

    def to_dict(self, receipt_count: int | None = None) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "address": self.address,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if receipt_count is not None:
            data["receipt_count"] = receipt_count
        return data


class Receipt(Base):
    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    store: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # Negative for returns, so summing the column gives net spend.
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    items: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    client: Mapped[Client] = relationship(back_populates="receipts")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "date": self.date.isoformat() if self.date else None,
            "store": self.store,
            "category": self.category,
            "type": self.type,
            "amount": float(self.amount),
            "items": self.items or "",
            "notes": self.notes or "",
            "has_image": bool(self.image_path),
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def init_db() -> None:
    Base.metadata.create_all(engine)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "client"


def unique_slug(session, name: str, exclude_id: int | None = None) -> str:
    """Slugify a name, adding -2, -3 ... if that slug is already taken."""
    base = slugify(name)
    candidate = base
    n = 1
    while True:
        stmt = select(Client).where(Client.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(Client.id != exclude_id)
        if session.scalars(stmt).first() is None:
            return candidate
        n += 1
        candidate = f"{base}-{n}"
