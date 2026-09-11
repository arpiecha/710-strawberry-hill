"""Database models and setup.

One Postgres database holds every client and every receipt, so adding a new
client is a row rather than a new deployment.
"""

import calendar
import logging
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
    start_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    receipts: Mapped[list["Receipt"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )
    bills: Mapped[list["Bill"]] = relationship(
        back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )

    def to_dict(self, receipt_count: int | None = None) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "address": self.address,
            "notes": self.notes,
            "start_date": self.start_date.isoformat() if self.start_date else None,
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


class Bill(Base):
    """A recurring bill for a job. A list on the dashboard — nothing is sent."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    due_day: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-31
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    client: Mapped[Client] = relationship(back_populates="bills")

    def next_due(self, today: date | None = None) -> date:
        """The next date this falls due, clamped to short months."""
        today = today or date.today()
        year, month = today.year, today.month
        day = min(self.due_day, calendar.monthrange(year, month)[1])
        if day < today.day:
            month += 1
            if month > 12:
                month, year = 1, year + 1
            day = min(self.due_day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def to_dict(self, today: date | None = None) -> dict:
        today = today or date.today()
        due = self.next_due(today)
        return {
            "id": self.id,
            "client_id": self.client_id,
            "name": self.name,
            "due_day": self.due_day,
            "amount": float(self.amount) if self.amount is not None else None,
            "notes": self.notes or "",
            "next_due": due.isoformat(),
            "days_away": (due - today).days,
        }


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add columns introduced after a table was first created.

    create_all() only creates missing tables, never missing columns, so a
    database that predates a new column would keep failing on every query
    that touches it.
    """
    from sqlalchemy import inspect, text

    wanted = {"clients": {"start_date": "DATE"}}
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in wanted.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    logging.getLogger(__name__).info("Added column %s.%s", table, name)


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
