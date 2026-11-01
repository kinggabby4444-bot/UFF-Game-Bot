"""
dbtiming.py - DIAGNOSTICS ONLY. It measures; it never changes what the bot does.

Everything it prints starts with "DIAG" so you can type DIAG into the search box
of the Render log viewer.

What it measures (printed once a minute, plus instant warnings for slow things):
  * every database call: time to get a connection, time per SQL statement,
    time for commit/rollback, total time, and which db.py function it was
  * how many NEW database connections were opened (connection churn)
  * a raw "SELECT 1" round trip on a separate connection (pure network speed)
  * CPU used by the process, and whether the container is being CPU-throttled
  * how late the event loop runs (and, if it stalls, WHERE it is stuck)
  * for every Telegram update: how long it waited in the bot's queue, how long
    it took to process, how old the message already was when it arrived
  * which of my earlier patches are actually active in your running files

Must be imported BEFORE main.py / database.py (server.py does this for you).
"""
import asyncio
import logging
import os
import socket
import sys
import threading
import time
import traceback
from collections import defaultdict
from urllib.parse import urlparse

import psycopg2

log = logging.getLogger("dbtiming")

INTERVAL = int(os.environ.get("DIAG_INTERVAL", "60"))        # seconds between summaries
SLOW_EXEC = float(os.environ.get("DIAG_SLOW_EXEC", "1.0"))   # warn if ONE sql statement takes longer
SLOW_CALL = float(os.environ.get("DIAG_SLOW_CALL", "3.0"))   # warn if one whole db call takes longer

_mu = threading.Lock()
_in_use = 0
_database = None
_upd = {}


def _new_window():
    return {
        "calls": 0, "call_sum": 0.0, "call_max": 0.0,
        "co_sum": 0.0, "co_max": 0.0,
        "stmts": 0, "ex_sum": 0.0, "ex_max": 0.0,
        "tx_sum": 0.0, "tx_max": 0.0,
        "pg": 0, "pg_sum": 0.0, "pg_max": 0.0,
        "peak": 0,
        "fn": defaultdict(lambda: [0, 0.0, 0.0]),   # name -> [count, total_seconds, max_seconds]
    }


_w = _new_window()


def _ms(seconds):
    return f"{seconds * 1000:.0f}ms"


# ---------------------------------------------------------------------------
# 1) count + time every NEW connection that psycopg2 opens
# ---------------------------------------------------------------------------
_orig_pg_connect = psycopg2.connect


def _timed_pg_connect(*a, **k):
    t = time.monotonic()
    try:
        return _orig_pg_connect(*a, **k)
    finally:
        dt = time.monotonic() - t
        with _mu:
            _w["pg"] += 1
            _w["pg_sum"] += dt
            _w["pg_max"] = max(_w["pg_max"], dt)


if not getattr(psycopg2, "_dbtiming_patched", False):
    psycopg2.connect = _timed_pg_connect
    psycopg2._dbtiming_patched = True


# ---------------------------------------------------------------------------
# 2) wrappers that time one database call (connect() ... close())
# ---------------------------------------------------------------------------
class _TimedCursor:
    def __init__(self, cur, owner):
        self._cur = cur
        self._o = owner

    def _timed(self, fn, *a, **k):
        t = time.monotonic()
        try:
            return fn(*a, **k)
        finally:
            self._o._add_exec(time.monotonic() - t)

    def execute(self, *a, **k):
        return self._timed(self._cur.execute, *a, **k)

    def executemany(self, *a, **k):
        return self._timed(self._cur.executemany, *a, **k)

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _TimedConn:
    def __init__(self, conn, fn, t0, checkout):
        self._c = conn
        self._fn = fn
        self._t0 = t0
        self._co = checkout
        self._n = 0
        self._ex = 0.0
        self._ex_max = 0.0
        self._tx = 0.0
        self._closed = False

    def _add_exec(self, dt):
        self._n += 1
        self._ex += dt
        if dt > self._ex_max:
            self._ex_max = dt
        if dt > SLOW_EXEC:
            log.warning("DIAG slow SQL statement: %.2fs inside db.%s()", dt, self._fn)

    def cursor(self, *a, **k):
        return _TimedCursor(self._c.cursor(*a, **k), self)

    def commit(self):
        t = time.monotonic()
        try:
            return self._c.commit()
        finally:
            self._tx += time.monotonic() - t

    def rollback(self):
        t = time.monotonic()
        try:
            return self._c.rollback()
        finally:
            self._tx += time.monotonic() - t

    def close(self):
        if self._closed:
            return
        self._closed = True
        t = time.monotonic()
        try:
            self._c.close()
        finally:
            now = time.monotonic()
            self._tx += now - t
            _record(self, now - self._t0)

    def __getattr__(self, name):
        return getattr(self._c, name)


