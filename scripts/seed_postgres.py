"""Seed a demo payment_events table in PostgreSQL with simulated journeys,
so the Studio's database connector has something real to learn from.

Run: python scripts/seed_postgres.py [dsn]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text

from fingerprints.simulate import simulate_payments

DSN = sys.argv[1] if len(sys.argv) > 1 else \
    "postgresql://postgres:admin123@localhost:5432/postgres"

DDL = """
DROP TABLE IF EXISTS payment_events;
CREATE TABLE payment_events (
    uetr            uuid            NOT NULL,
    ts              timestamp(3)    NOT NULL,
    service         varchar(32)     NOT NULL,
    status          varchar(16)     NOT NULL,
    msg_type        varchar(16),
    rail            varchar(16),
    amount          numeric(14,2),
    currency        char(3),
    debtor_account  varchar(34),
    debtor_name     varchar(64),
    country         char(2),
    latency_ms      integer
);
CREATE INDEX ix_payment_events_uetr ON payment_events (uetr);
"""

INSERT = text("""
INSERT INTO payment_events VALUES
(:uetr, :ts, :service, :status, :msg_type, :rail, :amount, :currency,
 :debtor_account, :debtor_name, :country, :latency_ms)
""")


REL_DDL = """
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS customers;
CREATE TABLE customers (
    customer_id  varchar(12) PRIMARY KEY,
    full_name    varchar(64),
    segment      varchar(16),
    country      char(2),
    joined       date
);
CREATE TABLE accounts (
    account_id   varchar(14) PRIMARY KEY,
    customer_id  varchar(12) NOT NULL REFERENCES customers(customer_id),
    iban         varchar(34),
    acct_type    varchar(12),
    currency     char(3),
    balance      numeric(14,2)
);
"""


def seed_relational(engine):
    import random
    from fingerprints.simulate import FIRST, LAST
    rng = random.Random(7)
    customers, accounts = [], []
    for i in range(800):
        cid = f"CUST{i:06d}"
        customers.append({
            "customer_id": cid,
            "full_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "segment": rng.choices(["RETAIL", "PRIORITY", "CORPORATE"],
                                   weights=[7, 2, 1])[0],
            "country": rng.choices(["SG", "MY", "IN", "HK"], weights=[6, 2, 1, 1])[0],
            "joined": f"20{rng.randint(15, 25)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        })
        for j in range(rng.choices([1, 2, 3], weights=[5, 3, 2])[0]):
            accounts.append({
                "account_id": f"ACCT{i:06d}{j}",
                "customer_id": cid,
                "iban": "SG%02d%s%010d" % (rng.randrange(100),
                                           rng.choice(["OCBC", "DBSS", "UOBV"]),
                                           rng.randrange(10**10)),
                "acct_type": rng.choices(["SAVINGS", "CURRENT", "FIXED"],
                                         weights=[5, 4, 1])[0],
                "currency": rng.choices(["SGD", "USD", "MYR"], weights=[7, 2, 1])[0],
                "balance": round(rng.lognormvariate(8.5, 1.4), 2),
            })
    with engine.begin() as conn:
        for stmt in REL_DDL.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        conn.execute(text(
            "INSERT INTO customers VALUES (:customer_id, :full_name, :segment, "
            ":country, :joined)"), customers)
        conn.execute(text(
            "INSERT INTO accounts VALUES (:account_id, :customer_id, :iban, "
            ":acct_type, :currency, :balance)"), accounts)
    return len(customers), len(accounts)


def main():
    records, _ = simulate_payments(n_journeys=2000, seed=42)
    engine = create_engine(DSN)
    with engine.begin() as conn:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        for i in range(0, len(records), 1000):
            conn.execute(INSERT, records[i:i + 1000])
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM payment_events")).scalar()
    print(f"seeded payment_events: {n} rows at {DSN.split('@')[-1]}")
    nc, na = seed_relational(engine)
    print(f"seeded customers: {nc} rows, accounts: {na} rows (FK -> customers)")


if __name__ == "__main__":
    main()
