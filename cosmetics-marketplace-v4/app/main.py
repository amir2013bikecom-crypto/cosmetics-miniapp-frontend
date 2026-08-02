import os
import logging
import hashlib
import hmac
import json
from typing import Optional, List
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Header, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base, relationship, selectinload
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text, select, desc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── CONFIG ─────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
SELLER_IDS = [int(x.strip()) for x in os.getenv("SELLER_IDS", "").split(",") if x.strip()]
SELLER_API_KEY = os.getenv("SELLER_API_KEY", "")
FRONTEND_URL = os.getenv("FRONTEND_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TELEGRAM_AUTH_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_AUTH_MAX_AGE_SECONDS", "86400"))
DELIVERY_COSTS = {"courier": 300.0, "sdek": 250.0, "post": 200.0}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── DATABASE ───────────────────────────────────────────────────────
Base = declarative_base()

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    price = Column(Float, nullable=False)
    old_price = Column(Float, nullable=True)
    image = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    rating = Column(Float, default=0.0)
    reviews_count = Column(Integer, default=0)

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, default=1)
    price = Column(Float, nullable=False)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    buyer_id = Column(String, nullable=False, index=True)
    buyer_name = Column(String, nullable=True)
    buyer_phone = Column(String, nullable=True)
    buyer_address = Column(Text, nullable=True)
    status = Column(String, default="pending")
    total = Column(Float, default=0.0)
    delivery_method = Column(String, default="courier")
    delivery_cost = Column(Float, default=0.0)
    track_number = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    items = relationship("OrderItem", lazy="selectin")

class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, nullable=False, index=True)
    buyer_id = Column(String, nullable=False)
    buyer_name = Column(String, nullable=True)
    rating = Column(Integer, default=5)
    text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ─── PYDANTIC SCHEMAS ───────────────────────────────────────────────
class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1)

class OrderCreate(BaseModel):
    buyer_name: Optional[str] = ""
    buyer_phone: Optional[str] = ""
    buyer_address: Optional[str] = ""
    items: List[OrderItemIn]
    delivery_method: Optional[str] = "courier"

    @validator("buyer_phone")
    def validate_phone(cls, v):
        if v and not v.replace("+", "").replace("-", "").replace(" ", "").isdigit():
            raise ValueError("Некорректный номер телефона")
        return v

VALID_STATUSES = {"pending", "shipped", "delivered", "cancelled"}
VALID_STATUS_TRANSITIONS = {
    "pending": {"shipped", "cancelled"},
    "shipped": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}

class StatusUpdate(BaseModel):
    status: str

    @validator("status")
    def validate_status(cls, v):
        if v not in VALID_STATUSES:
            raise ValueError(f"Статус должен быть одним из: {VALID_STATUSES}")
        return v

class TrackUpdate(BaseModel):
    track_number: str = Field(..., min_length=1)

class ReviewCreate(BaseModel):
    product_id: int
    buyer_id: str
    buyer_name: Optional[str] = ""
    rating: int = Field(5, ge=1, le=5)
    text: Optional[str] = Field("", max_length=2000)

# ─── BOT ────────────────────────────────────────────────────────────
bot_app: Optional[Application] = None