def _record(c, total):
    global _in_use
    with _mu:
        _in_use -= 1
        w = _w
        w["calls"] += 1
        w["call_sum"] += total
        w["call_max"] = max(w["call_max"], total)
        w["co_sum"] += c._co
        w["co_max"] = max(w["co_max"], c._co)
        w["stmts"] += c._n
        w["ex_sum"] += c._ex
        w["ex_max"] = max(w["ex_max"], c._ex_max)
        w["tx_sum"] += c._tx
        w["tx_max"] = max(w["tx_max"], c._tx)
        f = w["fn"][c._fn]
        f[0] += 1
        f[1] += total
        f[2] = max(f[2], total)
    if total > SLOW_CALL:
        log.warning(
            "DIAG slow db call: db.%s() took %.2fs total = %.2fs waiting for a connection + "
            "%.2fs in %d SQL statement(s) + %.2fs commit/rollback/close",
            c._fn, total, c._co, c._ex, c._n, c._tx,
        )


def install(database, game_main=None):
    """Wraps database.connect so every db.py function is timed. Safe to call once."""
    global _database
    _database = database
    orig = database.connect
    if getattr(orig, "_dbtiming", False):
        return

    def timed_connect():
        global _in_use
        fn = sys._getframe(1).f_code.co_name      # which db.py function asked for the connection
        t0 = time.monotonic()
        try:
            raw = orig()
        except BaseException as e:
            log.warning("DIAG getting a db connection FAILED in db.%s() after %.1fs: %r",
                        fn, time.monotonic() - t0, e)
            raise
        t1 = time.monotonic()
        with _mu:
            _in_use += 1
            if _in_use > _w["peak"]:
                _w["peak"] = _in_use
        return _TimedConn(raw, fn, t0, t1 - t0)

    timed_connect._dbtiming = True
    database.connect = timed_connect

    # Which of the earlier patches are really live in the deployed files?
    status = [
        "database.py semaphore patch: " + ("YES" if hasattr(database, "_slots") else "NO"),
        "database.py idle-ping patch: " + ("YES" if hasattr(database, "_PING_AFTER_IDLE_SECONDS") else "NO"),
    ]
    if game_main is not None:
        status.append("main.py throttle patch: " + ("YES" if hasattr(game_main, "_throttle") else "NO"))
        status.append("main.py ChatMigrated patch: " + ("YES" if hasattr(game_main, "ChatMigrated") else "NO"))
    log.warning("DIAG patches in the running code -> " + " | ".join(status))
    log.warning("DIAG limits: GAME_DB_POOL_MAX=%s UPDATE_CONCURRENCY=%s BOT_THREADS=%s cpu_count=%s | %s",
                os.environ.get("GAME_DB_POOL_MAX"), os.environ.get("UPDATE_CONCURRENCY"),
                os.environ.get("BOT_THREADS"), os.cpu_count(), _cpu_limit_text())

    threading.Thread(target=_startup_probe, daemon=True, name="diag-probe").start()
    threading.Thread(target=_reporter, daemon=True, name="diag-report").start()


