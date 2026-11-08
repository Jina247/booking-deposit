from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, NaiveDatetime
from sqlmodel import Field, Session, SQLModel, create_engine, select

from . import config, solana

engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
booking_lock = Lock()  # one booking written at a time (fine for SQLite / a demo)

# Booking states: pending -> deposited -> completed | no_show ; pending -> expired
PENDING, DEPOSITED, COMPLETED, NO_SHOW, EXPIRED = "pending", "deposited", "completed", "no_show", "expired"
BLOCKING = (DEPOSITED, COMPLETED, NO_SHOW)  # always block the slot; pending only blocks until it expires


class Service(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    duration_min: int
    price_cents: int
    deposit_cents: int


class Booking(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    service_id: int = Field(foreign_key="service.id")
    start: NaiveDatetime
    end: NaiveDatetime
    customer_name: str
    customer_email: str
    status: str = PENDING
    reference: str = Field(index=True, unique=True)
    signature: Optional[str] = Field(default=None, unique=True)  # a tx can only pay for one booking
    created_at: NaiveDatetime = Field(default_factory=datetime.now)
    expires_at: NaiveDatetime


@asynccontextmanager
async def lifespan(app: FastAPI):
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if not s.exec(select(Service)).first():
            s.add(Service(name="Gel manicure", duration_min=60, price_cents=4500, deposit_cents=1000))
            s.add(Service(name="Haircut", duration_min=30, price_cents=3000, deposit_cents=500))
            s.commit()
    yield


app = FastAPI(title="Booking Deposits on Solana", lifespan=lifespan)


def get_session():
    with Session(engine) as s:
        yield s


def require_owner(x_owner_key: str = Header(default="")):
    if x_owner_key != config.OWNER_KEY:
        raise HTTPException(401, "Owner key required")


# ---------- helpers ----------
def expire_stale(s: Session):
    now = datetime.now()
    stale = s.exec(select(Booking).where(Booking.status == PENDING, Booking.expires_at < now)).all()
    for b in stale:
        b.status = EXPIRED
        s.add(b)
    if stale:
        s.commit()


def slot_taken(s: Session, start: datetime, end: datetime) -> bool:
    """Any booking that overlaps [start, end) and still holds the slot."""
    now = datetime.now()
    rows = s.exec(select(Booking).where(Booking.start < end, Booking.end > start)).all()
    return any(b.status in BLOCKING or (b.status == PENDING and b.expires_at > now) for b in rows)


def to_out(b: Booking, s: Session) -> dict:
    svc = s.get(Service, b.service_id)
    out = {
        "id": b.id,
        "service": svc.name,
        "start": b.start.isoformat(timespec="minutes"),
        "customer_name": b.customer_name,
        "status": b.status,
        "deposit_usdc": svc.deposit_cents / 100,
        "balance_due_usdc": (svc.price_cents - svc.deposit_cents) / 100,
        "expires_at": b.expires_at.isoformat(timespec="seconds"),
        "signature": b.signature,
        "refund_needed": b.status == EXPIRED and b.signature is not None,
    }
    if b.status == PENDING:
        out["payment_url"] = solana.payment_url(
            svc.deposit_cents, b.reference, "Booking deposit", f"Deposit for {svc.name}"
        )
    return out


# ---------- public API ----------
@app.get("/api/config")
def public_config():
    """Lets the page show demo-only controls (like 'Simulate payment') in mock mode."""
    return {"mock_payments": config.MOCK_PAYMENTS}


@app.get("/api/services")
def list_services(s: Session = Depends(get_session)):
    return s.exec(select(Service)).all()


@app.get("/api/slots")
def list_slots(service_id: int, day: date, s: Session = Depends(get_session)):
    svc = s.get(Service, service_id)
    if not svc:
        raise HTTPException(404, "Service not found")
    expire_stale(s)
    slots, t = [], datetime.combine(day, datetime.min.time()).replace(hour=config.OPEN_HOUR)
    close = t.replace(hour=config.CLOSE_HOUR)
    step = timedelta(minutes=svc.duration_min)
    while t + step <= close:
        if t > datetime.now() and not slot_taken(s, t, t + step):
            slots.append(t.isoformat(timespec="minutes"))
        t += step
    return slots


class BookingIn(BaseModel):
    service_id: int
    start: datetime
    customer_name: str
    customer_email: str


@app.post("/api/bookings", status_code=201)
def create_booking(body: BookingIn, s: Session = Depends(get_session)):
    svc = s.get(Service, body.service_id)
    if not svc:
        raise HTTPException(404, "Service not found")
    start = body.start.replace(tzinfo=None)
    end = start + timedelta(minutes=svc.duration_min)
    if start <= datetime.now():
        raise HTTPException(400, "Start time must be in the future")
    if not (config.OPEN_HOUR <= start.hour and end <= start.replace(hour=config.CLOSE_HOUR, minute=0)):
        raise HTTPException(400, "Outside opening hours")
    with booking_lock:  # check + insert must happen together or two people can grab the same slot
        expire_stale(s)
        if slot_taken(s, start, end):
            raise HTTPException(409, "That slot is already taken")
        b = Booking(
            service_id=svc.id,
            start=start,
            end=end,
            customer_name=body.customer_name.strip(),
            customer_email=body.customer_email.strip(),
            reference=solana.new_reference(),
            expires_at=datetime.now() + timedelta(minutes=config.HOLD_MINUTES),
        )
        s.add(b)
        s.commit()
        s.refresh(b)
    return to_out(b, s)


@app.get("/api/bookings/{booking_id}")
def get_booking(booking_id: int, s: Session = Depends(get_session)):
    expire_stale(s)
    b = s.get(Booking, booking_id)
    if not b:
        raise HTTPException(404, "Booking not found")
    return to_out(b, s)


@app.post("/api/bookings/{booking_id}/verify")
def verify_payment(booking_id: int, s: Session = Depends(get_session)):
    """Check the chain (never the browser) and mark the deposit as paid."""
    expire_stale(s)
    b = s.get(Booking, booking_id)
    if not b:
        raise HTTPException(404, "Booking not found")
    expired_msg = "Booking expired, please book again"
    # Expired bookings are still checked: the customer may have paid just before the hold ended.
    if config.MOCK_PAYMENTS or b.status not in (PENDING, EXPIRED) or (b.status == EXPIRED and b.signature):
        if b.status == EXPIRED:
            raise HTTPException(410, expired_msg)
        return to_out(b, s)
    svc = s.get(Service, b.service_id)
    try:
        sig = solana.find_payment(b.reference, svc.deposit_cents)
    except Exception as e:  # network / RPC problem
        raise HTTPException(502, f"Could not reach Solana RPC: {e}")
    if not sig:
        if b.status == EXPIRED:
            raise HTTPException(410, expired_msg)
        return to_out(b, s)
    with booking_lock:
        other = s.exec(select(Booking).where(Booking.signature == sig, Booking.id != b.id)).first()
        if other:  # one transaction can only pay for one booking
            raise HTTPException(409, "This payment was already used for another booking")
        b.signature = sig
        if b.status == EXPIRED and slot_taken(s, b.start, b.end):
            # Paid too late and someone else took the slot. Keep the signature so the owner can refund.
            s.add(b)
            s.commit()
            raise HTTPException(409, "Your payment arrived after the hold expired and the slot was taken. The shop will refund you.")
        b.status = DEPOSITED
        s.add(b)
        s.commit()
    return to_out(b, s)


@app.post("/api/dev/mock-pay/{booking_id}")
def mock_pay(booking_id: int, s: Session = Depends(get_session)):
    """Demo/test only: pretend the deposit arrived. Disabled unless MOCK_PAYMENTS=1."""
    if not config.MOCK_PAYMENTS:
        raise HTTPException(404, "Not found")
    b = s.get(Booking, booking_id)
    if not b or b.status != PENDING:
        raise HTTPException(400, "Booking is not pending")
    b.status, b.signature = DEPOSITED, f"mock-{b.reference[:12]}"
    s.add(b)
    s.commit()
    return to_out(b, s)


# ---------- owner API ----------
@app.get("/api/owner/bookings", dependencies=[Depends(require_owner)])
def owner_bookings(s: Session = Depends(get_session)):
    expire_stale(s)
    rows = s.exec(select(Booking).order_by(Booking.start)).all()
    return [to_out(b, s) for b in rows]


def _owner_action(booking_id: int, new_status: str, s: Session):
    b = s.get(Booking, booking_id)
    if not b:
        raise HTTPException(404, "Booking not found")
    if b.status != DEPOSITED:
        raise HTTPException(409, f"Only deposited bookings can be changed (now: {b.status})")
    b.status = new_status
    s.add(b)
    s.commit()
    return to_out(b, s)


@app.post("/api/owner/bookings/{booking_id}/complete", dependencies=[Depends(require_owner)])
def complete(booking_id: int, s: Session = Depends(get_session)):
    """Customer showed up: the deposit counts toward the bill, only the balance is left."""
    return _owner_action(booking_id, COMPLETED, s)


@app.post("/api/owner/bookings/{booking_id}/no-show", dependencies=[Depends(require_owner)])
def no_show(booking_id: int, s: Session = Depends(get_session)):
    """Customer did not come: the shop keeps the deposit."""
    return _owner_action(booking_id, NO_SHOW, s)


@app.get("/")
def home():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
