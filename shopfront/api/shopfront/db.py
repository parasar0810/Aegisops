"""Database layer (SQLAlchemy 2). PostgreSQL in deployment, SQLite in tests."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Integer, String, Text, create_engine, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./shopfront.db")


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    image: Mapped[str] = mapped_column(String(16))  # emoji used as a lightweight product image


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), default="placed")
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), default=lambda: datetime.now(UTC).isoformat())
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cents: Mapped[int] = mapped_column(Integer)
    order: Mapped[Order] = relationship(back_populates="items")


SEED = [
    ("SF-1001", "Aurora Wireless Headphones", "Audio", "Over-ear, 40 h battery, active noise cancelling.", 12999, 40, "🎧"),
    ("SF-1002", "Pulse Smartwatch", "Wearables", "Heart-rate, GPS and 7-day battery in a 42 mm case.", 18999, 25, "⌚"),
    ("SF-1003", "Nimbus Mechanical Keyboard", "Computing", "Hot-swappable switches, aluminium frame.", 8999, 60, "⌨️"),
    ("SF-1004", "Orbit 4K Webcam", "Computing", "4K/30 fps with auto-framing and dual mics.", 7499, 35, "📷"),
    ("SF-1005", "Terra Trail Backpack", "Outdoor", "28 L, waterproof, laptop sleeve.", 5999, 80, "🎒"),
    ("SF-1006", "Summit Insulated Bottle", "Outdoor", "Keeps drinks cold for 24 h. 750 ml.", 2499, 150, "🧴"),
    ("SF-1007", "Lumen Desk Lamp", "Home", "Dimmable LED with wireless charging base.", 4499, 45, "💡"),
    ("SF-1008", "Brew Pour-Over Kit", "Home", "Ceramic dripper, glass carafe and filters.", 3499, 70, "☕"),
    ("SF-1009", "Echo Bluetooth Speaker", "Audio", "360° sound, IP67, 20 h playtime.", 6999, 55, "🔊"),
    ("SF-1010", "Stride Running Shoes", "Outdoor", "Lightweight daily trainer with carbon plate.", 11999, 30, "👟"),
    ("SF-1011", "Glide Wireless Mouse", "Computing", "Silent clicks, 70-day battery.", 2999, 120, "🖱️"),
    ("SF-1012", "Haven Throw Blanket", "Home", "Recycled-wool blend, 130×170 cm.", 5499, 40, "🛋️"),
]


def _broken_url(url: str) -> str:
    """Same database, unreachable port: produces genuine connection-refused errors."""
    u = make_url(url)
    if u.drivername.startswith("sqlite"):
        return "sqlite:////nonexistent-dir/shopfront.db"
    return u.set(port=6543).render_as_string(hide_password=False)


class Database:
    def __init__(self, url: str = DATABASE_URL) -> None:
        self.url = url
        self.broken = False
        self._lock = threading.Lock()
        self.engine = self._make_engine(url)

    @staticmethod
    def _make_engine(url: str) -> Engine:
        if url.startswith("sqlite"):
            return create_engine(url, connect_args={"check_same_thread": False})
        return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_timeout=3,
                             connect_args={"connect_timeout": 3})

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def init(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.session() as s:
            if not s.scalar(select(Product.id).limit(1)):
                s.add_all(Product(sku=a, name=b, category=c, description=d, price_cents=e, stock=f, image=g)
                          for a, b, c, d, e, f, g in SEED)
                s.commit()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def pool_in_use_ratio(self) -> float:
        pool = self.engine.pool
        try:
            return pool.checkedout() / max(pool.size() + getattr(pool, "_max_overflow", 0), 1)
        except AttributeError:
            return 0.0

    def set_broken(self, broken: bool) -> None:
        """Swap the engine to an unreachable endpoint (fault) or back to the real one (fix)."""
        with self._lock:
            old = self.engine
            self.engine = self._make_engine(_broken_url(self.url) if broken else self.url)
            self.broken = broken
            old.dispose()


db = Database()
