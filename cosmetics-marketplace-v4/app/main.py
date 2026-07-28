from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, select
from sqlalchemy.sql import func
from pydantic import BaseModel
from typing import List, Optional
import os
import asyncio
import aiohttp

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
if "?sslmode=require" in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("?sslmode=require", "")

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

# ===== MODELS =====
class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    description = Column(Text, nullable=True)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(Text)
    price = Column(Float)
    image_url = Column(String)
    category_id = Column(Integer, ForeignKey("categories.id"))
    stock = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    buyer_id = Column(String, index=True)
    buyer_name = Column(String)
    buyer_phone = Column(String)
    buyer_address = Column(Text)
    total = Column(Float)
    status = Column(String, default="pending")
    promo_code = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    product_id = Column(Integer)
    product_name = Column(String)
    quantity = Column(Integer)
    price = Column(Float)

class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"))
    buyer_id = Column(String)
    buyer_name = Column(String)
    rating = Column(Integer)
    text = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PromoCode(Base):
    __tablename__ = "promo_codes"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)
    discount_percent = Column(Integer, default=0)
    active = Column(Integer, default=1)

# ===== PYDANTIC SCHEMAS =====
class OrderItemIn(BaseModel):
    product_id: int
    quantity: int

class OrderIn(BaseModel):
    buyer_id: str
    buyer_name: str
    buyer_phone: str
    buyer_address: str
    items: List[OrderItemIn]
    promo_code: Optional[str] = None

class StatusUpdate(BaseModel):
    status: str

class ReviewIn(BaseModel):
    product_id: int
    buyer_id: str
    buyer_name: Optional[str] = None
    rating: int
    text: str

class PromoValidate(BaseModel):
    code: str

# ===== APP =====
app = FastAPI(title="Мир Косметики API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SELLER_KEY = os.getenv("SELLER_API_KEY", "admin123")
SELLERS = [int(x) for x in os.getenv("SELLER_IDS", "7890854793,940063562").split(",") if x]
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

async def get_db():
    async with async_session() as session:
        yield session

async def send_telegram_message(chat_id: int, text: str, reply_markup: dict = None):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                pass
    except Exception:
        pass

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/v1/products/")
async def list_products(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product))
    rows = result.scalars().all()
    out = []
    for p in rows:
        cat = await db.execute(select(Category).where(Category.id == p.category_id))
        c = cat.scalar_one_or_none()
        out.append({
            "id": p.id, "name": p.name, "description": p.description,
            "price": p.price, "image_url": p.image_url, "stock": p.stock,
            "category": {"name": c.name} if c else None
        })
    return out

@app.get("/api/v1/categories/")
async def list_categories(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Category))
    return [{"id": c.id, "name": c.name, "description": c.description} for c in result.scalars().all()]

@app.post("/api/v1/orders/")
async def create_order(data: OrderIn, db: AsyncSession = Depends(get_db)):
    total = 0
    for it in data.items:
        prod = await db.get(Product, it.product_id)
        if prod:
            total += prod.price * it.quantity

    discount = 0
    if data.promo_code:
        promo = await db.execute(select(PromoCode).where(PromoCode.code == data.promo_code, PromoCode.active == 1))
        p = promo.scalar_one_or_none()
        if p:
            discount = total * (p.discount_percent / 100)

    total = max(0, total - discount)

    order = Order(
        buyer_id=data.buyer_id, buyer_name=data.buyer_name,
        buyer_phone=data.buyer_phone, buyer_address=data.buyer_address,
        total=total, status="pending", promo_code=data.promo_code
    )
    db.add(order)
    await db.flush()

    for it in data.items:
        prod = await db.get(Product, it.product_id)
        if prod:
            db.add(OrderItem(
                order_id=order.id, product_id=it.product_id,
                product_name=prod.name, quantity=it.quantity, price=prod.price
            ))

    await db.commit()

    for sid in SELLERS:
        text = f"🛒 <b>Новый заказ #{order.id}</b>\nСумма: {total} ₽\nПокупатель: {data.buyer_name}\nТел: {data.buyer_phone}"
        await send_telegram_message(sid, text)

    return {"id": order.id, "total": total, "status": "pending"}

@app.get("/api/v1/orders/{buyer_id}")
async def buyer_orders(buyer_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Order).where(Order.buyer_id == buyer_id).order_by(Order.created_at.desc()))
    rows = result.scalars().all()
    out = []
    for o in rows:
        items_r = await db.execute(select(OrderItem).where(OrderItem.order_id == o.id))
        items = items_r.scalars().all()
        out.append({
            "id": o.id, "total": float(o.total), "status": o.status,
            "buyer_id": o.buyer_id, "buyer_name": o.buyer_name,
            "buyer_address": o.buyer_address, "buyer_phone": o.buyer_phone,
            "promo_code": o.promo_code,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "items": [{"product_name": i.product_name, "quantity": i.quantity, "price": float(i.price)} for i in items]
        })
    return out

# ===== ИСПРАВЛЕННЫЙ ENDPOINT: /admin/orders вместо /orders/all =====
@app.get("/api/v1/admin/orders")
async def admin_orders(x_seller_key: str = Header(""), db: AsyncSession = Depends(get_db)):
    if x_seller_key != SELLER_KEY:
        raise HTTPException(status_code=403, detail="Неверный ключ продавца")
    result = await db.execute(select(Order).order_by(Order.created_at.desc()))
    rows = result.scalars().all()
    out = []
    for o in rows:
        items_r = await db.execute(select(OrderItem).where(OrderItem.order_id == o.id))
        items = items_r.scalars().all()
        out.append({
            "id": o.id, "total": float(o.total), "status": o.status,
            "buyer_id": o.buyer_id, "buyer_name": o.buyer_name,
            "buyer_address": o.buyer_address, "buyer_phone": o.buyer_phone,
            "promo_code": o.promo_code,
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "items": [{"product_name": i.product_name, "quantity": i.quantity, "price": float(i.price)} for i in items]
        })
    return out

