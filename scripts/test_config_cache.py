"""CONFIG CACHE — correctness first, speed second (v12 phase 1).

load_config() runs on every request plus ~70 other call sites, re-parsing the
whole config.json each time. This caches it against a (mtime_ns, size, inode)
stamp. The dangerous failure is NOT slowness — it is a stale read: a password
change, a permission grant or an SMTP edit that the next request doesn't see.
Every test below is about invalidation; the speed check comes last.

Runs against a temp config file; the real config.json is never touched.
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="cfgcache_"))
config.CONFIG_PATH = _tmp / "config.json"
config.invalidate_config_cache()

_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


# === 1. No file at all — the login-free dev case ===========================
check("missing config.json returns the defaults",
      config.load_config()["app_name"] == "Finance Team Toolkit"
      and config.load_config()["auth"]["enabled"] is False)
check("the no-file result is itself cached (same object back)",
      config.load_config() is config.load_config())

# === 2. Creating the file invalidates ======================================
config.CONFIG_PATH.write_text(json.dumps({"organization": "DHL Cameroon"}),
                              encoding="utf-8")
check("a config.json appearing is picked up immediately",
      config.load_config()["organization"] == "DHL Cameroon")
check("defaults still merge under the user values",
      config.load_config()["app_name"] == "Finance Team Toolkit")

# === 3. A write through write_config_file is seen at once ==================
config.write_config_file({"organization": "DHL Nigeria", "banks": ["GTBank"]})
check("write_config_file result is visible on the very next read",
      config.load_config()["organization"] == "DHL Nigeria"
      and config.load_config()["banks"] == ["GTBank"])

# === 4. save_user_config (deep-merge under the lock) ========================
config.save_user_config({"smtp": {"host": "smtp.example.com"}})
cfg = config.load_config()
check("save_user_config is visible immediately and merges deeply",
      cfg["smtp"]["host"] == "smtp.example.com"
      and cfg["smtp"]["port"] == 587           # default preserved
      and cfg["organization"] == "DHL Nigeria")  # prior value preserved

# === 5. The hard one: two writes inside the same filesystem tick ===========
# os.replace gives a NEW inode every write, so the stamp changes even when
# mtime_ns and size are identical. Without the inode this test fails.
for i in range(40):
    config.write_config_file({"organization": f"tick-{i}", "banks": ["X"]})
    got = config.load_config()["organization"]
    if got != f"tick-{i}":
        check(f"rapid successive writes are never stale (failed at {i}: {got})",
              False)
        break
else:
    check("40 back-to-back writes each visible on the next read (no stale)",
          True)

# === 6. An out-of-band edit (someone hand-edits the file) ==================
config.load_config()
config.CONFIG_PATH.write_text(json.dumps({"organization": "hand-edited"}),
                              encoding="utf-8")
check("an out-of-band edit is detected via the stamp",
      config.load_config()["organization"] == "hand-edited")

# === 7. A broken file never takes the app down, and recovery is instant ====
config.CONFIG_PATH.write_text("{ this is not json", encoding="utf-8")
check("a corrupt config falls back to defaults instead of raising",
      config.load_config()["app_name"] == "Finance Team Toolkit")
config.CONFIG_PATH.write_text(json.dumps({"organization": "recovered"}),
                              encoding="utf-8")
check("fixing the corrupt file is picked up at once",
      config.load_config()["organization"] == "recovered")

# === 8. Deleting the file falls back to defaults ===========================
config.CONFIG_PATH.unlink()
check("deleting config.json falls back to defaults",
      config.load_config()["organization"] == "DHL Finance Team")
config.CONFIG_PATH.write_text(json.dumps({"organization": "back"}),
                              encoding="utf-8")
check("recreating it is picked up", config.load_config()["organization"] == "back")

# === 9. Concurrency — readers while a writer churns ========================
_errs, _stale = [], []


def _reader():
    try:
        for _ in range(300):
            c = config.load_config()
            # Whatever we get must be a coherent merged config, never a
            # half-built dict.
            if "app_name" not in c or "auth" not in c or "smtp" not in c:
                _stale.append(dict(c))
    except Exception as exc:  # noqa: BLE001
        _errs.append(repr(exc))


def _writer():
    try:
        for i in range(60):
            config.write_config_file({"organization": f"w{i}"})
    except Exception as exc:  # noqa: BLE001
        _errs.append(repr(exc))


ts = [threading.Thread(target=_reader) for _ in range(6)] + \
     [threading.Thread(target=_writer) for _ in range(2)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("concurrent readers + writers: no errors, never a partial config",
      not _errs and not _stale)

# === 10. The point of the exercise: a cache hit does not re-parse ==========
big = {"organization": "DHL", "auth": {"users": {
    f"user{i:04d}": "pbkdf2$" + "x" * 120 for i in range(1000)}}}
config.write_config_file(big)
config.load_config()                       # prime

# Assert BEHAVIOUR (parses actually skipped), not wall-clock — a timing
# threshold flakes when the machine is busy running the rest of the suite.
before = config.config_parse_count()
t0 = time.perf_counter()
for _ in range(200):
    config.load_config()
cached_s = time.perf_counter() - t0
check("200 cache hits performed ZERO re-parses",
      config.config_parse_count() == before)

before = config.config_parse_count()
t0 = time.perf_counter()
for _ in range(200):
    config.invalidate_config_cache()
    config.load_config()
uncached_s = time.perf_counter() - t0
check("invalidating forces a real parse every time",
      config.config_parse_count() == before + 200)

print(f"   1000-user config: {uncached_s / 200 * 1e6:8.1f} us/parse -> "
      f"{cached_s / 200 * 1e6:6.1f} us/cache-hit "
      f"({uncached_s / max(cached_s, 1e-9):.0f}x)  [informational]")
check("the 1000-user config still reads back correctly",
      len(config.load_config()["auth"]["users"]) == 1000)

# === 11. Platform paths exist for the later phases =========================
check("PLATFORM_DIR / TENANTS_DIR / IDENTITY_DB are defined",
      config.PLATFORM_DIR.name == "platform"
      and config.TENANTS_DIR.name == "tenants"
      and config.IDENTITY_DB.name == "identity.db")
check("they sit under data/, not inside a country's tree",
      config.PLATFORM_DIR.parent == config.DATA_DIR
      and config.TENANTS_DIR.parent == config.DATA_DIR)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL CONFIG-CACHE TESTS PASSED")