async def start_bot():
    global bot_app
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set")
        return
    bot_app = Application.builder().token(BOT_TOKEN).build()

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        keyboard = [[InlineKeyboardButton("🛍 Открыть магазин", web_app=WebAppInfo(url=FRONTEND_URL))]]
        await update.message.reply_text(
            f"Привет, {user.first_name}! Добро пожаловать в Мир Косметики ✨",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    bot_app.add_handler(CommandHandler("start", start_cmd))
    await bot_app.initialize()
    await bot_app.start()
    logger.info("Bot started")
    if WEBHOOK_URL:
        await bot_app.bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        logger.info(f"Webhook set to {WEBHOOK_URL}")

# ─── LIFESPAN ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await start_bot()
    yield
    if bot_app:
        await bot_app.stop()
        await bot_app.shutdown()
    await engine.dispose()

# ─── APP ────────────────────────────────────────────────────────────
app = FastAPI(title="Мир Косметики API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])

# ─── AUTH DEPENDENCY ────────────────────────────────────────────────
async def verify_seller(x_seller_key: Optional[str] = Header(None)):
    if not SELLER_API_KEY or x_seller_key != SELLER_API_KEY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid seller key")
    return x_seller_key

async def get_telegram_user(
    x_telegram_init_data: Optional[str] = Header(None),
) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram authentication is not configured")
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Missing Telegram init data")

    values = dict(parse_qsl(x_telegram_init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash or "auth_date" not in values or "user" not in values:
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")

    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram init data") from exc
    if datetime.now(timezone.utc).timestamp() - auth_date > TELEGRAM_AUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="Telegram session has expired")
    if "id" not in user:
        raise HTTPException(status_code=401, detail="Invalid Telegram user")
    return user

async def verify_seller_or_telegram(
    x_seller_key: Optional[str] = Header(None),
    x_telegram_init_data: Optional[str] = Header(None),
) -> dict:
    if x_seller_key and SELLER_API_KEY and hmac.compare_digest(x_seller_key, SELLER_API_KEY):
        return {"id": "service"}
    telegram_user = await get_telegram_user(x_telegram_init_data)
    if str(telegram_user["id"]) not in {str(seller_id) for seller_id in SELLER_IDS}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Seller access required")
    return telegram_user

# ─── ENDPOINTS ──────────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"message": "Мир Косметики API", "status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/v1/products")
async def get_products(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Product))
    products = result.scalars().all()
    return [{"id": p.id, "name": p.name, "category": p.category, "price": p.price,
             "old_price": p.old_price, "image": p.image, "description": p.description,
             "rating": p.rating, "reviews_count": p.reviews_count} for p in products]

@app.get("/api/v1/categories")
async def get_categories(session: AsyncSession = Depends(get_db)):
    result = await session.execute(select(Product.category).distinct())
    return [c[0] for c in result.all() if c[0]]

@app.post("/api/v1/orders")
async def create_order(
    order: OrderCreate,
    telegram_user: dict = Depends(get_telegram_user),
    session: AsyncSession = Depends(get_db),
):
    if not order.items:
        raise HTTPException(status_code=422, detail="Order must contain at least one item")
    if order.delivery_method not in DELIVERY_COSTS:
        raise HTTPException(status_code=422, detail="Invalid delivery method")

    product_ids = {item.product_id for item in order.items}
    result = await session.execute(select(Product).where(Product.id.in_(product_ids)))
    products = {product.id: product for product in result.scalars().all()}
    missing_ids = product_ids.difference(products)
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"Products not found: {sorted(missing_ids)}")

    delivery_cost = DELIVERY_COSTS[order.delivery_method]
    total = sum(products[item.product_id].price * item.quantity for item in order.items) + delivery_cost
    db_order = Order(
        buyer_id=str(telegram_user["id"]), buyer_name=telegram_user.get("first_name", ""),
        buyer_phone=order.buyer_phone, buyer_address=order.buyer_address,
        total=total, status="pending",
        delivery_method=order.delivery_method,
        delivery_cost=delivery_cost,
    )
    session.add(db_order)
    await session.flush()
    for item in order.items:
        session.add(OrderItem(
            order_id=db_order.id, product_id=item.product_id,
            product_name=products[item.product_id].name,
            quantity=item.quantity, price=products[item.product_id].price,
        ))
    await session.commit()
    logger.info(f"Order #{db_order.id} created by buyer {telegram_user['id']}")
    return {"success": True, "order_id": db_order.id, "total": total}

@app.get("/api/v1/orders/{buyer_id}")
async def get_orders(
    buyer_id: str,
    telegram_user: dict = Depends(get_telegram_user),
    session: AsyncSession = Depends(get_db),
):
    if buyer_id != str(telegram_user["id"]):
        raise HTTPException(status_code=403, detail="You can only view your own orders")
    result = await session.execute(
        select(Order).where(Order.buyer_id == buyer_id)
        .order_by(desc(Order.created_at))
        .options(selectinload(Order.items))
    )
    orders = result.scalars().all()
    return [{
        "id": o.id, "buyer_id": o.buyer_id, "buyer_name": o.buyer_name,
        "buyer_phone": o.buyer_phone, "buyer_address": o.buyer_address,
        "status": o.status, "total": o.total,
        "delivery_method": o.delivery_method,
        "delivery_cost": o.delivery_cost,
        "track_number": o.track_number,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "items": [{"product_id": i.product_id, "product_name": i.product_name,
                   "quantity": i.quantity, "price": i.price} for i in o.items]
    } for o in orders]

@app.get("/api/v1/admin/orders")
async def admin_orders(
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_seller_or_telegram)
):
    result = await session.execute(
        select(Order).order_by(desc(Order.created_at)).options(selectinload(Order.items))
    )
    orders = result.scalars().all()
    return [{
        "id": o.id, "buyer_id": o.buyer_id, "buyer_name": o.buyer_name,
        "buyer_phone": o.buyer_phone, "buyer_address": o.buyer_address,
        "status": o.status, "total": o.total,
        "delivery_method": o.delivery_method,
        "delivery_cost": o.delivery_cost,
        "track_number": o.track_number,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "items": [{"product_id": i.product_id, "product_name": i.product_name,
                   "quantity": i.quantity, "price": i.price} for i in o.items]
    } for o in orders]