# ---------------------------------------------------------------------------
# 3) CPU / container limits
# ---------------------------------------------------------------------------
def _read_first(*paths):
    for p in paths:
        try:
            with open(p) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def _cpu_limit_text():
    v2 = _read_first("/sys/fs/cgroup/cpu.max")
    if v2:
        parts = v2.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]):
            return f"container CPU limit = {int(parts[0]) / int(parts[1]):.2f} cores (cpu.max={v2})"
        return f"container CPU limit: none (cpu.max={v2})"
    quota = _read_first("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period = _read_first("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota and period and quota.lstrip("-").isdigit() and period.isdigit() and int(quota) > 0:
        return f"container CPU limit = {int(quota) / int(period):.2f} cores"
    return "container CPU limit not readable from inside (check the instance type in Render)"


def _read_cgroup():
    for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
        try:
            d = {}
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[1].lstrip("-").isdigit():
                        d[parts[0]] = int(parts[1])
            if d:
                return d
        except OSError:
            continue
    return None


def _cg_text(prev, cur):
    if not prev or not cur:
        return "cgroup throttling stats not readable here"
    periods = cur.get("nr_periods", 0) - prev.get("nr_periods", 0)
    throttled = cur.get("nr_throttled", 0) - prev.get("nr_throttled", 0)
    if "throttled_usec" in cur:
        stalled = (cur["throttled_usec"] - prev.get("throttled_usec", 0)) / 1e6
    elif "throttled_time" in cur:
        stalled = (cur["throttled_time"] - prev.get("throttled_time", 0)) / 1e9
    else:
        stalled = None
    txt = f"CPU-throttled in {throttled} of {periods} scheduling periods"
    if stalled is not None:
        txt += f" ({stalled:.1f}s stalled in total)"
    return txt


# ---------------------------------------------------------------------------
# 4) raw network probe (separate connection, plain SELECT 1)
# ---------------------------------------------------------------------------
def _open_probe():
    c = _orig_pg_connect(os.environ["DATABASE_URL"], connect_timeout=10)
    c.autocommit = True
    return c


def _startup_probe():
    try:
        time.sleep(5)
        url = os.environ.get("DATABASE_URL", "")
        u = urlparse(url)
        host, port = u.hostname, u.port or 5432
        t = time.monotonic()
        socket.create_connection((host, port), timeout=10).close()
        tcp = time.monotonic() - t
        t = time.monotonic()
        c = _open_probe()
        conn_t = time.monotonic() - t
        cur = c.cursor()
        one = []
        for _ in range(8):
            t = time.monotonic()
            cur.execute("SELECT 1")
            cur.fetchone()
            one.append(time.monotonic() - t)
        c.autocommit = False
        t = time.monotonic()
        cur.execute("SELECT 1")
        cur.fetchone()
        c.rollback()
        in_tx = time.monotonic() - t
        c.close()
        log.warning(
            "DIAG db probe: host=%s port=%s | TCP connect=%s | full new connection (TLS+login)=%s | "
            "SELECT 1 round trip min/avg/max=%s/%s/%s | BEGIN+SELECT+ROLLBACK=%s "
            "(one normal db.py call = about this, plus commit)",
            host, port, _ms(tcp), _ms(conn_t), _ms(min(one)), _ms(sum(one) / len(one)), _ms(max(one)), _ms(in_tx),
        )
    except Exception as e:
        log.warning("DIAG db probe failed: %r", e)


# ---------------------------------------------------------------------------
# 5) per-update stats (called from server.py)
# ---------------------------------------------------------------------------
def observe_update(name, wait, proc, age, depth):
    with _mu:
        u = _upd.setdefault(name, {"n": 0, "wait_sum": 0.0, "wait_max": 0.0, "proc_sum": 0.0, "proc_max": 0.0,
                                   "age_n": 0, "age_sum": 0.0, "age_max": 0.0, "depth_max": 0})
        u["n"] += 1
        u["wait_sum"] += wait
        u["wait_max"] = max(u["wait_max"], wait)
        u["proc_sum"] += proc
        u["proc_max"] = max(u["proc_max"], proc)
        if age is not None:
            u["age_n"] += 1
            u["age_sum"] += age
            u["age_max"] = max(u["age_max"], age)
        u["depth_max"] = max(u["depth_max"], depth)


# ---------------------------------------------------------------------------
# 6) the once-a-minute report
# ---------------------------------------------------------------------------
def _reporter():
    global _w
    probe = None
    last_wall, last_cpu, last_cg = time.monotonic(), time.process_time(), _read_cgroup()
    while True:
        time.sleep(INTERVAL)
        try:
            with _mu:
                w, _w = _w, _new_window()
                upd = dict(_upd)
                _upd.clear()
                in_use = _in_use

            lines = []
            n = w["calls"]
            if n:
                stm = max(w["stmts"], 1)
                lines.append(
                    f"DIAG db last {INTERVAL}s: {n} calls ({n / INTERVAL:.1f}/s) | whole call avg={_ms(w['call_sum'] / n)} "
                    f"max={_ms(w['call_max'])} | waiting for a connection avg={_ms(w['co_sum'] / n)} max={_ms(w['co_max'])} | "
                    f"SQL statements={w['stmts']} avg={_ms(w['ex_sum'] / stm)} max={_ms(w['ex_max'])} | "
                    f"commit/rollback/close avg={_ms(w['tx_sum'] / n)} max={_ms(w['tx_max'])} | "
                    f"connections in use: peak={w['peak']} now={in_use}"
                )
                top = sorted(w["fn"].items(), key=lambda kv: -kv[1][1])[:8]
                lines.append("DIAG db busiest functions (calls, total s, avg, max): " + "; ".join(
                    f"{name} x{c} {t:.1f}s {_ms(t / c)} {_ms(m)}" for name, (c, t, m) in top))
            else:
                lines.append(f"DIAG db last {INTERVAL}s: no database calls")
            if w["pg"]:
                lines.append(f"DIAG db NEW connections opened: {w['pg']} (avg {_ms(w['pg_sum'] / w['pg'])}, "
                             f"max {_ms(w['pg_max'])})  <- should be ~0 once warmed up")
            try:
                p = _database._pool
                lines.append(f"DIAG db pool: used={len(p._used)} idle={len(p._pool)} max={p.maxconn}")
            except Exception:
                pass

            try:
                if probe is None:
                    probe = _open_probe()
                t = time.monotonic()
                cur = probe.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
                lines.append(f"DIAG idle SELECT 1 round trip right now: {_ms(time.monotonic() - t)} "
                             f"(network speed; compare with the SQL statement avg above)")
            except Exception as e:
                probe = None
                lines.append(f"DIAG idle probe failed: {e!r}")

            wall, cpu, cg = time.monotonic(), time.process_time(), _read_cgroup()
            used = (cpu - last_cpu) / max(wall - last_wall, 1e-9) * 100
            lines.append(f"DIAG cpu: this process used {used:.0f}% of one core | {_cg_text(last_cg, cg)} | "
                         f"threads={threading.active_count()}")
            last_wall, last_cpu, last_cg = wall, cpu, cg

            for name, u in sorted(upd.items()):
                txt = (f"DIAG {name} updates: {u['n']} processed | waited in the bot's queue avg={_ms(u['wait_sum'] / u['n'])} "
                       f"max={_ms(u['wait_max'])} | processing avg={_ms(u['proc_sum'] / u['n'])} max={_ms(u['proc_max'])} | "
                       f"max queue depth={u['depth_max']}")
                if u["age_n"]:
                    txt += (f" | message already {_ms(u['age_sum'] / u['age_n'])} old when it ARRIVED "
                            f"(max {_ms(u['age_max'])}; big = Telegram backlog)")
                lines.append(txt)

            for line in lines:
                log.warning(line)
        except Exception:
            log.exception("DIAG reporter failed (will try again)")


# ---------------------------------------------------------------------------
# 7) event-loop lag + "where is the loop stuck" (called from server.py)
# ---------------------------------------------------------------------------
_hb = {}
_stall_started = False
_stall_mu = threading.Lock()


def _stall_watcher():
    reported = {}
    while True:
        time.sleep(0.5)
        now = time.monotonic()
        for name, (t, ident) in list(_hb.items()):
            if now - t > 3.0 and reported.get(name) != t:
                reported[name] = t
                frame = sys._current_frames().get(ident)
                stack = "".join(traceback.format_stack(frame, limit=14)) if frame else "(no frame)"
                log.warning("DIAG %s: its event loop has not responded for %.1fs. Where its thread is right now "
                            "(if this says 'select' it is idle/starved, otherwise it is blocked in this code):\n%s",
                            name, now - t, stack)


async def loop_monitor(name, bot=None):
    global _stall_started
    with _stall_mu:
        if not _stall_started:
            _stall_started = True
            threading.Thread(target=_stall_watcher, daemon=True, name="diag-stall").start()
    ident = threading.get_ident()
    tick = 0.5
    n, total, worst = 0, 0.0, 0.0
    next_report = time.monotonic() + INTERVAL
    _hb[name] = (time.monotonic(), ident)
    while True:
        t = time.monotonic()
        await asyncio.sleep(tick)
        now = time.monotonic()
        _hb[name] = (now, ident)
        lag = max(0.0, now - t - tick)
        n += 1
        total += lag
        worst = max(worst, lag)
        if lag > 2.0:
            log.warning("DIAG %s: event loop was blocked/starved for %.1fs", name, lag)
        if now >= next_report:
            log.warning("DIAG %s event-loop lateness over last %ss: avg=%s max=%s", name, INTERVAL,
                        _ms(total / max(n, 1)), _ms(worst))
            n, total, worst = 0, 0.0, 0.0
            next_report += INTERVAL
            if bot is not None:
                try:
                    wi = await asyncio.wait_for(bot.get_webhook_info(), timeout=10)
                    log.warning("DIAG %s Telegram webhook: pending_update_count=%s last_error_message=%s",
                                name, wi.pending_update_count, wi.last_error_message)
                except Exception as e:
                    log.warning("DIAG %s getWebhookInfo failed: %r", name, e)