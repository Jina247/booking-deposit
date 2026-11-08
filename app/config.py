import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bookings.db")

# Solana (devnet by default). The shop's wallet receives the USDC deposits.
RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
MERCHANT_WALLET = os.getenv("MERCHANT_WALLET", "")
# Devnet USDC mint (Circle). Use the mainnet mint only when you really go live.
USDC_MINT = os.getenv("USDC_MINT", "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU")

# MOCK_PAYMENTS=1 lets you demo and test without a real wallet or network.
MOCK_PAYMENTS = os.getenv("MOCK_PAYMENTS", "0") == "1"

OWNER_KEY = os.getenv("OWNER_KEY", "change-me")

HOLD_MINUTES = 15  # how long an unpaid booking holds the slot
OPEN_HOUR = 9
CLOSE_HOUR = 17