@app.patch("/api/v1/orders/{order_id}/status")
async def update_status(
    order_id: int,
    update: StatusUpdate,
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_seller_or_telegram)
):
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if update.status not in VALID_STATUS_TRANSITIONS[order.status]:
        raise HTTPException(status_code=409, detail=f"Cannot change {order.status} order to {update.status}")
    order.status = update.status
    await session.commit()
    logger.info(f"Order #{order_id} status updated to {update.status}")
    return {"success": True, "order_id": order.id, "status": order.status}

@app.patch("/api/v1/orders/{order_id}/track")
async def update_track(
    order_id: int,
    update: TrackUpdate,
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_seller_or_telegram)
):
    result = await session.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if "shipped" not in VALID_STATUS_TRANSITIONS[order.status]:
        raise HTTPException(status_code=409, detail=f"Cannot ship {order.status} order")
    order.track_number = update.track_number
    order.status = "shipped"
    await session.commit()
    logger.info(f"Order #{order_id} track updated: {update.track_number}")
    return {"success": True, "order_id": order.id, "track_number": order.track_number}

@app.post("/api/v1/reviews")
async def create_review(
    review: ReviewCreate,
    telegram_user: dict = Depends(get_telegram_user),
    session: AsyncSession = Depends(get_db),
):
    product = await session.get(Product, review.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    db_review = Review(
        product_id=review.product_id, buyer_id=str(telegram_user["id"]),
        buyer_name=telegram_user.get("first_name", ""), rating=review.rating, text=review.text
    )
    session.add(db_review)
    await session.commit()
    return {"success": True, "review_id": db_review.id}

@app.get("/api/v1/reviews/{product_id}")
async def get_reviews(product_id: int, session: AsyncSession = Depends(get_db)):
    result = await session.execute(
        select(Review).where(Review.product_id == product_id).order_by(desc(Review.created_at))
    )
    reviews = result.scalars().all()
    return [{
        "id": r.id, "buyer_id": r.buyer_id, "buyer_name": r.buyer_name,
        "rating": r.rating, "text": r.text,
        "created_at": r.created_at.isoformat() if r.created_at else None
    } for r in reviews]

@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not bot_app:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}

