import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy import DateTime, Integer, String, Text, create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./data/orders.db')
CATALOG_URL = os.environ.get('CATALOG_URL', 'http://127.0.0.1:3001').rstrip('/')
if DATABASE_URL.startswith('sqlite:///'):
    Path(DATABASE_URL[len('sqlite:///'):]).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=3)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Order(Base):
    __tablename__='orders'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    restaurant_id: Mapped[str] = mapped_column(String(30), nullable=False)
    items_json: Mapped[str] = mapped_column(Text, nullable=False)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default='placed', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

Clean = Annotated[str, BeforeValidator(lambda s: s.strip() if isinstance(s,str) else s)]
class OrderItemRequest(BaseModel):
    menuItemId: Clean = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1, le=20)

class OrderRequest(BaseModel):
    customerId: Clean = Field(min_length=1, max_length=120)
    restaurantId: Clean = Field(min_length=1, max_length=30)
    items: list[OrderItemRequest] = Field(min_length=1, max_length=30)

def order_out(o:Order)->dict:
    created=o.created_at.isoformat() if o.created_at else None
    return {'id':o.id,'customerId':o.customer_id,'restaurantId':o.restaurant_id,
            'items':json.loads(o.items_json),'totalCents':o.total_cents,
            'status':o.status,'createdAt':created}

@asynccontextmanager
async def lifespan(app:FastAPI):
    # Bounded startup retries cover PostgreSQL readiness after a container starts.
    for attempt in range(30):
        try:
            Base.metadata.create_all(engine)
            break
        except OperationalError:
            if attempt==29: raise
            time.sleep(1)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0),limits=httpx.Limits(max_connections=100,max_keepalive_connections=20)) as client:
        app.state.catalog_client=client
        yield
    engine.dispose()

app=FastAPI(title='QuickBite Orders Service',version='1.0.0',lifespan=lifespan)

@app.get('/health')
def health():
    try:
        with engine.connect() as connection: connection.execute(text('SELECT 1'))
        return {'status':'ok','database':'connected'}
    except Exception as exc:
        raise HTTPException(status_code=503,detail='Orders database unavailable') from exc

@app.get('/live')
def live():
    return {'status':'ok'}

@app.post('/orders',status_code=201)
async def create_order(order:OrderRequest):
    try:
        response=await app.state.catalog_client.get(f'{CATALOG_URL}/restaurants/{order.restaurantId}/menu')
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503,detail='Catalog service unavailable') from exc
    if response.status_code==404:
        raise HTTPException(status_code=404,detail='Restaurant not found')
    if response.status_code!=200:
        raise HTTPException(status_code=502,detail='Catalog service error')
    menu={entry['id']:entry for entry in response.json()['items']}
    total=0; accepted=[]
    for item in order.items:
        if item.menuItemId not in menu:
            raise HTTPException(status_code=422,detail=f'Invalid menu item: {item.menuItemId}')
        m=menu[item.menuItemId]
        total+=m['priceCents']*item.quantity
        accepted.append({'menuItemId':item.menuItemId,'name':m['name'],
                         'quantity':item.quantity,'unitPriceCents':m['priceCents']})
    entity=Order(id=str(uuid.uuid4()),customer_id=order.customerId,
                 restaurant_id=order.restaurantId,items_json=json.dumps(accepted),total_cents=total,
                 status='placed',created_at=datetime.now(timezone.utc))
    with SessionLocal.begin() as session: session.add(entity)
    return order_out(entity)

@app.get('/orders')
def list_orders(limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0)):
    with SessionLocal() as session:
        result=session.scalars(select(Order).order_by(Order.created_at.desc(),Order.id.desc()).offset(offset).limit(limit)).all()
        return [order_out(o) for o in result]

@app.get('/orders/{order_id}')
def get_order(order_id:str):
    try: order_id=str(uuid.UUID(order_id))
    except ValueError: raise HTTPException(status_code=404,detail='Order not found')
    with SessionLocal() as session:
        entity=session.get(Order,order_id)
        if entity is None: raise HTTPException(status_code=404,detail='Order not found')
        return order_out(entity)
