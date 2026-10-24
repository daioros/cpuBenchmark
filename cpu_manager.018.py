"""
CPU / GPU Manager  (Kivy)  -  one file for Windows AND Android.

Windows : add CPUs and GPUs from cpubenchmark.net / videocardbenchmark.net links (single or .txt batch),
          search, browse, edit, delete.   Data goes into cpus.db
Android : read-only viewer of that same cpus.db. Search + Browse only (no scraping code is loaded,
          no Add / Edit / Delete).

Windows:  pip install kivy requests beautifulsoup4
          python cpu_manager.018.py
Android:  rename to main.py, put it next to cpus.db and icon.png, build with Buildozer.
"""
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.factory import Factory
from kivy.metrics import dp, sp
from kivy.properties import NumericProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.popup import Popup
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.screenmanager import Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup, platform

IS_ANDROID = platform == "android"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cpus.db")
LOG_PATH = os.path.join(BASE_DIR, "scrape_log.txt")
PAUSE_SECONDS = 5   # wait between two page downloads
RETRY_WAIT = 30     # seconds to wait before retrying after HTTP 429 / 503 (x attempt number)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CPUManager/1.0"}

# (db column, label, is_numeric)
FIELDS_CPU = [
    ("name", "Name", False),
    ("class", "Class", False),
    ("socket", "Socket", False),
    ("cores", "Cores", True),
    ("threads", "Threads", True),
    ("other_names", "Other names", False),
    ("first_seen", "First seen on charts", False),
    ("multithread", "Multithread rating", True),
    ("single_thread", "Single thread rating", True),
]
# GPU fields. "class" stores the Videocard Category, "first_seen" the Videocard First Benchmarked.
FIELDS_GPU = [
    ("name", "Name", False),
    ("bus_interface", "Bus Interface", False),
    ("max_memory", "Max Memory Size", False),
    ("core_clock", "Core Clock(s)", False),
    ("memory_clock", "Memory Clock(s)", False),
    ("directx", "DirectX", False),
    ("opengl", "OpenGL", False),
    ("max_tdp", "Max TDP", False),
    ("class", "Videocard Category", False),
    ("other_names", "Other names", False),
    ("first_seen", "Videocard First Benchmarked", False),
    ("g2d_mark", "Average G2D Mark", True),
]
COLS = [f[0] for f in FIELDS_CPU]
GPU_COLS = [f[0] for f in FIELDS_GPU]

# colour of the CPU / GPU tag (hex for text markup, rgba for widgets)
TYPE_HEX = {"CPU": "26a69a", "GPU": "ab47bc"}
TYPE_RGBA = {"CPU": (0.15, 0.65, 0.60, 1), "GPU": (0.67, 0.28, 0.74, 1)}
GPU_HOST = "https://www.videocardbenchmark.net/"


def fields_for(t):
    return FIELDS_GPU if t == "GPU" else FIELDS_CPU


def cols_for(t):
    return GPU_COLS if t == "GPU" else COLS