@app.patch("/api/v1/orders/{order_id}/status")
async def update_status(order_id: int, data: StatusUpdate, x_seller_key: str = Header(""), db: AsyncSession = Depends(get_db)):
    if x_seller_key != SELLER_KEY:
        raise HTTPException(status_code=403, detail="Неверный ключ продавца")
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    order.status = data.status
    await db.commit()

    if data.status == "shipped":
        text = f"📦 Ваш заказ #{order_id} отправлен! Ожидайте доставку."
        keyboard = {"inline_keyboard": [[{"text": "✅ Получил", "callback_data": f"buyer_received_{order_id}"}, {"text": "❌ Не получил", "callback_data": f"buyer_notreceived_{order_id}"}]]}
        await send_telegram_message(int(order.buyer_id), text, keyboard)
    elif data.status == "cancelled":
        await send_telegram_message(int(order.buyer_id), f"❌ Заказ #{order_id} отменён продавцом.")
    elif data.status == "delivered":
        await send_telegram_message(int(order.buyer_id), f"✅ Заказ #{order_id} доставлен! Спасибо за покупку.")
        for sid in SELLERS:
            await send_telegram_message(sid, f"✅ Заказ #{order_id} доставлен покупателю.")

    return {"id": order.id, "status": order.status}

@app.post("/api/v1/reviews/")
async def create_review(data: ReviewIn, db: AsyncSession = Depends(get_db)):
    review = Review(**data.dict())
    db.add(review)
    await db.commit()
    return {"id": review.id}

@app.get("/api/v1/reviews/{product_id}")
async def list_reviews(product_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Review).where(Review.product_id == product_id).order_by(Review.created_at.desc()))
    return [{"id": r.id, "buyer_name": r.buyer_name, "rating": r.rating, "text": r.text, "created_at": r.created_at.isoformat() if r.created_at else None} for r in result.scalars().all()]

@app.post("/api/v1/promo/validate")
async def validate_promo(data: PromoValidate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PromoCode).where(PromoCode.code == data.code, PromoCode.active == 1))
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    return {"code": promo.code, "discount_percent": promo.discount_percent}

@app.post("/api/v1/seed")
async def seed(db: AsyncSession = Depends(get_db)):
    cats = ["Уход за лицом", "Уход за волосами", "Макияж", "Парфюмерия", "Уход за телом"]
    for c in cats:
        exists = await db.execute(select(Category).where(Category.name == c))
        if not exists.scalar_one_or_none():
            db.add(Category(name=c))
    await db.commit()

    cat_result = await db.execute(select(Category))
    cat_map = {c.name: c.id for c in cat_result.scalars().all()}

    sample_products = [
        ("Гидрофильное масло", "Нежное очищение кожи", 1290, "https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=400&q=80", cat_map.get("Уход за лицом")),
        ("Витаминная сыворотка C10", "Антиоксидантная защита", 1890, "https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?w=400&q=80", cat_map.get("Уход за лицом")),
        ("Восстанавливающий шампунь", "Для сухих волос", 890, "https://images.unsplash.com/photo-1527799820374-dcf8d9d4a388?w=400&q=80", cat_map.get("Уход за волосами")),
        ("Маска для волос", "Глубокое питание", 1150, "https://images.unsplash.com/photo-1522337360788-8b13dee7a37e?w=400&q=80", cat_map.get("Уход за волосами")),
        ("Тональный кушон", "Лёгкое покрытие", 1590, "https://images.unsplash.com/photo-1596462502278-27bfdc403348?w=400&q=80", cat_map.get("Макияж")),
        ("Матовая помада", "Стойкий цвет", 790, "https://images.unsplash.com/photo-1586495777744-4413f21062fa?w=400&q=80", cat_map.get("Макияж")),
        ("Парфюм Floral", "Нежный цветочный аромат", 3490, "https://images.unsplash.com/photo-1541643600914-78b084683601?w=400&q=80", cat_map.get("Парфюмерия")),
        ("Парфюм Woody", "Древесные ноты", 4290, "https://images.unsplash.com/photo-1594035910387-fea47794261f?w=400&q=80", cat_map.get("Парфюмерия")),
        ("Скраб для тела", "Кофейный скраб", 690, "https://images.unsplash.com/photo-1556228578-0d85b1a4d571?w=400&q=80", cat_map.get("Уход за телом")),
        ("Крем для рук", "Питательный крем", 450, "https://images.unsplash.com/photo-1596755389378-c31d21fd1273?w=400&q=80", cat_map.get("Уход за телом")),
    ]

    for name, desc, price, img, cat_id in sample_products:
        exists = await db.execute(select(Product).where(Product.name == name))
        if not exists.scalar_one_or_none():
            db.add(Product(name=name, description=desc, price=price, image_url=img, category_id=cat_id, stock=100))

    promo_codes = [("WELCOME10", 10), ("SUMMER20", 20), ("VIP30", 30)]
    for code, discount in promo_codes:
        exists = await db.execute(select(PromoCode).where(PromoCode.code == code))
        if not exists.scalar_one_or_none():
            db.add(PromoCode(code=code, discount_percent=discount, active=1))

    await db.commit()
    return {"detail": "База заполнена"}
