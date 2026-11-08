# Booking Deposits on Solana

Small service businesses lose money when customers don't show up. This app asks for a
small **USDC deposit on Solana** to hold a time slot. If the customer comes, the deposit
counts toward the bill. If they don't, the shop keeps it.

Built for the MLH Solana Hackathon (FastAPI backend + Solana Pay).

## How it works

```
customer picks slot -> POST /api/bookings      (slot held 15 min, status: pending)
                    -> Solana Pay link / QR     (unique reference key per booking)
wallet pays USDC    -> POST /api/bookings/{id}/verify
                       server asks the Solana RPC for transactions that mention the
                       reference, checks the shop wallet's USDC balance went up
                    -> status: deposited
owner               -> complete (showed up)  or  no-show (keeps deposit)
```

States: `pending -> deposited -> completed | no_show`, and `pending -> expired`.

Why Solana: a deposit of a few dollars is only practical when the fee is a fraction of a
cent and it settles in about a second.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn sqlmodel httpx base58 pytest

# Demo without a wallet (fake payments):
MOCK_PAYMENTS=1 MERCHANT_WALLET=11111111111111111111111111111111 OWNER_KEY=secret \
  uvicorn app.main:app --reload
# open http://127.0.0.1:8000  (API docs: /docs)
# in mock mode, click "Simulate payment (demo mode)" on the payment screen

# Real devnet payments:
MERCHANT_WALLET=<your devnet wallet address> OWNER_KEY=secret uvicorn app.main:app --reload
```

For real payments: create a devnet wallet (Phantom -> Developer settings -> Testnet mode ->
Solana Devnet), get devnet SOL and devnet USDC from the Circle faucet (faucet.circle.com),
and put your wallet address in `MERCHANT_WALLET`. Your wallet needs a USDC token account;
receiving some devnet USDC first creates it.

Run the tests: `python -m pytest -q tests`

## Design decisions

- **Server-side verification.** The browser never says "paid". The backend checks the chain.
- **Unique reference per booking.** Lets us find the exact payment without asking users for anything.
- **Replay protection.** A transaction signature can only pay for one booking (unique column).
- **No double booking.** The overlap check and the insert run under one lock. Pending bookings
  hold the slot for 15 minutes, then expire so slots are not blocked forever.
- **Money as integer cents**, never floats, to avoid rounding bugs.

## Known limits

- Deposits go straight to the shop wallet. There is no on-chain escrow, so refunds are manual.
  Next step: an Anchor escrow program that releases or forfeits the deposit.
- The lock is in-process and SQLite is single-file. For production use Postgres with an
  exclusion constraint on the time range, and a background job for expiry.
- One shop, fixed opening hours (9-17), no time zones, no auth for customers.
- Owner login is a single shared key. Replace with real auth.
