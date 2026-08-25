#!/usr/bin/env python3
"""
QuantumPOS license key generator.

Just edit the CONFIG block below and run:
    python licence.py

Keys go into the [Serials] table and are validated by a plain DB lookup, so a
valid key is any unique string inserted for the right coid/branch with an
ExpiryDate. This writes ready-to-run SQL using your dbo.SaveKey procedure.
"""

import secrets
from datetime import date

# ============================================================================
#  CONFIG  -- edit these, then run the file
# ============================================================================

COID   = 412            # Company ID
BRANCH = "main"         # Branch name the keys are bound to (must match your setup)
COUNT  = 5              # How many keys to generate

# ---- Expiry date -----------------------------------------------------------
# Option A: set an exact date. Leave as "" to use the relative option instead.
EXPIRY_DATE = ""        # e.g. "2027-06-30"  (format: YYYY-MM-DD)

# Option B: used only when EXPIRY_DATE above is "".  Expire this far from today.
EXPIRY_YEARS  = 1
EXPIRY_MONTHS = 0
EXPIRY_DAYS   = 0

# ---- Output ----------------------------------------------------------------
MODE     = "savekey"    # "savekey" = EXEC dbo.SaveKey  |  "insert" = direct INSERT (sets used=0)
OUT_FILE = "license_keys.sql"

# ---- Key appearance --------------------------------------------------------
# Your app has 5 boxes of 4 characters, e.g.  7500 1280 1520 4804 Z124
PREFIX    = ""          # no prefix
GROUPS    = 5           # number of boxes
GROUP_LEN = 4           # characters per box  -> 5x4 = 20 chars

# How the groups are joined in the SerialKey that gets STORED in the database.
# This must match how your app builds the string from the 5 boxes when it
# checks the key. " " (a space) matches how you wrote the example above.
# If the app rejects a valid key, change this to "" (boxes joined with nothing).
SEPARATOR = " "

# ============================================================================
#  You normally don't need to touch anything below here.
# ============================================================================

# Crockford-style alphabet, ambiguous chars removed (no I, L, O, U, 0, 1)
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"


def add_period(d: date, years: int, months: int, days: int) -> date:
    """Add years/months/days to a date without any external library."""
    total_months = d.year * 12 + (d.month - 1) + years * 12 + months
    y, m = divmod(total_months, 12)
    m += 1
    # clamp day to the last valid day of the target month
    if m == 2:
        last = 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28
    elif m in (4, 6, 9, 11):
        last = 30
    else:
        last = 31
    result = date(y, m, min(d.day, last))
    return date.fromordinal(result.toordinal() + days)


def make_key() -> str:
    body = SEPARATOR.join(
        "".join(secrets.choice(ALPHABET) for _ in range(GROUP_LEN))
        for _ in range(GROUPS)
    )
    return f"{PREFIX}{SEPARATOR}{body}" if PREFIX else body


def sql_escape(s: str) -> str:
    return s.replace("'", "''")


def main() -> None:
    # Resolve expiry
    if EXPIRY_DATE.strip():
        expiry = date.fromisoformat(EXPIRY_DATE.strip())
    else:
        expiry = add_period(date.today(), EXPIRY_YEARS, EXPIRY_MONTHS, EXPIRY_DAYS)
    expiry_str = expiry.isoformat()  # 'YYYY-MM-DD'

    # Generate unique keys
    keys, seen = [], set()
    while len(keys) < COUNT:
        k = make_key()
        if k not in seen:
            seen.add(k)
            keys.append(k)

    b = sql_escape(BRANCH)
    lines = [
        "-- QuantumPOS license keys",
        f"-- coid={COID}  branch='{BRANCH}'  expiry={expiry_str}  count={COUNT}",
        "-- Generated keys are unused; the client sets used=1 on activation.",
        "",
    ]

    if MODE == "savekey":
        for k in keys:
            lines.append(
                f"EXEC dbo.SaveKey @serialkey = '{sql_escape(k)}', "
                f"@expdate = '{expiry_str}', @coid = {COID}, @branch = '{b}';"
            )
    else:  # direct insert, explicitly marks the key unused
        lines.append(
            "INSERT INTO Serials (SerialKey, used, ExpiryDate, Uploaded, coid, branch) VALUES"
        )
        rows = [
            f"  ('{sql_escape(k)}', 0, '{expiry_str}', 0, {COID}, '{b}')"
            for k in keys
        ]
        lines.append(",\n".join(rows) + ";")

    with open(OUT_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Generated {len(keys)} key(s) for coid={COID} branch='{BRANCH}' "
          f"expiring {expiry_str}\n")
    for k in keys:
        print("  " + k)
    print(f"\nSQL written to: {OUT_FILE}")


if __name__ == "__main__":
    main()