# ----------------------------------------------------------------- database
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE cpus(
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL DEFAULT 'CPU',
    site_id       INTEGER,
    name          TEXT NOT NULL,
    class         TEXT,
    socket        TEXT,
    cores         INTEGER,
    threads       INTEGER,
    other_names   TEXT,
    first_seen    TEXT,
    multithread   INTEGER,
    single_thread INTEGER,
    bus_interface TEXT,
    max_memory    TEXT,
    core_clock    TEXT,
    memory_clock  TEXT,
    directx       TEXT,
    opengl        TEXT,
    max_tdp       TEXT,
    g2d_mark      INTEGER,
    url           TEXT,
    search_text   TEXT,
    UNIQUE(type, site_id)
);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_cpus_name   ON cpus(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_cpus_socket ON cpus(socket);
CREATE INDEX IF NOT EXISTS idx_cpus_type   ON cpus(type);
"""


def init_db():
    """Creates the database, or upgrades an old one (adds the Type column and the GPU fields).
    The Type of every existing row is taken from its link: videocardbenchmark.net -> GPU, else CPU."""
    conn = connect()
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cpus'").fetchone()
    if not exists:
        conn.executescript(SCHEMA)
    elif "type" not in [r[1] for r in conn.execute("PRAGMA table_info(cpus)")]:
        conn.close()
        backup = DB_PATH + ".backup_before_type"
        if not IS_ANDROID and not os.path.exists(backup):
            shutil.copy(DB_PATH, backup)              # safety copy of the old database
        conn = connect()
        old_cols = "id,site_id,name,class,socket,cores,threads,other_names,first_seen,multithread,single_thread,url,search_text"
        conn.executescript(
            "BEGIN;\nALTER TABLE cpus RENAME TO cpus_old;\n" + SCHEMA +
            "INSERT INTO cpus (id,type,site_id,name,class,socket,cores,threads,other_names,first_seen,"
            "multithread,single_thread,url,search_text)\n"
            "SELECT id, CASE WHEN lower(url) LIKE '" + GPU_HOST + "%' THEN 'GPU' ELSE 'CPU' END,\n"
            "       site_id,name,class,socket,cores,threads,other_names,first_seen,multithread,single_thread,url,search_text\n"
            "FROM cpus_old;\nDROP TABLE cpus_old;\nCOMMIT;")
    try:
        conn.executescript(INDEXES)
        conn.commit()
    except sqlite3.Error:
        if not IS_ANDROID:       # on Android the DB may be read-only: indexes are already in the bundled file
            raise
    finally:
        conn.close()


def norm(text):
    """lowercase, every non-alphanumeric char -> space  ('i5-4440' == 'i5 4440')."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def make_search_text(d):
    return norm(" ".join([d.get("name") or "", d.get("other_names") or "", d.get("socket") or ""]))


def db_search(query, limit=80):
    """Returns (rows, total). Every typed word must START a word of the name / other names / socket
    ('ryzen 5' no longer matches 'Ryzen 7 5800X'). Best matches first: exact name, name starts with
    the query, phrase inside the name, words inside the name, match only in other names / socket."""
    q = norm(query)
    tokens = q.split()
    if not tokens:
        return [], 0
    where = " AND ".join(["(search_text LIKE ? OR search_text LIKE ?)"] * len(tokens))
    params = []
    for t in tokens:
        params += [t + "%", "% " + t + "%"]
    conn = connect()
    rows = conn.execute(
        "SELECT id,name,type,socket,multithread,bus_interface,g2d_mark FROM cpus WHERE " + where +
        " ORDER BY name COLLATE NOCASE LIMIT 3000", params).fetchall()
    conn.close()

    def rank(r):
        n = norm(r["name"])
        if n == q:
            return 0
        if n.startswith(q):
            return 1
        if (" " + q) in (" " + n):          # the whole query as a phrase inside the name ('ryzen 5')
            return 2
        words = n.split()
        if all(any(w.startswith(t) for w in words) for t in tokens):
            return 3
        return 4
    rows = sorted(rows, key=rank)          # stable: alphabetical inside each rank
    return rows[:limit], len(rows)


def db_all():
    conn = connect()
    rows = conn.execute(
        "SELECT id,name,type,socket,multithread,bus_interface,g2d_mark FROM cpus ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return rows


def db_get(cpu_id):
    conn = connect()
    row = conn.execute("SELECT * FROM cpus WHERE id=?", (cpu_id,)).fetchone()
    conn.close()
    return row


def db_delete(cpu_id):
    conn = connect()
    conn.execute("DELETE FROM cpus WHERE id=?", (cpu_id,))
    conn.commit()
    conn.close()


def db_update_manual(cpu_id, data, kind="CPU"):
    data = dict(data)
    data["search_text"] = make_search_text(data)
    cols = cols_for(kind) + ["search_text"]
    conn = connect()
    conn.execute("UPDATE cpus SET " + ",".join(c + "=?" for c in cols) + " WHERE id=?",
                 [data[c] for c in cols] + [cpu_id])
    conn.commit()
    conn.close()


def db_name_by_site_id(site_id, kind="CPU"):
    """Name of the saved item (CPU or GPU) with this website id, or None."""
    conn = connect()
    row = conn.execute("SELECT name FROM cpus WHERE site_id=? AND type=?", (site_id, kind)).fetchone()
    conn.close()
    return row["name"] if row else None


def db_upsert(conn, d):
    """Returns (status, changes). status: ADDED / UPDATED / UNCHANGED.
    Primary key = (type, site id from the link), secondary key = (type, name ignoring case)."""
    kind = d.get("type", "CPU")
    cols = cols_for(kind)
    row = conn.execute("SELECT * FROM cpus WHERE site_id=? AND type=?", (d["site_id"], kind)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM cpus WHERE name=? COLLATE NOCASE AND type=?", (d["name"], kind)).fetchone()
    extra = ["type", "site_id", "url", "search_text"]
    if row is None:
        d["search_text"] = make_search_text(d)
        allc = cols + extra
        conn.execute("INSERT INTO cpus(%s) VALUES(%s)" % (",".join(allc), ",".join("?" * len(allc))),
                     [d[c] for c in allc])
        conn.commit()
        return "ADDED", []
    changes = []
    for c in cols:
        old, new = row[c], d[c]
        if isinstance(new, str) or isinstance(old, str):
            differs = (old or "") != (new or "")
        else:
            differs = old != new
        if differs:
            changes.append("%s: %s -> %s" % (c, "" if old is None else old, "" if new is None else new))
    if row["site_id"] != d["site_id"]:
        changes.append("site_id: %s -> %s" % (row["site_id"], d["site_id"]))
    if not changes:
        return "UNCHANGED", []
    d["search_text"] = make_search_text(d)
    allc = cols + ["site_id", "url", "search_text"]
    conn.execute("UPDATE cpus SET " + ",".join(c + "=?" for c in allc) + " WHERE id=?",
                 [d[c] for c in allc] + [row["id"]])
    conn.commit()
    return "UPDATED", changes


# ------------------------------------------------- scraping (Windows only)
GENERIC_NAMES = {"cpu benchmarks", "cpu benchmark", "passmark", "passmark software", "benchmark",
                 "benchmarks", "not found", "page not found", "error", "404", "access denied",
                 "just a moment", "cpu list", "videocard benchmarks", "video card", "video card benchmarks"}


# Normal quirks of the site: the CPU is saved fine, so they are NOT logged.
# Set LOG_MINOR_NOTES = True to log them again.
LOG_MINOR_NOTES = False
MINOR_NOTES = ("socket", "class is empty", "first seen missing", "other names missing",
               "threads not listed", "single thread rating not available")
PENDING_START = None   # batch header, written only if that batch has a problem to log


def log_line(status, url, details=""):
    """Only problems are logged (FAILED, or saved-with-warnings). Nothing for clean successes."""
    global PENDING_START
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            if PENDING_START:
                f.write("%s | START | %s | %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *PENDING_START))
                PENDING_START = None
            f.write("%s | %s | %s | %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, url, details))
    except OSError:
        pass          # log file locked / not writable: never stop the scraping because of it


def _field(lines, label, accept=None):
    """Value after `label`: on the same line, or on the next line."""
    low = label.lower()
    for i, line in enumerate(lines):
        if not line.lower().startswith(low):
            continue
        rest = line[len(label):].strip()
        val = rest or (lines[i + 1] if i + 1 < len(lines) else "")
        if not rest and re.match(r"[A-Za-z#][A-Za-z0-9#()/ .\-]{1,40}:(\s|$)", val):
            continue                      # next line is another label: this one is empty
        if not val or val.endswith(":"):
            continue
        if accept and not re.fullmatch(accept, val):
            continue
        return val
    return None


def _cores_threads(lines):
    """Cores/threads in the formats the site uses:
       'Cores: 4' + 'Threads: 8' | 'Cores: 8 (in 4 physical modules)' (no threads line)
       'Total Cores: 14 Cores, 20 Threads' (hybrid CPUs)."""
    for i, l in enumerate(lines):
        if l.lower().startswith("total cores:"):
            rest = l[12:].strip() or (lines[i + 1] if i + 1 < len(lines) else "")
            m = re.match(r"(\d+)\s*cores?\W+(\d+)\s*threads?", rest, re.I)
            if m:
                return int(m.group(1)), int(m.group(2))

    def lead_int(label):
        for i, l in enumerate(lines):
            if l.lower().startswith(label):
                rest = l[len(label):].strip() or (lines[i + 1] if i + 1 < len(lines) else "")
                m = re.match(r"(\d[\d,]*)", rest)
                if m:
                    return int(m.group(1).replace(",", ""))
        return None
    return lead_int("cores:"), lead_int("threads:")


def _has_label(lines, label):
    return any(l.lower().startswith(label.lower()) for l in lines)


def parse_cpu(html, url):
    """Returns (data, errors, warnings). data is only usable when errors is empty."""
    from bs4 import BeautifulSoup
    errors, warnings = [], []

    q = parse_qs(urlparse(url).query)
    soup = BeautifulSoup(html, "html.parser")

    id_from_page = False
    # id: from the link, otherwise from the page's own canonical link (primary key)
    m = re.fullmatch(r"\d+", (q.get("id") or [""])[0])
    if m:
        site_id = int(m.group(0))
    else:
        canon = soup.find("link", rel="canonical")
        cq = parse_qs(urlparse(canon.get("href", "") if canon else "").query)
        m = re.fullmatch(r"\d+", (cq.get("id") or [""])[0])
        if not m:
            return None, ["link has no numeric id= and the page does not declare one"], []
        site_id = int(m.group(0))
        url = canon.get("href")
        id_from_page = True
        warnings.append("id %d taken from the page (link had no id=)" % site_id)
    for t in soup(["script", "style"]):
        t.decompose()

    name = None
    for h in soup.find_all("h1"):
        t = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
        if t and t.lower() not in GENERIC_NAMES:
            name = t
            break
    if not name and soup.title:
        name = re.sub(r"\s*Benchmark\s*$", "", soup.title.get_text(strip=True))
    name = (name or "").strip()
    if len(name) < 4 or name.lower() in GENERIC_NAMES:
        errors.append("name empty/too short/generic page title: %r" % name)

    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").split("\n")]
    lines = [l for l in lines if l]

    def num(label, key, lo, hi):
        v = _field(lines, label, r"[\d,]+")
        if v is None:
            errors.append("%s not found" % key)
            return None
        n = int(v.replace(",", ""))
        if not lo <= n <= hi:
            errors.append("%s out of range: %d" % (key, n))
            return None
        return n

    cls = _field(lines, "Class:")
    if not cls:
        if _has_label(lines, "Class:"):
            cls = ""      # the site itself leaves it empty (some old CPUs): save empty + warn
            warnings.append("class is empty on the site")
        else:
            errors.append("class not found")
    elif len(cls) > 40:
        errors.append("class too long: %r" % cls[:50])

    cores, threads = _cores_threads(lines)
    if cores is None:
        errors.append("cores not found")
    elif not 1 <= cores <= 512:
        errors.append("cores out of range: %d" % cores)
        cores = None
    if threads is None and cores:
        threads = cores        # page lists no threads line (e.g. AMD FX): same as cores
        warnings.append("threads not listed on the site, set equal to cores")
    elif threads is not None and not 1 <= threads <= 1024:
        errors.append("threads out of range: %d" % threads)
        threads = None
    multi = num("Multithread Rating", "multithread", 1, 1000000)
    # Very old / rarely benchmarked CPUs legitimately have no single-thread rating:
    # the page says "Insufficient data samples to calculate single thread ranking".
    if any("insufficient data samples to calculate single thread" in l.lower() for l in lines):
        single = None
        warnings.append("single thread rating not available on the site (too few samples)")
    else:
        single = num("Single Thread Rating", "single thread", 1, 20000)
    if cores and threads and threads < cores:
        errors.append("threads (%d) lower than cores (%d)" % (threads, cores))

    socket = _field(lines, "Socket:")
    if socket is None:
        warnings.append("socket missing" if not _has_label(lines, "Socket:") else "socket is empty on the site")
    elif len(socket) > 60:
        errors.append("socket suspiciously long (%d chars)" % len(socket))

    other = _field(lines, "Other names:")
    if other is None:
        warnings.append("other names missing")
    elif len(other) > 800:
        errors.append("other names suspiciously long (%d chars)" % len(other))

    seen = _field(lines, "CPU First Seen on Charts:")
    if seen is None:
        warnings.append("first seen missing")
    elif not re.fullmatch(r"(Q[1-4] \d{4}|\d{4})", seen):
        errors.append("first seen has unexpected format: %r" % seen[:40])

    link_name = (q.get("cpu") or [""])[0]
    if name and link_name and norm(link_name) != norm(name):
        if id_from_page and norm(name).split()[:len(norm(link_name).split())] != norm(link_name).split():
            # id-less link: the page must really be the CPU that was asked for
            errors.append("link name %r does not match page name %r (wrong CPU?)" % (link_name, name))
        else:
            warnings.append("page name %r differs from link name %r" % (name, link_name))

    if errors:
        return None, errors, warnings
    return {"type": "CPU", "site_id": site_id, "url": url, "name": name, "class": cls or "", "socket": socket or "",
            "cores": cores, "threads": threads, "other_names": other or "",
            "first_seen": seen or "", "multithread": multi, "single_thread": single}, [], warnings


def parse_gpu(html, url):
    """GPU page of videocardbenchmark.net. Returns (data, errors, warnings).
    Only the basic fields are checked (name, Average G2D Mark, link/id). Older cards have fewer
    fields, so every other field is optional and simply saved empty when the page lacks it."""
    from bs4 import BeautifulSoup
    errors, warnings = [], []
    q = parse_qs(urlparse(url).query)
    soup = BeautifulSoup(html, "html.parser")
    id_from_page = False
    m = re.fullmatch(r"\d+", (q.get("id") or [""])[0])
    if m:
        site_id = int(m.group(0))
    else:
        canon = soup.find("link", rel="canonical")
        cq = parse_qs(urlparse(canon.get("href", "") if canon else "").query)
        m = re.fullmatch(r"\d+", (cq.get("id") or [""])[0])
        if not m:
            return None, ["link has no numeric id= and the page does not declare one"], []
        site_id = int(m.group(0))
        url = canon.get("href")
        id_from_page = True
        warnings.append("id %d taken from the page (link had no id=)" % site_id)
    for t in soup(["script", "style"]):
        t.decompose()

    name = None
    for h in soup.find_all("h1"):
        t = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
        if t and t.lower() not in GENERIC_NAMES:
            name = t
            break
    if not name and soup.title:
        name = re.sub(r"^\s*PassMark\s*-\s*|\s*-\s*Price performance comparison\s*$", "", soup.title.get_text(strip=True))
    name = (name or "").strip()
    if len(name) < 3 or name.lower() in GENERIC_NAMES:
        errors.append("name empty/too short/generic page title: %r" % name)

    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").split("\n")]
    lines = [l for l in lines if l]

    g2d = _field(lines, "Average G2D Mark:", r"[\d,]+")
    if g2d is None:
        errors.append("average G2D mark not found")
    else:
        g2d = int(g2d.replace(",", ""))
        if not 1 <= g2d <= 100000:
            errors.append("average G2D mark out of range: %d" % g2d)

    def opt(label):
        v = _field(lines, label)
        return "" if v is None or v.upper() in ("NA", "N/A") else v

    link_name = (q.get("gpu") or [""])[0]
    if name and link_name and norm(link_name) != norm(name):
        if id_from_page and norm(name).split()[:len(norm(link_name).split())] != norm(link_name).split():
            errors.append("link name %r does not match page name %r (wrong GPU?)" % (link_name, name))
        else:
            warnings.append("page name %r differs from link name %r" % (name, link_name))
    if errors:
        return None, errors, warnings
    return {"type": "GPU", "site_id": site_id, "url": url, "name": name,
            "bus_interface": opt("Bus Interface:"), "max_memory": opt("Max Memory Size:"),
            "core_clock": opt("Core Clock(s):"), "memory_clock": opt("Memory Clock(s):"),
            "directx": opt("DirectX:"), "opengl": opt("OpenGL:"), "max_tdp": opt("Max TDP:"),
            "class": opt("Videocard Category:"), "other_names": opt("Other names:"),
            "first_seen": opt("Videocard First Benchmarked:"), "g2d_mark": g2d}, [], warnings


def process_url(url, skip_existing=False):
    """Scrape one link and save it. Returns (status, details).
    skip_existing=True: if the link has id= and that CPU / GPU is already saved, nothing is
    downloaded and the status is SKIPPED (not logged)."""
    url = url.strip()
    host = urlparse(url).netloc.lower()
    if "videocardbenchmark.net" in host:
        kind = "GPU"
    elif "cpubenchmark.net" in host:
        kind = "CPU"
    else:
        msg = "unsupported website (use cpubenchmark.net or videocardbenchmark.net)"
        log_line("FAILED", url, msg)
        return "FAILED", msg
    if skip_existing:
        m = re.fullmatch(r"\d+", (parse_qs(urlparse(url).query).get("id") or [""])[0])
        if m:
            saved = db_name_by_site_id(int(m.group(0)), kind)
            if saved:
                return "SKIPPED", saved
    import requests
    html = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code in (429, 503) and attempt < 2:      # site asks us to slow down
                time.sleep(RETRY_WAIT * (attempt + 1))
                continue
            r.raise_for_status()
            html = r.content.decode("utf-8", errors="replace")
            break
        except Exception as e:  # noqa
            msg = "page can't be loaded: %s" % e
            log_line("FAILED", url, msg)
            return "FAILED", msg
    try:
        data, errors, warnings = (parse_gpu if kind == "GPU" else parse_cpu)(html, url)
    except Exception as e:  # noqa
        msg = "parser crashed: %s" % e
        log_line("FAILED", url, msg)
        return "FAILED", msg
    if not LOG_MINOR_NOTES:
        warnings = [w for w in warnings if not w.startswith(MINOR_NOTES)]
    wtxt = ("; warnings: " + ", ".join(warnings)) if warnings else ""
    if errors:
        msg = "; ".join(errors)
        log_line("FAILED", url, msg + wtxt)
        return "FAILED", msg
    conn = None
    try:
        conn = connect()
        status, changes = db_upsert(conn, data)
    except Exception as e:  # noqa   (e.g. database is locked because another program has it open)
        msg = "database error: %s" % e
        log_line("FAILED", url, msg + wtxt)
        return "FAILED", msg
    finally:
        if conn is not None:
            conn.close()
    detail = data["name"]
    if changes:
        detail += " [" + "; ".join(changes) + "]"
    if warnings:                      # clean ADDED / UPDATED / UNCHANGED are not logged
        log_line(status, url, detail + wtxt)
    return status, detail + wtxt


def read_links(path):
    links, seen = [], set()
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line.lower().startswith("http") and line not in seen:
                seen.add(line)
                links.append(line)
    return links


def short_link(link):
    """Short label for a link in the progress list (CPU or GPU name from the link)."""
    q = parse_qs(urlparse(link).query)
    for k in ("cpu", "gpu"):
        if q.get(k):
            return q[k][0][:40]
    return link[-40:]


# --------------------------------------------------------------------- UI
KV = """
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp

# every label / button / text box follows the app font size (the + / - buttons)
<Label>:
    font_size: sp(app.fs)
<TextInput>:
    font_size: sp(app.fs)

# the two font buttons together take half of the bar they are placed in
<FontButtons@BoxLayout>:
    size_hint_x: 0.5
    spacing: dp(6)
    Button:
        text: 'A-'
        font_size: sp(app.fs * 0.75)
        on_release: app.change_font(-2)
    Button:
        text: 'A+'
        bold: True
        font_size: sp(app.fs * 1.5)
        on_release: app.change_font(2)

# bottom bar of every screen: [ A- ][ A+ ] (left half)  [ < Back ] (right half)
<NavBar@BoxLayout>:
    size_hint_y: None
    height: app.bar_h * 1.3
    spacing: dp(6)

<BarRow@BoxLayout>:
    size_hint_y: None
    height: app.bar_h
    spacing: dp(8)

# big coloured CPU / GPU tag on top of the details window
<TypeLabel@Label>:
    markup: True
    bold: True
    font_size: sp(app.fs * 2.6)
    size_hint_y: None
    height: sp(app.fs) * 3.4

<CpuRow>:
    size_hint_y: None
    height: app.row_h
    markup: True
    halign: 'left'
    valign: 'middle'
    text_size: self.width - dp(20), self.height
    shorten: True
    shorten_from: 'right'
    background_normal: ''
    background_color: (0.16, 0.18, 0.22, 1)

<CpuList>:
    viewclass: 'CpuRow'
    bar_width: dp(8)
    scroll_type: ['bars', 'content']
    RecycleBoxLayout:
        default_size: None, app.row_h
        default_size_hint: 1, None
        size_hint_y: None
        height: self.minimum_height
        orientation: 'vertical'
        spacing: dp(2)

<HomeScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(12)
        spacing: dp(8)
        Label:
            text: 'CPU Manager'
            font_size: sp(app.fs * 2)
            size_hint_y: None
            height: sp(app.fs) * 3
        Label:
            id: stats
            size_hint_y: None
            height: app.bar_h * 0.8
        BoxLayout:
            size_hint_y: None
            height: app.bar_h
            spacing: dp(6)
            Button:
                text: 'Browse all'
                on_release: app.go('browse')
            Button:
                id: add_btn
                text: 'Add CPU / GPU'
                on_release: app.go('add')
        BoxLayout:
            size_hint_y: None
            height: app.bar_h * 1.6
            spacing: dp(6)
            TextInput:
                id: q
                hint_text: 'Search CPU or GPU (e.g. i5 4440)'
                multiline: False
                font_size: sp(app.fs * 1.3)
                padding: [dp(12), max((self.height - self.line_height) / 2, 0)]
                on_text: root.search_soon(self.text)
                on_text_validate: root.search_now()
            Button:
                text: 'Clear'
                size_hint_x: None
                width: sp(app.fs) * 6
                font_size: sp(app.fs * 1.3)
                on_release: q.text = ''
            Button:
                text: 'Search'
                size_hint_x: None
                width: sp(app.fs) * 7
                font_size: sp(app.fs * 1.3)
                on_release: root.search_now()
        Label:
            id: info
            size_hint_y: None
            height: app.bar_h * 0.7
        CpuList:
            id: results
        NavBar:
            FontButtons:
            Widget:
                size_hint_x: 0.5

<BrowseScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(8)
        spacing: dp(6)
        Label:
            id: info
            size_hint_y: None
            height: app.bar_h
        CpuList:
            id: results
        NavBar:
            FontButtons:
            Button:
                text: '< Back'
                size_hint_x: 0.5
                on_release: app.go('home')

<AddScreen>:
    BoxLayout:
        orientation: 'vertical'
        padding: dp(10)
        spacing: dp(8)
        Label:
            text: 'Add CPU / GPU'
            size_hint_y: None
            height: app.bar_h
        TextInput:
            id: url
            hint_text: 'Paste a cpubenchmark.net or videocardbenchmark.net link'
            multiline: False
            size_hint_y: None
            height: app.bar_h
        BoxLayout:
            size_hint_y: None
            height: app.bar_h * 1.7
            spacing: dp(8)
            Button:
                id: b1
                text: 'Scrape this link'
                halign: 'center'
                text_size: self.width - dp(8), None
                on_release: root.scrape_one()
            Button:
                id: b2
                text: 'Open .txt with several links'
                halign: 'center'
                text_size: self.width - dp(8), None
                on_release: root.pick_txt()
        BoxLayout:
            size_hint_y: None
            height: app.bar_h * 1.3
            spacing: dp(6)
            CheckBox:
                id: skip_chk
                active: False
                size_hint_x: None
                width: app.bar_h
            Label:
                text: 'Skip CPUs / GPUs already in the database (no download, no wait)'
                halign: 'left'
                valign: 'middle'
                text_size: self.width, self.height
                on_touch_down: if self.collide_point(*args[1].pos): skip_chk.active = not skip_chk.active
        Label:
            id: progress
            size_hint_y: None
            height: app.bar_h * 0.8
        ScrollView:
            Label:
                id: output
                size_hint_y: None
                height: self.texture_size[1]
                text_size: self.width - dp(10), None
                halign: 'left'
                valign: 'top'
        NavBar:
            FontButtons:
            Button:
                text: '< Back'
                size_hint_x: 0.5
                on_release: app.go('home')
"""


class CpuRow(RecycleDataViewBehavior, Button):
    cpu_id = NumericProperty(0)

    def refresh_view_attrs(self, rv, index, data):
        self.cpu_id = data.get("cpu_id", 0)
        return super().refresh_view_attrs(rv, index, data)

    def on_release(self):
        App.get_running_app().open_detail(self.cpu_id)


def mt_color(v):
    """Colour (hex) for the multithread rating."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "9e9e9e"
    if v < 5000:
        return "ff5252"      # red
    if v < 10000:
        return "ffa726"      # orange
    if v < 20000:
        return "bdbdbd"      # grey
    if v < 40000:
        return "66bb6a"      # green
    return "42a5f5"          # blue (>= 40000)


def type_tag(kind):
    """Coloured bold CPU / GPU tag used in the lists."""
    kind = kind if kind in TYPE_HEX else "CPU"
    return "[b][color=%s]%s[/color][/b]  " % (TYPE_HEX[kind], kind)


def count_text(rows):
    n_gpu = sum(1 for r in rows if r["type"] == "GPU")
    return "%d CPUs and %d GPUs in the database" % (len(rows) - n_gpu, n_gpu)


class CpuList(RecycleView):
    def show(self, rows, details=True):
        """details=False -> only the type tag and the name (used by Search)."""
        data = []
        for r in rows:
            text = type_tag(r["type"]) + escape_markup(r["name"])
            if details:
                if r["type"] == "GPU":
                    text += "    |    %s    |    G2D %s" % (
                        escape_markup(r["bus_interface"] or "-"), "-" if r["g2d_mark"] is None else r["g2d_mark"])
                else:
                    text += "    |    %s    |    MT [color=%s]%s[/color]" % (
                        escape_markup(r["socket"] or "-"), mt_color(r["multithread"]),
                        "-" if r["multithread"] is None else r["multithread"])
            data.append({"cpu_id": r["id"], "text": text})
        self.data = data
        self.scroll_y = 1


class HomeScreen(Screen):
    """Title, counter, buttons and the search (results update while typing)."""
    _search_ev = None

    def _cancel_pending(self):
        if self._search_ev is not None:
            self._search_ev.cancel()
            self._search_ev = None

    def search_soon(self, text):
        """Called on every key: waits 0.15 s of silence, then searches (keeps typing smooth)."""
        self._cancel_pending()
        self._search_ev = Clock.schedule_once(lambda dt: self.do_search(text), 0.15)

    def do_search(self, text):
        self._cancel_pending()
        rows, total = db_search(text)
        self.ids.results.show(rows, details=False)       # names only
        if not text.strip():
            self.ids.info.text = ""
        else:
            self.ids.info.text = "%d match(es)%s" % (total, " (showing first %d)" % len(rows) if total > len(rows) else "")

    def search_now(self):
        """Search button / Enter key: search and close the keyboard."""
        text = self.ids.q.text
        self.do_search(text)
        if not text.strip():
            self.ids.info.text = "Type a name to search"
        self.ids.q.focus = False

    def refresh(self):
        self.do_search(self.ids.q.text)


class BrowseScreen(Screen):
    def refresh(self):
        rows = db_all()
        self.ids.results.show(rows)
        self.ids.info.text = count_text(rows)


class AddScreen(Screen):
    busy = False

    def _set(self, output=None, progress=None):
        def apply(dt):
            if output is not None:
                self.ids.output.text = output
            if progress is not None:
                self.ids.progress.text = progress
        Clock.schedule_once(apply, 0)

    def _finish(self):
        self.busy = False
        Clock.schedule_once(lambda dt: App.get_running_app().refresh_all(), 0)

    # ---- single link
    def scrape_one(self):
        url = self.ids.url.text.strip()
        if self.busy:
            return
        if not url.lower().startswith("http"):
            self.ids.output.text = "Paste a link that starts with http"
            return
        self.busy = True
        skip = self.ids.skip_chk.active
        self._set(output="", progress="Scraping...")

        def work():
            try:
                status, details = process_url(url, skip)
                if status == "FAILED":
                    out = "NOT saved (extraction problem)\n%s\n\nSee scrape_log.txt" % details
                else:
                    out = {"ADDED": "Added", "UPDATED": "Updated", "UNCHANGED": "Already up to date",
                           "SKIPPED": "Skipped (already in the database)"}[status] + ": " + details
                self._set(output=out, progress="Done")
            except Exception as e:  # noqa
                self._set(output="Unexpected error: %s" % e, progress="Stopped")
            finally:
                self._finish()
        threading.Thread(target=work, daemon=True).start()

    # ---- .txt with many links
    def pick_txt(self):
        if self.busy:
            return
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(title="Open .txt with links",
                                              filetypes=[("Text files", "*.txt")])
            root.destroy()
        except Exception as e:  # noqa
            self.ids.output.text = "Could not open file picker: %s" % e
            return
        if not path:
            return
        links = read_links(path)
        if not links:
            self.ids.output.text = "No links found (lines must start with http)."
            return
        self.busy = True
        self._set(output="", progress="Starting %d link(s)..." % len(links))
        threading.Thread(target=self._batch, args=(path, links, self.ids.skip_chk.active),
                         daemon=True).start()

    def _batch(self, path, links, skip):
        global PENDING_START
        try:
            self._batch_run(path, links, skip)
        except Exception as e:  # noqa
            self._set(output="Batch stopped by an unexpected error: %s" % e, progress="Stopped")
        finally:
            PENDING_START = None          # a clean batch must not leave a stale START header behind
            self._finish()                # always unlock the screen

    def _batch_run(self, path, links, skip):
        global PENDING_START
        PENDING_START = (path, "%d unique links" % len(links))
        counts = {"ADDED": 0, "UPDATED": 0, "UNCHANGED": 0, "FAILED": 0, "SKIPPED": 0}
        failed, lines = [], []
        for i, link in enumerate(links, 1):
            self._set(progress="Scraping %d/%d" % (i, len(links)))
            status, details = process_url(link, skip)
            counts[status] += 1
            if status == "FAILED":
                failed.append((link, details))
            lines.append("%s %s: %s" % (status, short_link(link), details))
            self._set(output="\n".join(lines[-40:]))
            if i < len(links) and status != "SKIPPED":      # nothing was downloaded: no need to wait
                time.sleep(PAUSE_SECONDS)
        if failed:   # handy to retry only the failed ones
            with open(os.path.join(BASE_DIR, "failed_links.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(l for l, r in failed) + "\n")
        saved = counts["ADDED"] + counts["UPDATED"] + counts["UNCHANGED"]
        summary = "Done: %d saved, %d failed\n(added %d, updated %d, already up to date %d)" % (
            saved, counts["FAILED"], counts["ADDED"], counts["UPDATED"], counts["UNCHANGED"])
        if counts["SKIPPED"]:
            summary += "\nSkipped (already in the database, not downloaded): %d" % counts["SKIPPED"]
        if failed:
            summary += "\n\nNOT saved (extraction problem) - first 5:\n" + "\n".join(
                "- %s\n    %s" % (l, r) for l, r in failed[:5])
        summary += "\n\nLog: scrape_log.txt  |  failed links: failed_links.txt"
        self._set(output=summary, progress="Done")


# ------------------------------------------------------------------- app
class CpuApp(App):
    title = "CPU Manager"
    fs = NumericProperty(16)        # base font size (sp), changed with the + / - buttons
    bar_h = NumericProperty(0)      # height of bars / buttons, follows the font size
    row_h = NumericProperty(0)      # height of a list row, follows the font size

    def on_fs(self, *_):
        self.bar_h = sp(self.fs) * 2.3 + dp(6)
        self.row_h = sp(self.fs) * 2.1 + dp(14)

    def change_font(self, delta):
        self.fs = max(10, min(40, self.fs + delta))

    def build(self):
        self.on_fs()
        init_db()
        if not IS_ANDROID:
            Window.size = (520, 760)
            icon = os.path.join(BASE_DIR, "icon.png")
            if os.path.exists(icon):
                self.icon = icon
        Window.bind(on_keyboard=self.on_key)
        Builder.load_string(KV)
        self.sm = ScreenManager()
        self.sm.add_widget(HomeScreen(name="home"))
        self.sm.add_widget(BrowseScreen(name="browse"))
        home = self.sm.get_screen("home")
        if IS_ANDROID:
            home.ids.add_btn.parent.remove_widget(home.ids.add_btn)   # hidden on Android
        else:
            self.sm.add_widget(AddScreen(name="add"))
        self.refresh_all()
        return self.sm

    def on_key(self, window, key, *args):
        """Android Back button (and Esc): close the open window, else go Home, else leave the app."""
        if key != 27:
            return False
        for w in Window.children[:]:
            if isinstance(w, ModalView):
                w.dismiss()
                return True
        if self.sm.current != "home":
            self.go("home")
            return True
        return False                      # on Home: default behaviour (exit)

    def go(self, name):
        self.sm.current = name
        if name in ("home", "browse"):
            self.sm.get_screen(name).refresh()

    def refresh_all(self):
        conn = connect()
        counts = {r["type"]: r["n"] for r in conn.execute("SELECT type, COUNT(*) AS n FROM cpus GROUP BY type")}
        conn.close()
        self.sm.get_screen("home").ids.stats.text = "%d CPUs and %d GPUs in the database" % (
            counts.get("CPU", 0), counts.get("GPU", 0))
        for s in ("home", "browse"):
            self.sm.get_screen(s).refresh()

    # ---- detail / edit / delete
    def open_detail(self, cpu_id):
        row = db_get(cpu_id)
        if row is None:
            return
        kind = row["type"] if row["type"] in TYPE_HEX else "CPU"
        lines = []
        for col, lab, _ in fields_for(kind):
            v = row[col]
            val = escape_markup(str(v)) if v not in (None, "") else "-"
            if kind == "CPU" and col == "multithread":
                val = "[color=%s][b]%s[/b][/color]" % (mt_color(v), val)
            lines.append("[b]%s:[/b] %s" % (lab, val))
        lines.append("[b]Link:[/b] %s" % escape_markup(row["url"] or "-"))
        box = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))
        # big coloured CPU / GPU tag
        box.add_widget(Factory.TypeLabel(text=kind, color=TYPE_RGBA[kind]))
        sv = ScrollView()
        lbl = Label(text="\n".join(lines), markup=True, halign="left", valign="top", size_hint_y=None)
        lbl.bind(width=lambda w, v: setattr(w, "text_size", (v - dp(10), None)))
        lbl.bind(texture_size=lambda w, s: setattr(w, "height", s[1]))
        sv.add_widget(lbl)
        box.add_widget(sv)
        # title (name) is bigger than the normal text, coloured like the type, and follows the font buttons
        pop = Popup(title=row["name"], title_size=sp(self.fs * 1.6), title_color=TYPE_RGBA[kind],
                    separator_color=TYPE_RGBA[kind], content=box, size_hint=(1, 1))   # full screen
        size_cb = lambda inst, v: setattr(pop, "title_size", sp(v * 1.6))
        self.bind(fs=size_cb)
        pop.bind(on_dismiss=lambda *_: self.unbind(fs=size_cb))
        if not IS_ANDROID:                       # Edit / Delete only on Windows
            actions = Factory.BarRow()
            actions.add_widget(Button(text="Edit", on_release=lambda *_: (pop.dismiss(), self.open_edit(cpu_id))))
            actions.add_widget(Button(text="Delete", on_release=lambda *_: (pop.dismiss(), self.confirm_delete(cpu_id, row["name"], kind))))
            box.add_widget(actions)
        nav = Factory.NavBar()                   # same bottom bar as every screen
        nav.add_widget(Factory.FontButtons())
        nav.add_widget(Button(text="< Back", size_hint_x=0.5, on_release=pop.dismiss))
        box.add_widget(nav)
        pop.open()

    def confirm_delete(self, cpu_id, name, kind="CPU"):
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        box.add_widget(Label(text="Delete this %s?\n%s" % (kind, name), halign="center"))
        row = Factory.BarRow()
        pop = Popup(title="Confirm", content=box, size_hint=(0.85, 0.4))

        def do(*_):
            db_delete(cpu_id)
            pop.dismiss()
            self.refresh_all()
        row.add_widget(Button(text="Delete", on_release=do))
        row.add_widget(Button(text="Cancel", on_release=pop.dismiss))
        box.add_widget(row)
        pop.open()

    def open_edit(self, cpu_id):
        row = db_get(cpu_id)
        kind = row["type"] if row["type"] in TYPE_HEX else "CPU"
        fields = fields_for(kind)
        box = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        sv = ScrollView()
        grid = GridLayout(cols=2, spacing=dp(6), size_hint_y=None, row_default_height=self.bar_h,
                          row_force_default=True)
        grid.bind(minimum_height=grid.setter("height"))
        inputs = {}
        for col, lab, _ in fields:
            grid.add_widget(Label(text=lab, size_hint_x=0.4))
            ti = TextInput(text="" if row[col] is None else str(row[col]), multiline=False, size_hint_x=0.6)
            inputs[col] = ti
            grid.add_widget(ti)
        sv.add_widget(grid)
        box.add_widget(sv)
        err = Label(text="", color=(1, 0.4, 0.4, 1), size_hint_y=None, height=self.bar_h * 1.6)
        err.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
        box.add_widget(err)
        btns = Factory.BarRow()
        pop = Popup(title="Edit " + kind, title_color=TYPE_RGBA[kind], content=box, size_hint=(0.95, 0.9))

        def save(*_):
            data, problems = {}, []
            for col, lab, is_num in fields:
                v = inputs[col].text.strip()
                if col == "name" and not v:
                    problems.append("Name cannot be empty")
                if is_num:
                    if col == "single_thread" and v == "":
                        data[col] = None          # allowed: site has no rating for this CPU
                    elif re.fullmatch(r"\d+", v):
                        data[col] = int(v)
                    else:
                        problems.append("%s must be a number" % lab)
                else:
                    data[col] = v
            if problems:
                err.text = "; ".join(problems)
                return
            db_update_manual(cpu_id, data, kind)
            pop.dismiss()
            self.refresh_all()
        btns.add_widget(Button(text="Save", on_release=save))
        btns.add_widget(Button(text="Cancel", on_release=pop.dismiss))
        box.add_widget(btns)
        pop.open()


if __name__ == "__main__":
    CpuApp().run()
