#!/usr/bin/env python3
"""Nightly sandbox reset: back up data.db (keep 7), wipe items, reseed demo rows.
Run as www-data via /etc/cron.d/testapi-reset. Stdlib only."""
import os
import glob
import time
import shutil
import sqlite3

DB = os.environ.get("API_DB", "/opt/testapi/data.db")
BK = os.path.join(os.path.dirname(DB), "backups")

os.makedirs(BK, exist_ok=True)
if os.path.exists(DB):
    shutil.copy(DB, os.path.join(BK, "data-" + time.strftime("%Y%m%d-%H%M%S") + ".db"))
for old in sorted(glob.glob(os.path.join(BK, "data-*.db")))[:-7]:
    try:
        os.remove(old)
    except OSError:
        pass

# autocommit (isolation_level=None) so VACUUM can run outside a transaction
con = sqlite3.connect(DB, timeout=10, isolation_level=None)
con.execute("DELETE FROM items")
for stmt in ("DELETE FROM sqlite_sequence WHERE name='items'",
             "DELETE FROM idempotency"):
    try:
        con.execute(stmt)
    except sqlite3.OperationalError:
        pass
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
con.executemany("INSERT INTO items(name, qty, tags, created, updated) VALUES(?,?,?,?,?)",
                [("sample-widget", 5, '["demo"]', ts, ts),
                 ("sample-gadget", 2, "[]", ts, ts)])
con.execute("VACUUM")
con.close()
print("reset OK", ts)