@app.post("/api/v1/seed")
async def seed_data(
    session: AsyncSession = Depends(get_db),
    _: dict = Depends(verify_seller_or_telegram),
):
    result = await session.execute(select(Product))
    if result.scalars().first():
        return {"message": "Already seeded"}

    products = [
        Product(name="Гидрофильное масло", category="Уход за лицом", price=890, old_price=1200,
                image="https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=400",
                description="Глубокое очищение кожи", rating=4.8, reviews_count=124),
        Product(name="Сыворотка с витамином C", category="Уход за лицом", price=1290, old_price=1590,
                image="https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?w=400",
                description="Осветление и выравнивание тона", rating=4.9, reviews_count=89),
        Product(name="Крем для лица увлажняющий", category="Уход за лицом", price=750, old_price=950,
                image="https://images.unsplash.com/photo-1570194065650-d99fb4b38b15?w=400",
                description="24 часа увлажнения", rating=4.7, reviews_count=156),
        Product(name="Тоник для лица", category="Уход за лицом", price=450, old_price=600,
                image="https://images.unsplash.com/photo-1601049541289-9b1b7bbbfe19?w=400",
                description="Сужение пор и баланс pH", rating=4.6, reviews_count=78),
        Product(name="Патчи под глаза", category="Уход за лицом", price=590, old_price=790,
                image="https://images.unsplash.com/photo-1596755389378-c31d21fd1273?w=400",
                description="Устранение отёков и тёмных кругов", rating=4.5, reviews_count=203),
        Product(name="Мицеллярная вода", category="Уход за лицом", price=490, old_price=650,
                image="https://images.unsplash.com/photo-1556228578-0d85b1a4d571?w=400",
                description="Мягкое очищение без смывания", rating=4.7, reviews_count=312),
        Product(name="Матовая помада", category="Макияж", price=650, old_price=890,
                image="https://images.unsplash.com/photo-1586495777744-4413f21062fa?w=400",
                description="Стойкий цвет на 12 часов", rating=4.7, reviews_count=256),
        Product(name="Тушь для ресниц", category="Макияж", price=890, old_price=1100,
                image="https://images.unsplash.com/photo-1631214524115-6f8eb1beb6b5?w=400",
                description="Объём и удлинение", rating=4.8, reviews_count=312),
        Product(name="Тональный кушон", category="Макияж", price=1590, old_price=1990,
                image="https://images.unsplash.com/photo-1596462502278-27bfdc403348?w=400",
                description="Лёгкое покрытие", rating=4.5, reviews_count=89),
        Product(name="Палетка теней", category="Макияж", price=1290, old_price=1590,
                image="https://images.unsplash.com/photo-1596704017254-9b121068fb31?w=400",
                description="12 оттенков нюд", rating=4.9, reviews_count=178),
        Product(name="Гель для бровей", category="Макияж", price=450, old_price=590,
                image="https://images.unsplash.com/photo-1596462502278-27bfdc403348?w=400",
                description="Фиксация и объём", rating=4.6, reviews_count=134),
        Product(name="Хайлайтер", category="Макияж", price=790, old_price=990,
                image="https://images.unsplash.com/photo-1596704017254-9b121068fb31?w=400",
                description="Сияние кожи", rating=4.8, reviews_count=167),
        Product(name="Восстанавливающий шампунь", category="Уход за волосами", price=890, old_price=1100,
                image="https://images.unsplash.com/photo-1527799820374-dcf8d9d4a388?w=400",
                description="Для сухих волос", rating=4.6, reviews_count=134),
        Product(name="Маска для волос", category="Уход за волосами", price=1150, old_price=1390,
                image="https://images.unsplash.com/photo-1522337360788-8b13dee7a37e?w=400",
                description="Глубокое питание", rating=4.8, reviews_count=98),
        Product(name="Масло для волос", category="Уход за волосами", price=690, old_price=850,
                image="https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?w=400",
                description="Блеск и защита кончиков", rating=4.5, reviews_count=67),
        Product(name="Термозащитный спрей", category="Уход за волосами", price=550, old_price=720,
                image="https://images.unsplash.com/photo-1527799820374-dcf8d9d4a388?w=400",
                description="Защита при укладке", rating=4.4, reviews_count=89),
        Product(name="Парфюм Floral", category="Парфюмерия", price=3490, old_price=4200,
                image="https://images.unsplash.com/photo-1541643600914-78b084683601?w=400",
                description="Нежный цветочный аромат", rating=4.9, reviews_count=245),
        Product(name="Парфюм Woody", category="Парфюмерия", price=4290, old_price=5200,
                image="https://images.unsplash.com/photo-1594035910387-fea47794261f?w=400",
                description="Древесные ноты", rating=4.8, reviews_count=189),
        Product(name="Туалетная вода Fresh", category="Парфюмерия", price=2590, old_price=3100,
                image="https://images.unsplash.com/photo-1596462502278-27bfdc403348?w=400",
                description="Свежий цитрусовый аромат", rating=4.7, reviews_count=156),
        Product(name="Масляные духи", category="Парфюмерия", price=1890, old_price=2400,
                image="https://images.unsplash.com/photo-1541643600914-78b084683601?w=400",
                description="Стойкий восточный аромат", rating=4.8, reviews_count=112),
        Product(name="Скраб для тела", category="Уход за телом", price=690, old_price=850,
                image="https://images.unsplash.com/photo-1556228578-0d85b1a4d571?w=400",
                description="Кофейный скраб", rating=4.6, reviews_count=112),
        Product(name="Крем для рук", category="Уход за телом", price=450, old_price=550,
                image="https://images.unsplash.com/photo-1596755389378-c31d21fd1273?w=400",
                description="Питательный крем", rating=4.5, reviews_count=89),
        Product(name="Гель для душа", category="Уход за телом", price=590, old_price=720,
                image="https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?w=400",
                description="Увлажнение и аромат", rating=4.7, reviews_count=134),
        Product(name="Масло для тела", category="Уход за телом", price=790, old_price=950,
                image="https://images.unsplash.com/photo-1601049541289-9b1b7bbbfe19?w=400",
                description="Питание после душа", rating=4.8, reviews_count=78),
        Product(name="Дезодорант спрей", category="Уход за телом", price=350, old_price=450,
                image="https://images.unsplash.com/photo-1556228578-0d85b1a4d571?w=400",
                description="48 часов защиты", rating=4.4, reviews_count=198),
    ]

    for p in products:
        session.add(p)
    await session.commit()
    return {"message": f"Seeded {len(products)} products"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
