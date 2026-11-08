import os
import tempfile
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["MOCK_PAYMENTS"] = "1"
os.environ["MERCHANT_WALLET"] = "11111111111111111111111111111111"
os.environ["OWNER_KEY"] = "secret"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

OWNER = {"X-Owner-Key": "secret"}


def tomorrow_at(hour):
    d = datetime.now() + timedelta(days=1)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0)


def book(c, hour=10, name="Mai"):
    return c.post(
        "/api/bookings",
        json={"service_id": 1, "start": tomorrow_at(hour).isoformat(), "customer_name": name, "customer_email": "a@b.co"},
    )


def test_full_flow_and_double_booking():
    with TestClient(app) as c:
        r = book(c)
        assert r.status_code == 201
        b = r.json()
        assert b["status"] == "pending" and b["payment_url"].startswith("solana:")
        assert b["deposit_usdc"] == 10 and b["balance_due_usdc"] == 35

        # same slot (even overlapping) is refused while the hold is active
        assert book(c, 10, "Other").status_code == 409

        # slot disappears from the available list
        day = tomorrow_at(10).date().isoformat()
        slots = c.get(f"/api/slots?service_id=1&day={day}").json()
        assert tomorrow_at(10).isoformat(timespec="minutes") not in slots

        # pay (mock) then owner actions need the key
        assert c.post(f"/api/dev/mock-pay/{b['id']}").json()["status"] == "deposited"
        assert c.post(f"/api/owner/bookings/{b['id']}/complete").status_code == 401
        assert c.post(f"/api/owner/bookings/{b['id']}/complete", headers=OWNER).json()["status"] == "completed"
        # cannot change it twice
        assert c.post(f"/api/owner/bookings/{b['id']}/no-show", headers=OWNER).status_code == 409


def test_no_show_and_unpaid_cannot_be_actioned():
    with TestClient(app) as c:
        b = book(c, 13).json()
        assert c.post(f"/api/owner/bookings/{b['id']}/no-show", headers=OWNER).status_code == 409  # not paid yet
        c.post(f"/api/dev/mock-pay/{b['id']}")
        assert c.post(f"/api/owner/bookings/{b['id']}/no-show", headers=OWNER).json()["status"] == "no_show"
        assert len(c.get("/api/owner/bookings", headers=OWNER).json()) >= 1


def test_expiry_frees_slot_and_validation():
    from sqlmodel import Session

    from app.main import Booking, engine

    with TestClient(app) as c:
        b = book(c, 15).json()
        with Session(engine) as s:  # force the hold to be in the past
            row = s.get(Booking, b["id"])
            row.expires_at = datetime.now() - timedelta(minutes=1)
            s.add(row)
            s.commit()
        assert c.get(f"/api/bookings/{b['id']}").json()["status"] == "expired"
        assert book(c, 15, "New").status_code == 201  # slot is free again

        past = {"service_id": 1, "start": (datetime.now() - timedelta(days=1)).isoformat(), "customer_name": "x", "customer_email": "x@y.z"}
        assert c.post("/api/bookings", json=past).status_code == 400
        assert book(c, 20).status_code == 400  # outside opening hours


def test_solana_balance_math():
    from app import config, solana

    tx = {"meta": {
        "preTokenBalances": [{"owner": config.MERCHANT_WALLET, "mint": config.USDC_MINT, "uiTokenAmount": {"uiAmountString": "5"}}],
        "postTokenBalances": [{"owner": config.MERCHANT_WALLET, "mint": config.USDC_MINT, "uiTokenAmount": {"uiAmountString": "15.5"}}],
    }}
    assert float(solana._usdc_received(tx)) == 10.5
    assert len(solana.new_reference()) >= 32


def _expire(booking_id):
    from sqlmodel import Session

    from app.main import Booking, engine

    with Session(engine) as s:
        row = s.get(Booking, booking_id)
        row.expires_at = datetime.now() - timedelta(minutes=1)
        s.add(row)
        s.commit()


def test_late_payment_is_kept_or_flagged_for_refund(monkeypatch):
    from app import config, solana

    monkeypatch.setattr(config, "MOCK_PAYMENTS", False)
    with TestClient(app) as c:
        # paid just before the hold ended, checked after: booking is revived
        b = book(c, 11).json()
        _expire(b["id"])
        monkeypatch.setattr(solana, "find_payment", lambda ref, amt: "sig-late-1")
        r = c.post(f"/api/bookings/{b['id']}/verify")
        assert r.status_code == 200 and r.json()["status"] == "deposited"

        # paid late and someone else already took the slot: flagged for refund
        b2 = book(c, 14).json()
        _expire(b2["id"])
        assert book(c, 14, "Other").status_code == 201
        monkeypatch.setattr(solana, "find_payment", lambda ref, amt: "sig-late-2")
        assert c.post(f"/api/bookings/{b2['id']}/verify").status_code == 409
        row = c.get(f"/api/bookings/{b2['id']}").json()
        assert row["status"] == "expired" and row["refund_needed"]

        # the same transaction cannot pay for a second booking
        b3 = book(c, 16).json()
        monkeypatch.setattr(solana, "find_payment", lambda ref, amt: "sig-late-1")
        assert c.post(f"/api/bookings/{b3['id']}/verify").status_code == 409

        # expired and never paid: still 410
        b4 = book(c, 9).json()
        _expire(b4["id"])
        monkeypatch.setattr(solana, "find_payment", lambda ref, amt: None)
        assert c.post(f"/api/bookings/{b4['id']}/verify").status_code == 410


def test_config_exposes_mock_mode(monkeypatch):
    from app import config

    with TestClient(app) as c:
        assert c.get("/api/config").json() == {"mock_payments": True}
        monkeypatch.setattr(config, "MOCK_PAYMENTS", False)
        assert c.get("/api/config").json() == {"mock_payments": False}
