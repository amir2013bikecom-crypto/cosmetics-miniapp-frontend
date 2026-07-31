from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, select
from sqlalchemy.sql import func
from pydantic import BaseModel
from typing import List, Optional
import os
import aiohttp

# ===== DATABASE =====
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./app.db")
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

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(Text)
    price = Column(Float)
    old_price = Column(Float, nullable=True)
    image = Column(String)
    category = Column(String)
    rating = Column(Float, default=0)
    reviews_count = Column(Integer, default=0)
    stock = Column(Integer, default=100)

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
    product_name: str
    quantity: int
    price: float

class OrderIn(BaseModel):
    buyer_id: str
    buyer_name: str
    buyer_phone: str
    buyer_address: str
    items: List[OrderItemIn]
    total: float
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

# ===== AUTOSTART: SEED PRODUCTS =====
@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Автозаполнение если база пуста
    async with async_session() as session:
        result = await session.execute(select(Product))
        if not result.scalars().all():
            sample = [
                {"name":"Гидрофильное масло","category":"Уход за лицом","price":890,"old_price":1200,"image":"https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=400","description":"Глубокое очищение кожи","rating":4.8,"reviews_count":124},
                {"name":"Сыворотка с витамином C","category":"Уход за лицом","price":1290,"old_price":1590,"image":"https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?w=400","description":"Осветление и выравнивание тона","rating":4.9,"reviews_count":89},
                {"name":"Матовая помада","category":"Макияж","price":650,"old_price":890,"image":"https://images.unsplash.com/photo-1586495777744-4413f21062fa?w=400","description":"Стойкий цвет на 12 часов","rating":4.7,"reviews_count":256},
            ]
            for p in sample:
                session.add(Product(**p))
            await session.commit()
            print("✅ SEED: Товары добавлены автоматически")

@app.get("/")
async def root():
    return {"message": "Мир Косметики API", "status": "ok"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/v1/products")
async def list_products(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product))
    rows = result.scalars().all()
    return [{"id":p.id,"name":p.name,"category":p.category,"price":p.price,"old_price":p.old_price,"image":p.image,"description":p.description,"rating":p.rating,"reviews_count":p.reviews_count} for p in rows]

@app.get("/api/v1/categories")
async def list_categories(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product.category).distinct())
    return [c[0] for c in result.all() if c[0]]

@app.post("/api/v1/orders")
async def create_order(data: OrderIn, db: AsyncSession = Depends(get_db)):
    order = Order(
        buyer_id=data.buyer_id, buyer_name=data.buyer_name,
        buyer_phone=data.buyer_phone, buyer_address=data.buyer_address,
        total=data.total, status="pending", promo_code=data.promo_code
    )
    db.add(order)
    await db.flush()

    for it in data.items:
        db.add(OrderItem(
            order_id=order.id, product_id=it.product_id,
            product_name=it.product_name, quantity=it.quantity, price=it.price
        ))

    await db.commit()

    for sid in SELLERS:
        text = f"🛒 <b>Новый заказ #{order.id}</b>\nСумма: {data.total} ₽\nПокупатель: {data.buyer_name}\nТел: {data.buyer_phone}"
        await send_telegram_message(sid, text)

    return {"success": True, "id": order.id, "total": data.total, "status": "pending"}

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
        text = f"📦 Ваш заказ #{order_id} отправлен!"
        keyboard = {"inline_keyboard": [[{"text": "✅ Получил", "callback_data": f"received_{order_id}"}, {"text": "❌ Не получил", "callback_data": f"notreceived_{order_id}"}]]}
        await send_telegram_message(int(order.buyer_id), text, keyboard)
    elif data.status == "cancelled":
        await send_telegram_message(int(order.buyer_id), f"❌ Заказ #{order_id} отменён.")
    elif data.status == "delivered":
        await send_telegram_message(int(order.buyer_id), f"✅ Заказ #{order_id} доставлен! Спасибо.")

    return {"id": order.id, "status": order.status}

@app.post("/api/v1/reviews")
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
    sample = [
        {"name":"Гидрофильное масло","category":"Уход за лицом","price":890,"old_price":1200,"image":"https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=400","description":"Глубокое очищение кожи","rating":4.8,"reviews_count":124},
        {"name":"Сыворотка с витамином C","category":"Уход за лицом","price":1290,"old_price":1590,"image":"https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?w=400","description":"Осветление и выравнивание тона","rating":4.9,"reviews_count":89},
        {"name":"Матовая помада","category":"Макияж","price":650,"old_price":890,"image":"https://images.unsplash.com/photo-1586495777744-4413f21062fa?w=400","description":"Стойкий цвет на 12 часов","rating":4.7,"reviews_count":256},
    ]
    for p in sample:
        exists = await db.execute(select(Product).where(Product.name == p["name"]))
        if not exists.scalar_one_or_none():
            db.add(Product(**p))
    await db.commit()
    return {"message": "Seeded 3 products"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
