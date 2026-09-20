"""
Student Test Portal (v2) - single-file Flask app: SQLite + HTML + CSS + JavaScript.

STUDENT FLOW
    Details -> Instructions -> Timed test (one question at a time) -> Report card

ADMIN FLOW  (/admin)
    Overview (results analysis) | Questions (single + bulk add) | Results | Settings

Optional environment variables (everything else is set from the Admin > Settings tab):
    SECRET_KEY       long random text (keeps login sessions secure)   <- set this on Render
    ADMIN_PASSWORD   admin login password (default: admin123)         <- change this!
    DB_PATH          location of the SQLite file (default: ./test_app.db)

Run locally :  python app.py
Run on Render: gunicorn app:app
"""
import csv
import hmac
import io
import json
import os
import random
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "test_app.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
IST = timezone(timedelta(hours=5, minutes=30))  # results are time-stamped in Indian time

OPTION_KEYS = {"a": "option_a", "b": "option_b", "c": "option_c", "d": "option_d"}


# ----------------------------------------------------------------------
# DATABASE + SETTINGS
# ----------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with closing(get_db()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_text TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_option TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                student_class TEXT NOT NULL,
                semester TEXT NOT NULL,
                phone TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                time_taken INTEGER,
                answers TEXT
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # upgrade databases created by the older version
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(results)")}
        if "time_taken" not in cols:
            conn.execute("ALTER TABLE results ADD COLUMN time_taken INTEGER")
        if "answers" not in cols:
            conn.execute("ALTER TABLE results ADD COLUMN answers TEXT")
        conn.commit()


init_db()

DEFAULT_SETTINGS = {
    "school_name": "Student Test Portal",
    "test_title": "Class Test",
    "minutes": "15",
    "is_open": "1",
    "shuffle": "1",
    "allow_retake": "0",
}


def load_settings(conn):
    raw = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        raw[row["key"]] = row["value"]
    try:
        minutes = max(1, min(240, int(raw["minutes"])))
    except ValueError:
        minutes = 15
    return {
        "school_name": raw["school_name"],
        "test_title": raw["test_title"],
        "minutes": minutes,
        "is_open": raw["is_open"] == "1",
        "shuffle": raw["shuffle"] == "1",
        "allow_retake": raw["allow_retake"] == "1",
    }


def cfg():
    """Settings for the current request (loaded once)."""
    if "cfg" not in g:
        with closing(get_db()) as conn:
            g.cfg = load_settings(conn)
    return g.cfg


@app.context_processor
def inject_cfg():
    return {"cfg": cfg()}


# ----------------------------------------------------------------------
# SHARED LAYOUT + STYLE  (white background, responsive, one font family)
# ----------------------------------------------------------------------
LAYOUT_TOP = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ cfg.school_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#FFFFFF;
  --ink:#1B2140;
  --muted:#5F6785;
  --line:#E4E8F3;
  --soft:#F6F8FE;
  --brand:#3B4EDB;
  --brand-dark:#2E3DB0;
  --brand-tint:#ECEFFE;
  --ok:#1F9D55;
  --ok-tint:#E8F7EE;
  --err:#D64545;
  --err-tint:#FDECEC;
  --amber:#E8A200;
  --amber-tint:#FFF6DD;
  --radius:16px;
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font-family:'Nunito',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased;
}
a{color:var(--brand);text-decoration:none;}
[hidden]{display:none !important;}

/* page frame */
.brandbar{max-width:1000px;margin:0 auto;padding:22px 20px 0;display:flex;align-items:center;justify-content:space-between;gap:16px;}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:1.05rem;min-width:0;}
.brand span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mark{
  flex:none;width:36px;height:36px;border-radius:11px;background:var(--brand);
  display:grid;place-items:center;box-shadow:0 0 0 4px var(--brand-tint);
}
.brand-test{font-size:.85rem;font-weight:700;color:var(--muted);white-space:nowrap;}
.wrap{max-width:1000px;margin:0 auto;padding:22px 20px 56px;}
.narrow{max-width:720px;margin:0 auto;}

/* typography */
h1{font-size:1.75rem;line-height:1.2;margin:0 0 8px;font-weight:800;letter-spacing:-0.01em;}
h2{font-size:1.15rem;margin:0 0 4px;font-weight:800;}
.lead{color:var(--muted);margin:0 0 22px;}
.muted{color:var(--muted);font-size:.9rem;}

/* cards, pills */
.card{border:1px solid var(--line);border-radius:var(--radius);padding:28px;margin-bottom:20px;background:#fff;}
.pill{display:inline-block;padding:4px 12px;border-radius:999px;background:var(--brand-tint);color:var(--brand-dark);font-weight:700;font-size:.85rem;}
.pill.ok{background:var(--ok-tint);color:var(--ok);}
.pill.bad{background:var(--err-tint);color:var(--err);}

/* forms */
.field{display:block;margin-bottom:16px;}
.field > span{display:block;font-weight:700;font-size:.9rem;margin-bottom:6px;}
input[type=text],input[type=tel],input[type=password],input[type=number],select,textarea{
  width:100%;padding:13px 14px;border:1.5px solid var(--line);border-radius:12px;
  font:inherit;font-size:16px;color:var(--ink);background:#fff;outline:none;
  transition:border-color .15s, box-shadow .15s;
}
textarea{resize:vertical;line-height:1.5;}
input::placeholder,textarea::placeholder{color:#9AA1BA;}
input:focus,select:focus,textarea:focus{border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-tint);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;}
.check{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;cursor:pointer;}
.check input{width:20px;height:20px;margin-top:2px;accent-color:var(--brand);flex:none;}
.check b{display:block;font-size:.95rem;}
.check small{color:var(--muted);}

/* buttons */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:13px 22px;border-radius:12px;border:1.5px solid var(--brand);
  background:var(--brand);color:#fff;font:inherit;font-weight:800;cursor:pointer;
  text-decoration:none;transition:background .15s, transform .1s;
}
.btn:hover{background:var(--brand-dark);border-color:var(--brand-dark);}
.btn:active{transform:translateY(1px);}
.btn:focus-visible,.pb:focus-visible,.tab:focus-visible{outline:3px solid var(--brand);outline-offset:2px;}
.btn-full{width:100%;margin-top:6px;}
.btn-ghost{background:#fff;color:var(--ink);border-color:var(--line);}
.btn-ghost:hover{background:var(--soft);border-color:var(--line);}
.btn-ghost.on{background:var(--amber-tint);border-color:var(--amber);color:#7A5200;}
.btn-danger{background:#fff;color:var(--err);border-color:#F1B5B5;}
.btn-danger:hover{background:var(--err-tint);border-color:#F1B5B5;}
.btn-sm{padding:8px 14px;font-size:.875rem;border-radius:10px;}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.btn:disabled{opacity:.45;cursor:not-allowed;}
.small-link{display:block;text-align:center;margin-top:10px;font-size:.9rem;}

/* flash */
.flash{padding:12px 16px;border-radius:12px;margin-bottom:18px;font-weight:600;font-size:.95rem;background:var(--brand-tint);color:var(--brand-dark);}
.flash.error{background:var(--err-tint);color:var(--err);}
.flash.success{background:var(--ok-tint);color:var(--ok);}

/* home */
.split{display:grid;grid-template-columns:1fr;gap:28px;align-items:start;}
@media (min-width:860px){.split{grid-template-columns:1fr 1fr;gap:56px;padding-top:24px;}}
.intro h1{font-size:2.3rem;margin:14px 0 10px;}
.steps{list-style:none;margin:28px 0 0;padding:0;}
.steps li{display:flex;gap:14px;position:relative;padding-bottom:20px;}
.steps li:last-child{padding-bottom:0;}
.steps li::before{content:"";position:absolute;left:15px;top:32px;bottom:2px;width:2px;background:var(--line);}
.steps li:last-child::before{display:none;}
.steps .n{
  flex:none;width:32px;height:32px;border-radius:50%;border:2px solid var(--brand);color:var(--brand-dark);
  display:grid;place-items:center;font-weight:800;font-size:.9rem;background:#fff;
}
.steps b{display:block;line-height:1.3;padding-top:4px;}
.steps small{color:var(--muted);}

/* instructions */
.who{display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid var(--line);}
.avatar{width:48px;height:48px;border-radius:50%;background:var(--brand);color:#fff;display:grid;place-items:center;font-weight:800;font-size:1.2rem;flex:none;}
.who-name{font-weight:800;font-size:1.1rem;line-height:1.2;}
.rules{list-style:none;margin:16px 0 24px;padding:0;}
.rules li{display:flex;gap:12px;padding:9px 0;}
.rules li::before{
  content:"";flex:none;width:20px;height:20px;margin-top:2px;border-radius:50%;background:var(--brand-tint);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%232E3DB0' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 12.5l4 4 8-9'/%3E%3C/svg%3E");
  background-size:70%;background-repeat:no-repeat;background-position:center;
}

/* test */
.testbar{
  position:sticky;top:0;z-index:10;background:#fff;
  margin:0 -20px 18px;padding:12px 20px 10px;border-bottom:1px solid var(--line);
}
.tb-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.tb-name{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw;}
.tb-meta{font-size:.82rem;color:var(--muted);}
.timer{
  font-weight:800;font-variant-numeric:tabular-nums;white-space:nowrap;
  background:var(--brand-tint);color:var(--brand-dark);padding:7px 14px;border-radius:999px;
}
.timer.warn{background:var(--amber-tint);color:#7A5200;}
.timer.low{background:var(--err-tint);color:var(--err);}
.track{height:6px;background:var(--line);border-radius:999px;overflow:hidden;margin-top:10px;}
.bar{height:100%;width:0;background:var(--brand);border-radius:999px;transition:width .25s;}
.tb-count{font-size:.8rem;color:var(--muted);margin-top:6px;font-weight:600;}

.palette{border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;margin-bottom:16px;}
.pal-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px;}
.pal-top b{font-size:.95rem;}
.pal-grid{display:flex;flex-wrap:wrap;gap:8px;}
.pb{
  position:relative;width:38px;height:38px;border-radius:10px;border:1.5px solid var(--line);background:#fff;
  font:inherit;font-weight:800;font-size:.9rem;color:var(--muted);cursor:pointer;
}
.pb.answered{background:var(--brand-tint);border-color:var(--brand);color:var(--brand-dark);}
.pb.current{background:var(--brand);border-color:var(--brand);color:#fff;}
.pb.flagged::after{content:"";position:absolute;top:-5px;right:-5px;width:13px;height:13px;border-radius:50%;background:var(--amber);border:2px solid #fff;}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:.8rem;color:var(--muted);margin-top:12px;}
.legend i{display:inline-block;width:12px;height:12px;border-radius:4px;margin-right:6px;vertical-align:-1px;border:1.5px solid var(--line);}
.legend .l-ans{background:var(--brand-tint);border-color:var(--brand);}
.legend .l-cur{background:var(--brand);border-color:var(--brand);}
.legend .l-flag{background:var(--amber);border-color:var(--amber);border-radius:50%;}

.q{display:none;border:1px solid var(--line);border-radius:var(--radius);padding:24px;}
.q.active{display:block;}
.q-head{display:flex;gap:14px;margin-bottom:18px;}
.q-num{
  flex:none;width:34px;height:34px;border-radius:50%;background:var(--ink);color:#fff;
  display:grid;place-items:center;font-weight:800;font-size:.95rem;
}
.q-text{font-weight:700;font-size:1.15rem;line-height:1.4;padding-top:3px;}
.opt{
  position:relative;display:flex;align-items:center;gap:12px;padding:13px 14px;
  border:1.5px solid var(--line);border-radius:12px;margin-bottom:10px;cursor:pointer;
  transition:border-color .15s, background .15s;
}
.opt:last-child{margin-bottom:0;}
.opt:hover{border-color:var(--brand);}
.opt input{position:absolute;opacity:0;pointer-events:none;}
.letter{
  flex:none;width:30px;height:30px;border-radius:8px;background:var(--soft);border:1px solid var(--line);
  display:grid;place-items:center;font-weight:800;font-size:.85rem;color:var(--muted);
  transition:background .15s, color .15s;
}
.opt:has(input:checked){border-color:var(--brand);background:var(--brand-tint);}
.opt input:checked + .letter{background:var(--brand);border-color:var(--brand);color:#fff;}
.opt:has(input:focus-visible){outline:3px solid var(--brand);outline-offset:2px;}
.qnav{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;margin-top:16px;}
.qnav .prev{justify-self:start;}
.qnav .next{justify-self:end;}
.hint{text-align:center;color:var(--muted);font-size:.8rem;margin-top:14px;}

dialog.dlg{
  border:none;border-radius:var(--radius);padding:26px;max-width:480px;width:calc(100% - 32px);
  color:var(--ink);font-family:inherit;box-shadow:0 24px 60px rgba(27,33,64,.28);
}
dialog.dlg::backdrop{background:rgba(27,33,64,.45);}
.dlg-sec{margin:14px 0 4px;}
.dlg-sec b{display:block;font-size:.88rem;margin-bottom:8px;}
.jump{display:flex;flex-wrap:wrap;gap:6px;}
.jump button{
  min-width:36px;height:34px;padding:0 10px;border-radius:9px;border:1.5px solid var(--line);background:#fff;
  font:inherit;font-weight:800;font-size:.85rem;cursor:pointer;color:var(--ink);
}
.jump button:hover{border-color:var(--brand);}
.dlg .btn-row{margin-top:22px;}
.dlg .btn-row .btn{flex:1;}

/* report card */
.hero{text-align:center;}
.ring{
  --pct:0;width:160px;height:160px;border-radius:50%;margin:22px auto 16px;
  background:conic-gradient(var(--brand) calc(var(--pct) * 1%), var(--line) 0);
  display:grid;place-items:center;
}
.ring span{
  width:128px;height:128px;border-radius:50%;background:#fff;
  display:grid;place-items:center;font-size:2.2rem;font-weight:800;color:var(--brand-dark);
}
.grade{display:inline-block;padding:6px 18px;border-radius:999px;background:var(--brand);color:#fff;font-weight:800;margin:8px 0 10px;}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-top:24px;}
.chip{border-radius:12px;padding:12px 8px;background:var(--soft);}
.chip b{display:block;font-size:1.35rem;font-weight:800;line-height:1.3;}
.chip small{color:var(--muted);font-weight:600;}
.chip.ok b{color:var(--ok);}
.chip.bad b{color:var(--err);}
.rv-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px;}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:3px;}
.seg button{border:none;background:none;padding:6px 14px;border-radius:999px;font:inherit;font-weight:700;font-size:.85rem;color:var(--muted);cursor:pointer;}
.seg button.on{background:var(--brand);color:#fff;}
.rv{display:flex;gap:12px;padding:14px 0;border-bottom:1px solid var(--line);}
.rv-list .rv:last-child{border-bottom:none;}
.rv-mark{
  flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;
  font-weight:800;font-size:.85rem;margin-top:2px;
}
.rv-mark.ok{background:var(--ok-tint);color:var(--ok);}
.rv-mark.bad{background:var(--err-tint);color:var(--err);}
.rv-mark.skip{background:var(--soft);color:var(--muted);}
.rv-q{font-weight:700;}
.rv-a{font-size:.9rem;color:var(--muted);}
.rv-a b{color:var(--ink);}
.rv-a .good{color:var(--ok);}
.print-only{display:none;}

/* admin */
.dash-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:18px;}
.dash-top h1{margin-bottom:4px;}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:24px;overflow-x:auto;}
.tab{
  padding:11px 16px;border:none;background:none;font:inherit;font-weight:700;color:var(--muted);
  border-bottom:3px solid transparent;cursor:pointer;white-space:nowrap;margin-bottom:-1px;
}
.tab.active{color:var(--brand-dark);border-bottom-color:var(--brand);}
.panel{display:none;}
.panel.active{display:block;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:22px;}
.stat{background:var(--soft);border-radius:12px;padding:14px 16px;}
.stat b{display:block;font-size:1.6rem;font-weight:800;color:var(--brand);line-height:1.25;}
.stat span{font-size:.85rem;color:var(--muted);font-weight:600;}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
@media (max-width:820px){.two{grid-template-columns:1fr;}}
.brow{display:grid;grid-template-columns:34px 1fr 30px;gap:12px;align-items:center;margin:10px 0;font-weight:700;font-size:.9rem;}
.meter{height:10px;background:var(--soft);border-radius:999px;overflow:hidden;}
.meter i{display:block;height:100%;background:var(--brand);border-radius:999px;}
.meter.good i{background:var(--ok);}
.meter.mid i{background:var(--amber);}
.meter.low i{background:var(--err);}
.irow{display:grid;grid-template-columns:1fr 150px 44px;gap:14px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);font-size:.92rem;}
.irow:last-child{border-bottom:none;}
.irow .qt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.irow .pc{font-weight:800;text-align:right;}
.qrow{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--line);}
.qrow:last-child{border-bottom:none;}
.qrow .opts{font-size:.9rem;color:var(--muted);margin-top:4px;}
.qrow .opts .right{color:var(--ok);font-weight:800;}
.tools{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:12px;}
.tools input{max-width:280px;}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;font-size:.92rem;min-width:640px;}
th{text-align:left;color:var(--muted);font-weight:700;font-size:.82rem;padding:8px 10px;border-bottom:1.5px solid var(--line);white-space:nowrap;}
td{padding:10px;border-bottom:1px solid var(--line);white-space:nowrap;}
code.fmt{display:block;background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:.85rem;margin:0 0 14px;overflow-x:auto;white-space:nowrap;}

@media (max-width:560px){
  .grid2{grid-template-columns:1fr;}
  .card{padding:22px 18px;}
  h1{font-size:1.5rem;}
  .intro h1{font-size:1.9rem;}
  .btn-row .btn{width:100%;}
  .qrow{flex-direction:column;}
  .tb-name{max-width:38vw;}
  .brand-test{display:none;}
  .q{padding:18px;}
  .qnav{grid-template-columns:1fr 1fr;}
  .qnav .mark{grid-column:1 / -1;order:-1;}
  .irow{grid-template-columns:1fr 44px;}
  .irow .meter{grid-column:1 / -1;order:3;}
}
@media print{
  body{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .no-print{display:none !important;}
  .print-only{display:block;}
  .card{border-color:#ccc;break-inside:avoid;}
}
@media (prefers-reduced-motion:reduce){*{transition:none !important;}}
</style>
</head>
<body>
<div class="brandbar no-print">
  <div class="brand">
    <div class="mark">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>
    </div>
    <span>{{ cfg.school_name }}</span>
  </div>
  <div class="brand-test">{{ cfg.test_title }}</div>
</div>
<main class="wrap">
{% for cat, msg in get_flashed_messages(with_categories=true) %}
  <div class="flash {{ cat }}">{{ msg }}</div>
{% endfor %}
"""

LAYOUT_BOTTOM = """
</main>
</body>
</html>
"""


def page(body_html):
    return LAYOUT_TOP + body_html + LAYOUT_BOTTOM


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------
def now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds):
    if seconds is None:
        return "-"
    m, s = divmod(int(seconds), 60)
    return f"{m} min {s:02d} sec" if m else f"{s} sec"


def fmt_clock(seconds):
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def ordered_questions(conn):
    """Questions in this student's own (shuffled) order, or by id."""
    rows = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    order = session.get("order")
    if not order:
        return rows
    pos = {qid: i for i, qid in enumerate(order)}
    return sorted(rows, key=lambda q: (pos.get(q["id"], 10**9), q["id"]))


def evaluate(questions, answers):
    """Return (score, review_list) for answers like {'12': 'b'}."""
    score = 0
    review = []
    for i, q in enumerate(questions, start=1):
        chosen = answers.get(str(q["id"]))
        correct = q["correct_option"]
        if chosen not in OPTION_KEYS:
            status = "skip"
        elif chosen == correct:
            status = "ok"
            score += 1
        else:
            status = "bad"
        review.append(
            {
                "number": i,
                "question": q["question_text"],
                "your": q[OPTION_KEYS[chosen]] if chosen in OPTION_KEYS else "Not answered",
                "correct": q[OPTION_KEYS[correct]],
                "status": status,
            }
        )
    return score, review


def grade_for(pct, first_name=""):
    who = f", {first_name}" if first_name else ""
    if pct >= 90:
        return "A+", f"Outstanding{who}!", "Excellent work. You clearly know this topic well."
    if pct >= 75:
        return "A", f"Great job{who}!", "You did really well. Check the few you missed and you're set."
    if pct >= 60:
        return "B", f"Good effort{who}!", "A little more practice will take you higher."
    if pct >= 40:
        return "C", f"Nice try{who}!", "You're on the right track. Review the questions below and try again."
    return "D", f"Keep going{who}!", "Every test is practice. Read the answer review and you'll do better next time."


def csv_safe(value):
    """Stop Excel from running text like '=SUM(...)' typed by a student as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def back_to(tab):
    return redirect(url_for("admin_dashboard") + "#" + tab)


# ----------------------------------------------------------------------
# STUDENT FLOW  1) details
# ----------------------------------------------------------------------
HOME_TEMPLATE = """
<div class="split">
  <section class="intro">
    <span class="pill">{{ cfg.test_title }}</span>
    <h1>Welcome to your test</h1>
    <p class="lead" style="margin-bottom:0;">
      {% if n %}Answer {{ n }} multiple-choice question{{ '' if n == 1 else 's' }} in {{ cfg.minutes }} minutes and see your report card as soon as you finish.{% else %}Your teacher is still setting up the questions.{% endif %}
    </p>
    <ol class="steps">
      <li><div class="n">1</div><div><b>Enter your details</b><small>Name, class, semester and phone</small></div></li>
      <li><div class="n">2</div><div><b>Read the instructions</b><small>The timer starts only when you press Begin</small></div></li>
      <li><div class="n">3</div><div><b>Answer the questions</b><small>One at a time, with a question map</small></div></li>
      <li><div class="n">4</div><div><b>Get your report card</b><small>Score, grade and answer review</small></div></li>
    </ol>
  </section>

  <section>
    {% if not cfg.is_open %}
      <div class="card">
        <h2>This test is closed</h2>
        <p class="lead" style="margin:6px 0 0;">Please ask your teacher when it will open.</p>
      </div>
    {% elif not n %}
      <div class="card">
        <h2>The test isn't ready yet</h2>
        <p class="lead" style="margin:6px 0 0;">No questions have been added. Please ask your teacher.</p>
      </div>
    {% else %}
      <div class="card">
        <h2 style="margin-bottom:18px;">Your details</h2>
        <form method="POST" action="{{ url_for('start_test') }}">
          <label class="field"><span>Full name</span>
            <input type="text" name="name" required maxlength="60" autocomplete="name" placeholder="Your full name">
          </label>
          <div class="grid2">
            <label class="field"><span>Class</span>
              <input type="text" name="student_class" required maxlength="20" placeholder="e.g. 6th or 10-A">
            </label>
            <label class="field"><span>Semester or term</span>
              <input type="text" name="semester" required maxlength="30" placeholder="e.g. Semester 1">
            </label>
          </div>
          <label class="field"><span>Phone number</span>
            <input type="tel" name="phone" required inputmode="numeric" pattern="[0-9]{10}" maxlength="10" autocomplete="tel" placeholder="10-digit mobile number">
          </label>
          <button class="btn btn-full" type="submit">Continue</button>
        </form>
      </div>
    {% endif %}
    <a class="small-link muted" href="{{ url_for('admin_login') }}">Teacher login</a>
  </section>
</div>
"""


@app.route("/")
def home():
    with closing(get_db()) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    return render_template_string(page(HOME_TEMPLATE), n=count)


@app.route("/start", methods=["POST"])
def start_test():
    settings = cfg()
    if not settings["is_open"]:
        flash("This test is closed right now.", "error")
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()[:60]
    student_class = request.form.get("student_class", "").strip()[:20]
    semester = request.form.get("semester", "").strip()[:30]
    phone = request.form.get("phone", "").strip()

    if not (name and student_class and semester and phone):
        flash("Please fill in all the fields.", "error")
        return redirect(url_for("home"))
    if not re.fullmatch(r"\d{10}", phone):
        flash("Phone number must be exactly 10 digits.", "error")
        return redirect(url_for("home"))

    with closing(get_db()) as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM questions ORDER BY id")]
        if not ids:
            return redirect(url_for("home"))
        if not settings["allow_retake"]:
            dup = conn.execute(
                "SELECT 1 FROM results WHERE lower(student_name) = lower(?) AND phone = ? "
                "AND lower(student_class) = lower(?) AND lower(semester) = lower(?) LIMIT 1",
                (name, phone, student_class, semester),
            ).fetchone()
            if dup:
                flash("You have already submitted this test. Ask your teacher if you need another attempt.", "error")
                return redirect(url_for("home"))

    if settings["shuffle"]:
        random.shuffle(ids)

    for key in ("deadline", "started_at", "answers", "time_taken", "submitted_at"):
        session.pop(key, None)
    session["student"] = {
        "name": name,
        "student_class": student_class,
        "semester": semester,
        "phone": phone,
    }
    session["submitted"] = False
    session["order"] = ids
    return redirect(url_for("instructions"))


# ----------------------------------------------------------------------
# STUDENT FLOW  2) instructions
# ----------------------------------------------------------------------
INSTRUCTIONS_TEMPLATE = """
<div class="narrow">
  <section class="card">
    <div class="who">
      <div class="avatar">{{ student.name[0]|upper }}</div>
      <div>
        <div class="who-name">{{ student.name }}</div>
        <div class="muted">Class {{ student.student_class }}, {{ student.semester }}</div>
      </div>
    </div>
    <h1>Before you begin</h1>
    <ul class="rules">
      <li><span>The test has <b>{{ n }} question{{ '' if n == 1 else 's' }}</b>. Each one has exactly one correct answer.</span></li>
      <li><span>You have <b>{{ cfg.minutes }} minutes</b>. The timer starts when you press Begin test.</span></li>
      <li><span>Move between questions, change answers and mark questions to review before you submit.</span></li>
      <li><span>Your answers are saved on this device, so a page refresh will not lose them.</span></li>
      <li><span>The test submits by itself when the time is over.</span></li>
    </ul>
    <form method="POST" action="{{ url_for('begin_test') }}">
      <button class="btn btn-full" type="submit">Begin test</button>
    </form>
    <a class="small-link muted" href="{{ url_for('home') }}">Change my details</a>
  </section>
</div>
"""


@app.route("/instructions")
def instructions():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    if session.get("deadline"):
        return redirect(url_for("test_page"))
    with closing(get_db()) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    return render_template_string(page(INSTRUCTIONS_TEMPLATE), student=session["student"], n=count)


@app.route("/begin", methods=["POST"])
def begin_test():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    if not session.get("deadline"):
        now = int(time.time())
        session["started_at"] = now
        session["deadline"] = now + cfg()["minutes"] * 60
    return redirect(url_for("test_page"))


# ----------------------------------------------------------------------
# STUDENT FLOW  3) the test
# ----------------------------------------------------------------------
TEST_TEMPLATE = """
<div class="narrow">
<div class="testbar">
  <div class="tb-row">
    <div>
      <div class="tb-name">{{ student.name }}</div>
      <div class="tb-meta">Class {{ student.student_class }}, {{ student.semester }}</div>
    </div>
    <div id="timer" class="timer" role="timer">Time left <span id="time-left">--:--</span></div>
  </div>
  <div class="track"><div class="bar" id="bar"></div></div>
  <div class="tb-count"><span id="answered">0</span> of {{ questions|length }} answered</div>
</div>

<noscript><div class="flash error">Please turn on JavaScript in your browser to take this test.</div></noscript>

<div class="palette">
  <div class="pal-top">
    <b>Question map</b>
    <button class="btn btn-sm" type="button" id="finish-btn">Review and submit</button>
  </div>
  <div class="pal-grid">
    {% for q in questions %}<button type="button" class="pb" data-i="{{ loop.index0 }}" aria-label="Go to question {{ loop.index }}">{{ loop.index }}</button>{% endfor %}
  </div>
  <div class="legend">
    <span><i class="l-ans"></i>Answered</span>
    <span><i class="l-cur"></i>Current</span>
    <span><i class="l-flag"></i>Marked for review</span>
  </div>
</div>

<form method="POST" action="{{ url_for('submit_test') }}" id="quiz-form">
  {% for q in questions %}
  <section class="q" data-i="{{ loop.index0 }}">
    <div class="q-head">
      <div class="q-num">{{ loop.index }}</div>
      <div class="q-text">{{ q.question_text }}</div>
    </div>
    <label class="opt"><input type="radio" name="q_{{ q.id }}" value="a"><span class="letter">A</span><span>{{ q.option_a }}</span></label>
    <label class="opt"><input type="radio" name="q_{{ q.id }}" value="b"><span class="letter">B</span><span>{{ q.option_b }}</span></label>
    <label class="opt"><input type="radio" name="q_{{ q.id }}" value="c"><span class="letter">C</span><span>{{ q.option_c }}</span></label>
    <label class="opt"><input type="radio" name="q_{{ q.id }}" value="d"><span class="letter">D</span><span>{{ q.option_d }}</span></label>
  </section>
  {% endfor %}

  <div class="qnav">
    <button class="btn btn-ghost prev" type="button" id="prev-btn">Previous</button>
    <button class="btn btn-ghost mark" type="button" id="mark-btn">Mark for review</button>
    <button class="btn next" type="button" id="next-btn">Next</button>
  </div>
  <p class="hint no-print">Tip: press 1 to 4 to choose an option, and the arrow keys to move between questions.</p>
</form>

<dialog class="dlg" id="summary">
  <h2>Ready to submit?</h2>
  <p class="muted" id="sum-text" style="margin:4px 0 0;"></p>
  <div class="dlg-sec" id="sum-un" hidden><b>Not answered yet (tap to go there)</b><div class="jump" id="sum-un-list"></div></div>
  <div class="dlg-sec" id="sum-fl" hidden><b>Marked for review</b><div class="jump" id="sum-fl-list"></div></div>
  <div class="btn-row">
    <button class="btn btn-ghost" type="button" id="keep-btn">Keep working</button>
    <button class="btn" type="button" id="confirm-btn">Submit test</button>
  </div>
</dialog>
</div>

<script>
(function () {
  var TOTAL = {{ questions|length }};
  var endAt = Date.now() + {{ remaining }} * 1000;
  var storeKey = 'stp_answers_{{ deadline }}';

  var form = document.getElementById('quiz-form');
  var qs = [].slice.call(document.querySelectorAll('.q'));
  var pbs = [].slice.call(document.querySelectorAll('.pb'));
  var timeEl = document.getElementById('time-left');
  var timerEl = document.getElementById('timer');
  var bar = document.getElementById('bar');
  var countEl = document.getElementById('answered');
  var prevBtn = document.getElementById('prev-btn');
  var nextBtn = document.getElementById('next-btn');
  var markBtn = document.getElementById('mark-btn');
  var dlg = document.getElementById('summary');
  var state = { cur: 0, flags: {} };
  var sent = false;
  var allow = false;

  function answered(i) { return !!qs[i].querySelector('input:checked'); }
  function answeredCount() {
    var n = 0;
    for (var i = 0; i < TOTAL; i++) if (answered(i)) n++;
    return n;
  }

  function save() {
    try {
      var ans = {};
      [].forEach.call(form.querySelectorAll('input[type=radio]:checked'), function (r) { ans[r.name] = r.value; });
      localStorage.setItem(storeKey, JSON.stringify({ cur: state.cur, flags: state.flags, ans: ans }));
    } catch (e) {}
  }
  function load() {
    try {
      var s = JSON.parse(localStorage.getItem(storeKey) || 'null');
      if (!s) return;
      state.cur = Math.min(Math.max(s.cur || 0, 0), TOTAL - 1);
      state.flags = s.flags || {};
      Object.keys(s.ans || {}).forEach(function (name) {
        var el = form.querySelector('input[name="' + name + '"][value="' + s.ans[name] + '"]');
        if (el) el.checked = true;
      });
    } catch (e) {}
  }

  function render() {
    qs.forEach(function (q, i) { q.classList.toggle('active', i === state.cur); });
    pbs.forEach(function (b, i) {
      b.classList.toggle('current', i === state.cur);
      b.classList.toggle('answered', i !== state.cur && answered(i));
      b.classList.toggle('flagged', !!state.flags[i]);
    });
    var n = answeredCount();
    countEl.textContent = n;
    bar.style.width = (TOTAL ? (n / TOTAL) * 100 : 0) + '%';
    prevBtn.disabled = state.cur === 0;
    nextBtn.textContent = state.cur === TOTAL - 1 ? 'Review and submit' : 'Next';
    var on = !!state.flags[state.cur];
    markBtn.classList.toggle('on', on);
    markBtn.textContent = on ? 'Marked for review' : 'Mark for review';
    markBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  function go(i) {
    state.cur = Math.min(Math.max(i, 0), TOTAL - 1);
    render();
    save();
    window.scrollTo(0, 0);
  }

  function makeJump(listId, wrapId, indexes) {
    var list = document.getElementById(listId);
    var wrap = document.getElementById(wrapId);
    list.innerHTML = '';
    indexes.forEach(function (i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = String(i + 1);
      b.addEventListener('click', function () { dlg.close(); go(i); });
      list.appendChild(b);
    });
    wrap.hidden = indexes.length === 0;
  }
  function openSummary() {
    var un = [], fl = [];
    for (var i = 0; i < TOTAL; i++) {
      if (!answered(i)) un.push(i);
      if (state.flags[i]) fl.push(i);
    }
    document.getElementById('sum-text').textContent =
      'You answered ' + (TOTAL - un.length) + ' of ' + TOTAL + ' questions. You cannot change answers after you submit.';
    makeJump('sum-un-list', 'sum-un', un);
    makeJump('sum-fl-list', 'sum-fl', fl);
    if (dlg.showModal) dlg.showModal();
    else if (confirm('Submit your test now?')) send();
  }

  function send() {
    if (sent) return;
    sent = true;
    allow = true;
    try { localStorage.removeItem(storeKey); } catch (e) {}
    document.getElementById('confirm-btn').disabled = true;
    document.getElementById('confirm-btn').textContent = 'Submitting...';
    form.submit();
  }

  function tick() {
    var left = Math.max(0, Math.round((endAt - Date.now()) / 1000));
    var m = String(Math.floor(left / 60)).padStart(2, '0');
    var s = String(left % 60).padStart(2, '0');
    timeEl.textContent = m + ':' + s;
    timerEl.classList.toggle('warn', left <= 300 && left > 60);
    timerEl.classList.toggle('low', left <= 60);
    if (left === 0) { clearInterval(iv); if (dlg.open) dlg.close(); send(); }
  }

  prevBtn.addEventListener('click', function () { go(state.cur - 1); });
  nextBtn.addEventListener('click', function () {
    if (state.cur === TOTAL - 1) openSummary(); else go(state.cur + 1);
  });
  markBtn.addEventListener('click', function () {
    if (state.flags[state.cur]) delete state.flags[state.cur]; else state.flags[state.cur] = true;
    render(); save();
  });
  pbs.forEach(function (b) { b.addEventListener('click', function () { go(parseInt(b.dataset.i, 10)); }); });
  document.getElementById('finish-btn').addEventListener('click', openSummary);
  document.getElementById('keep-btn').addEventListener('click', function () { dlg.close(); });
  document.getElementById('confirm-btn').addEventListener('click', send);
  form.addEventListener('change', function () { render(); save(); });
  form.addEventListener('submit', function (e) { if (!allow) { e.preventDefault(); openSummary(); } });

  document.addEventListener('keydown', function (e) {
    if (dlg.open || e.ctrlKey || e.metaKey || e.altKey) return;
    var k = e.key.toLowerCase();
    var idx = { '1': 0, '2': 1, '3': 2, '4': 3, 'a': 0, 'b': 1, 'c': 2, 'd': 3 }[k];
    if (idx !== undefined) {
      var radios = qs[state.cur].querySelectorAll('input[type=radio]');
      if (radios[idx]) { radios[idx].checked = true; radios[idx].dispatchEvent(new Event('change', { bubbles: true })); }
    } else if (e.key === 'ArrowRight') { go(state.cur + 1); }
    else if (e.key === 'ArrowLeft') { go(state.cur - 1); }
  });

  window.addEventListener('beforeunload', function (e) {
    if (!sent) { e.preventDefault(); e.returnValue = ''; }
  });

  var iv = setInterval(tick, 500);
  load();
  render();
  tick();
})();
</script>
"""


@app.route("/test")
def test_page():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    if not session.get("deadline"):
        return redirect(url_for("instructions"))
    with closing(get_db()) as conn:
        questions = ordered_questions(conn)
    if not questions:
        return redirect(url_for("home"))
    remaining = max(0, int(session["deadline"]) - int(time.time()))
    return render_template_string(
        page(TEST_TEMPLATE),
        student=session["student"],
        questions=questions,
        remaining=remaining,
        deadline=int(session["deadline"]),
    )


@app.route("/submit", methods=["POST"])
def submit_test():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    if not session.get("deadline"):
        return redirect(url_for("instructions"))

    now = int(time.time())
    started = int(session.get("started_at", now))
    limit = max(0, int(session["deadline"]) - started)
    time_taken = max(0, min(now - started, limit))

    with closing(get_db()) as conn:
        questions = ordered_questions(conn)
        answers = {}
        for q in questions:
            value = request.form.get(f"q_{q['id']}")
            if value in OPTION_KEYS:
                answers[str(q["id"])] = value

        score, _ = evaluate(questions, answers)
        student = session["student"]
        submitted_at = now_str()
        conn.execute(
            "INSERT INTO results (student_name, student_class, semester, phone, score, total, "
            "submitted_at, time_taken, answers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                student["name"],
                student["student_class"],
                student["semester"],
                student["phone"],
                score,
                len(questions),
                submitted_at,
                time_taken,
                json.dumps(answers),
            ),
        )
        conn.commit()

    session["answers"] = answers
    session["time_taken"] = time_taken
    session["submitted_at"] = submitted_at
    session["submitted"] = True
    return redirect(url_for("report"))


# ----------------------------------------------------------------------
# STUDENT FLOW  4) report card
# ----------------------------------------------------------------------
REPORT_TEMPLATE = """
<div class="narrow">
  <div class="print-only" style="text-align:center;margin-bottom:14px;">
    <h2>{{ cfg.school_name }}</h2>
    <div class="muted">{{ cfg.test_title }} &ndash; Report card &ndash; {{ submitted_at }}</div>
  </div>

  <section class="card hero">
    <span class="pill no-print">Report card</span>
    <h1 style="margin-top:12px;">{{ headline }}</h1>
    <p class="lead" style="margin-bottom:0;"><b style="color:var(--ink);">{{ student.name }}</b><br>Class {{ student.student_class }}, {{ student.semester }}</p>
    <div class="ring" id="ring" data-pct="{{ pct }}" style="--pct:{{ pct }};"><span id="pct-val">{{ pct }}%</span></div>
    <h2>{{ score }} out of {{ total }} correct</h2>
    <div class="grade">Grade {{ grade }}</div>
    <p class="muted" style="margin:0;">{{ message }}</p>
    <div class="chips">
      <div class="chip ok"><b>{{ score }}</b><small>Correct</small></div>
      <div class="chip bad"><b>{{ wrong }}</b><small>Wrong</small></div>
      <div class="chip"><b>{{ skipped }}</b><small>Skipped</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
    </div>
  </section>

  <section class="card">
    <div class="rv-head">
      <h2>Answer review</h2>
      <div class="seg no-print" role="group" aria-label="Filter answers">
        <button type="button" class="on" data-filter="all">All</button>
        <button type="button" data-filter="review">Needs review</button>
      </div>
    </div>
    <div class="rv-list">
      {% for r in review %}
      <div class="rv" data-status="{{ r.status }}">
        <div class="rv-mark {{ r.status }}">{% if r.status == 'ok' %}&#10003;{% elif r.status == 'bad' %}&#10005;{% else %}&ndash;{% endif %}</div>
        <div>
          <div class="rv-q">{{ r.number }}. {{ r.question }}</div>
          <div class="rv-a">Your answer: <b>{{ r.your }}</b></div>
          {% if r.status != 'ok' %}<div class="rv-a">Correct answer: <b class="good">{{ r.correct }}</b></div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    <div class="btn-row no-print" style="margin-top:20px;">
      <button class="btn" type="button" onclick="window.print()">Print or save as PDF</button>
      <a class="btn btn-ghost" href="{{ url_for('home') }}">Back to start</a>
    </div>
  </section>
</div>

<script>
(function () {
  var ring = document.getElementById('ring');
  var val = document.getElementById('pct-val');
  var target = parseInt(ring.dataset.pct, 10) || 0;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce && target > 0) {
    var t0 = null, dur = 900;
    ring.style.setProperty('--pct', 0);
    val.textContent = '0%';
    var step = function (ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      var cur = Math.round(target * eased);
      ring.style.setProperty('--pct', cur);
      val.textContent = cur + '%';
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  var btns = [].slice.call(document.querySelectorAll('.seg button'));
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      btns.forEach(function (x) { x.classList.toggle('on', x === b); });
      var only = b.dataset.filter === 'review';
      [].forEach.call(document.querySelectorAll('.rv'), function (r) {
        r.hidden = only && r.dataset.status === 'ok';
      });
    });
  });
})();
</script>
"""


@app.route("/report")
def report():
    if "student" not in session or not session.get("submitted"):
        return redirect(url_for("home"))

    with closing(get_db()) as conn:
        questions = ordered_questions(conn)

    score, review = evaluate(questions, session.get("answers", {}))
    total = len(questions)
    pct = round((score / total) * 100) if total else 0
    student = session["student"]
    grade, headline, message = grade_for(pct, student["name"].split()[0])

    return render_template_string(
        page(REPORT_TEMPLATE),
        student=student,
        score=score,
        total=total,
        pct=pct,
        grade=grade,
        headline=headline,
        message=message,
        review=review,
        wrong=sum(1 for r in review if r["status"] == "bad"),
        skipped=sum(1 for r in review if r["status"] == "skip"),
        time_text=fmt_duration(session.get("time_taken")),
        submitted_at=session.get("submitted_at", ""),
    )


# ----------------------------------------------------------------------
# ADMIN
# ----------------------------------------------------------------------
def admin_required():
    return session.get("is_admin", False)


LOGIN_TEMPLATE = """
<div class="narrow" style="max-width:440px;">
  <section class="card">
    <h1>Teacher login</h1>
    <p class="lead">Sign in to manage questions and see results.</p>
    <form method="POST">
      <label class="field"><span>Password</span>
        <input type="password" name="password" required autocomplete="current-password" placeholder="Admin password">
      </label>
      <button class="btn btn-full" type="submit">Log in</button>
    </form>
  </section>
  <a class="small-link muted" href="{{ url_for('home') }}">Back to the test</a>
</div>
"""


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if admin_required():
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied.encode(), ADMIN_PASSWORD.encode()):
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect password. Please try again.", "error")
    return render_template_string(page(LOGIN_TEMPLATE))


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


DASHBOARD_TEMPLATE = """
<div class="dash-top">
  <div>
    <h1>Dashboard</h1>
    <p class="lead" style="margin:0;">
      <span class="pill {{ 'ok' if cfg.is_open else 'bad' }}">{{ 'Test is open' if cfg.is_open else 'Test is closed' }}</span>
    </p>
  </div>
  <div class="btn-row">
    <a class="btn" href="{{ url_for('download_csv') }}">Download results (CSV)</a>
    <a class="btn btn-ghost" href="{{ url_for('home') }}" target="_blank" rel="noopener">Student page</a>
    <a class="btn btn-ghost" href="{{ url_for('admin_logout') }}">Log out</a>
  </div>
</div>

<div class="tabs" role="tablist">
  <button class="tab" type="button" data-tab="overview">Overview</button>
  <button class="tab" type="button" data-tab="questions">Questions</button>
  <button class="tab" type="button" data-tab="results">Results</button>
  <button class="tab" type="button" data-tab="settings">Settings</button>
</div>

<!-- OVERVIEW -->
<div class="panel" data-panel="overview">
  <div class="stats">
    <div class="stat"><b>{{ stats.count }}</b><span>Submissions</span></div>
    <div class="stat"><b>{{ stats.avg }}%</b><span>Average score</span></div>
    <div class="stat"><b>{{ stats.best }}%</b><span>Highest score</span></div>
    <div class="stat"><b>{{ stats.avg_time }}</b><span>Average time</span></div>
    <div class="stat"><b>{{ questions|length }}</b><span>Questions</span></div>
  </div>

  <div class="two">
    <section class="card">
      <h2>Grade distribution</h2>
      <p class="muted" style="margin:0 0 8px;">How many students earned each grade.</p>
      {% for b in bands %}
      <div class="brow"><span>{{ b.label }}</span><div class="meter"><i style="width:{{ b.width }}%;"></i></div><span>{{ b.count }}</span></div>
      {% endfor %}
    </section>
    <section class="card">
      <h2>Hardest questions</h2>
      <p class="muted" style="margin:0 0 8px;">Lowest share of students who got them right.</p>
      {% for it in hardest %}
      <div class="irow" style="grid-template-columns:1fr 44px;">
        <div class="qt" title="{{ it.text }}">Q{{ it.number }}. {{ it.text }}</div><div class="pc">{{ it.pct }}%</div>
      </div>
      {% else %}
      <p class="muted" style="margin:8px 0 0;">Appears after the first submission.</p>
      {% endfor %}
    </section>
  </div>

  <section class="card">
    <h2>Question-wise analysis</h2>
    <p class="muted" style="margin:0 0 8px;">Percentage of students who answered each question correctly.</p>
    {% for it in items %}
    <div class="irow">
      <div class="qt" title="{{ it.text }}">Q{{ it.number }}. {{ it.text }}</div>
      {% if it.pct is not none %}
      <div class="meter {{ it.level }}"><i style="width:{{ it.pct }}%;"></i></div><div class="pc">{{ it.pct }}%</div>
      {% else %}
      <div class="muted">No data</div><div></div>
      {% endif %}
    </div>
    {% else %}
    <p class="muted" style="margin:8px 0 0;">Add questions to see analysis.</p>
    {% endfor %}
  </section>
</div>

<!-- QUESTIONS -->
<div class="panel" data-panel="questions">
  <div class="two">
    <section class="card">
      <h2>Add one question</h2>
      <p class="lead" style="margin-bottom:18px;">Fill in four options and choose the correct one.</p>
      <form method="POST" action="{{ url_for('add_question') }}">
        <label class="field"><span>Question</span>
          <input type="text" name="question_text" required placeholder="Type the question">
        </label>
        <div class="grid2">
          <label class="field"><span>Option A</span><input type="text" name="option_a" required></label>
          <label class="field"><span>Option B</span><input type="text" name="option_b" required></label>
          <label class="field"><span>Option C</span><input type="text" name="option_c" required></label>
          <label class="field"><span>Option D</span><input type="text" name="option_d" required></label>
        </div>
        <label class="field"><span>Correct option</span>
          <select name="correct_option" required>
            <option value="a">A</option><option value="b">B</option><option value="c">C</option><option value="d">D</option>
          </select>
        </label>
        <button class="btn btn-full" type="submit">Add question</button>
      </form>
    </section>

    <section class="card">
      <h2>Add many at once</h2>
      <p class="lead" style="margin-bottom:12px;">One question per line, separated by the | symbol. The last part is the correct letter.</p>
      <code class="fmt">Question | Option A | Option B | Option C | Option D | B</code>
      <form method="POST" action="{{ url_for('bulk_add') }}">
        <label class="field"><span>Questions</span>
          <textarea name="bulk" rows="9" required placeholder="What is 2 + 2? | 3 | 4 | 5 | 6 | b&#10;Capital of India? | Mumbai | Delhi | Chennai | Kolkata | b"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add all questions</button>
      </form>
    </section>
  </div>

  <section class="card">
    <h2>All questions ({{ questions|length }})</h2>
    {% for q in questions %}
    <div class="qrow">
      <div>
        <b>{{ loop.index }}. {{ q.question_text }}</b>
        <div class="opts">
          {% for letter, key in [('A','option_a'),('B','option_b'),('C','option_c'),('D','option_d')] %}
            <span class="{{ 'right' if letter|lower == q.correct_option else '' }}">{{ letter }}. {{ q[key] }}</span>{% if not loop.last %} &nbsp; {% endif %}
          {% endfor %}
        </div>
      </div>
      <form method="POST" action="{{ url_for('delete_question', qid=q.id) }}" onsubmit="return confirm('Delete this question?');">
        <button class="btn btn-danger btn-sm" type="submit">Delete</button>
      </form>
    </div>
    {% else %}
    <p class="muted" style="margin:8px 0 0;">No questions yet. Add your first one above.</p>
    {% endfor %}
  </section>
</div>

<!-- RESULTS -->
<div class="panel" data-panel="results">
  <section class="card">
    <div class="tools">
      <div>
        <h2>Results</h2>
        <span class="muted">Latest {{ results|length }} of {{ stats.count }}. The CSV has everyone, plus question-wise marks.</span>
      </div>
      <input type="text" id="search" placeholder="Search name, class or phone" aria-label="Search results">
    </div>
    {% if results %}
    <div class="tscroll">
      <table id="results-table">
        <thead><tr><th>Name</th><th>Class</th><th>Semester</th><th>Phone</th><th>Score</th><th>%</th><th>Grade</th><th>Time</th><th>Submitted</th><th></th></tr></thead>
        <tbody>
        {% for r in results %}
        <tr>
          <td>{{ r.student_name }}</td><td>{{ r.student_class }}</td><td>{{ r.semester }}</td><td>{{ r.phone }}</td>
          <td>{{ r.score }}/{{ r.total }}</td><td>{{ r.pct }}%</td><td>{{ r.grade }}</td><td>{{ r.time }}</td><td>{{ r.submitted_at }}</td>
          <td>
            <form method="POST" action="{{ url_for('delete_result', rid=r.id) }}" onsubmit="return confirm('Delete this result?');">
              <button class="btn btn-danger btn-sm" type="submit">Delete</button>
            </form>
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <p class="muted" style="margin:8px 0 0;">No submissions yet.</p>
    {% endif %}
  </section>
</div>

<!-- SETTINGS -->
<div class="panel" data-panel="settings">
  <div class="two">
    <section class="card">
      <h2>Test settings</h2>
      <p class="lead" style="margin-bottom:18px;">These apply to the next student who starts.</p>
      <form method="POST" action="{{ url_for('save_settings') }}">
        <label class="field"><span>School or coaching name</span>
          <input type="text" name="school_name" maxlength="60" value="{{ cfg.school_name }}" required>
        </label>
        <label class="field"><span>Test title</span>
          <input type="text" name="test_title" maxlength="80" value="{{ cfg.test_title }}" required>
        </label>
        <label class="field"><span>Time limit (minutes)</span>
          <input type="number" name="minutes" min="1" max="240" value="{{ cfg.minutes }}" required>
        </label>
        <label class="check"><input type="checkbox" name="is_open" {{ 'checked' if cfg.is_open }}>
          <span><b>Test is open</b><small>Turn off to stop new students from starting.</small></span></label>
        <label class="check"><input type="checkbox" name="shuffle" {{ 'checked' if cfg.shuffle }}>
          <span><b>Shuffle question order</b><small>Each student sees a different order, so neighbours can't copy.</small></span></label>
        <label class="check"><input type="checkbox" name="allow_retake" {{ 'checked' if cfg.allow_retake }}>
          <span><b>Allow retakes</b><small>If off, the same name, class, semester and phone can submit only once.</small></span></label>
        <button class="btn btn-full" type="submit">Save settings</button>
      </form>
    </section>

    <section class="card">
      <h2>Danger zone</h2>
      <p class="lead" style="margin-bottom:16px;">These cannot be undone. Download the CSV first if you need the data.</p>
      <form method="POST" action="{{ url_for('reset_results') }}" onsubmit="return confirm('All results will be deleted permanently. Continue?');" style="margin-bottom:12px;">
        <button class="btn btn-danger" type="submit">Delete all results</button>
      </form>
      <form method="POST" action="{{ url_for('delete_all_questions') }}" onsubmit="return confirm('All questions will be deleted permanently. Continue?');">
        <button class="btn btn-danger" type="submit">Delete all questions</button>
      </form>
    </section>
  </div>
</div>

<script>
(function () {
  var tabs = [].slice.call(document.querySelectorAll('.tab'));
  var panels = [].slice.call(document.querySelectorAll('.panel'));
  function show(name) {
    if (!panels.some(function (p) { return p.dataset.panel === name; })) name = 'overview';
    tabs.forEach(function (t) {
      var on = t.dataset.tab === name;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    panels.forEach(function (p) { p.classList.toggle('active', p.dataset.panel === name); });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      history.replaceState(null, '', '#' + t.dataset.tab);
      show(t.dataset.tab);
    });
  });
  show(location.hash.slice(1) || 'overview');

  var search = document.getElementById('search');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.toLowerCase();
      [].forEach.call(document.querySelectorAll('#results-table tbody tr'), function (tr) {
        tr.hidden = tr.textContent.toLowerCase().indexOf(q) === -1;
      });
    });
  }
})();
</script>
"""


def build_dashboard_data(conn):
    questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    rows = conn.execute("SELECT * FROM results ORDER BY id DESC").fetchall()

    band_counts = {"A+": 0, "A": 0, "B": 0, "C": 0, "D": 0}
    pcts, times = [], []
    per_q = {q["id"]: [0, 0] for q in questions}  # [correct, attempted]
    results = []

    for r in rows:
        pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
        grade = grade_for(pct)[0]
        band_counts[grade] += 1
        pcts.append(pct)
        if r["time_taken"] is not None:
            times.append(r["time_taken"])
        if r["answers"]:
            try:
                ans = json.loads(r["answers"])
            except ValueError:
                ans = {}
            for q in questions:
                per_q[q["id"]][1] += 1
                if ans.get(str(q["id"])) == q["correct_option"]:
                    per_q[q["id"]][0] += 1
        if len(results) < 200:
            d = dict(r)
            d["pct"] = pct
            d["grade"] = grade
            d["time"] = fmt_clock(r["time_taken"]) or "-"
            results.append(d)

    top = max(band_counts.values()) if band_counts else 0
    bands = [
        {"label": k, "count": v, "width": round(v / top * 100) if top else 0}
        for k, v in band_counts.items()
    ]

    items = []
    for i, q in enumerate(questions, start=1):
        correct, attempted = per_q[q["id"]]
        pct = round(correct / attempted * 100) if attempted else None
        level = "" if pct is None else ("good" if pct >= 70 else "mid" if pct >= 40 else "low")
        items.append({"number": i, "text": q["question_text"], "pct": pct, "level": level})
    hardest = sorted([x for x in items if x["pct"] is not None], key=lambda x: x["pct"])[:3]

    stats = {
        "count": len(rows),
        "avg": round(sum(pcts) / len(pcts)) if pcts else 0,
        "best": max(pcts) if pcts else 0,
        "avg_time": fmt_clock(sum(times) / len(times)) if times else "-",
    }
    return questions, results, stats, bands, items, hardest


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        questions, results, stats, bands, items, hardest = build_dashboard_data(conn)
    return render_template_string(
        page(DASHBOARD_TEMPLATE),
        questions=questions,
        results=results,
        stats=stats,
        bands=bands,
        items=items,
        hardest=hardest,
    )


@app.route("/admin/add_question", methods=["POST"])
def add_question():
    if not admin_required():
        return redirect(url_for("admin_login"))

    text = request.form.get("question_text", "").strip()
    options = [request.form.get(f"option_{k}", "").strip() for k in "abcd"]
    correct = request.form.get("correct_option", "")

    if not text or not all(options) or correct not in OPTION_KEYS:
        flash("Please fill in the question, all four options and the correct answer.", "error")
        return back_to("questions")

    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text, *options, correct),
        )
        conn.commit()
    flash("Question added.", "success")
    return back_to("questions")


@app.route("/admin/bulk_add", methods=["POST"])
def bulk_add():
    if not admin_required():
        return redirect(url_for("admin_login"))

    rows, bad_lines = [], []
    for number, raw in enumerate(request.form.get("bulk", "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 6 or not all(parts) or parts[5].lower() not in OPTION_KEYS:
            bad_lines.append(str(number))
            continue
        rows.append((parts[0], parts[1], parts[2], parts[3], parts[4], parts[5].lower()))

    if rows:
        with closing(get_db()) as conn:
            conn.executemany(
                "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        flash(f"{len(rows)} question(s) added.", "success")
    if bad_lines:
        flash("Skipped line(s) " + ", ".join(bad_lines) + ". Each line needs 6 parts: question, 4 options, correct letter.", "error")
    if not rows and not bad_lines:
        flash("Nothing to add. Paste at least one question.", "error")
    return back_to("questions")


@app.route("/admin/delete_question/<int:qid>", methods=["POST"])
def delete_question(qid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
        conn.commit()
    flash("Question deleted.", "success")
    return back_to("questions")


@app.route("/admin/delete_all_questions", methods=["POST"])
def delete_all_questions():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM questions")
        conn.commit()
    flash("All questions deleted.", "success")
    return back_to("settings")


@app.route("/admin/delete_result/<int:rid>", methods=["POST"])
def delete_result(rid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM results WHERE id = ?", (rid,))
        conn.commit()
    flash("Result deleted.", "success")
    return back_to("results")


@app.route("/admin/reset_results", methods=["POST"])
def reset_results():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM results")
        conn.commit()
    flash("All results deleted.", "success")
    return back_to("settings")


@app.route("/admin/settings", methods=["POST"])
def save_settings():
    if not admin_required():
        return redirect(url_for("admin_login"))
    try:
        minutes = max(1, min(240, int(request.form.get("minutes", "15"))))
    except ValueError:
        minutes = 15
    values = {
        "school_name": request.form.get("school_name", "").strip()[:60] or DEFAULT_SETTINGS["school_name"],
        "test_title": request.form.get("test_title", "").strip()[:80] or DEFAULT_SETTINGS["test_title"],
        "minutes": str(minutes),
        "is_open": "1" if request.form.get("is_open") else "0",
        "shuffle": "1" if request.form.get("shuffle") else "0",
        "allow_retake": "1" if request.form.get("allow_retake") else "0",
    }
    with closing(get_db()) as conn:
        for key, value in values.items():
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    flash("Settings saved.", "success")
    return back_to("settings")


@app.route("/admin/download_csv")
def download_csv():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
        rows = conn.execute("SELECT * FROM results ORDER BY id").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Name", "Class", "Semester", "Phone", "Score", "Total", "Percentage", "Grade", "Time Taken (m:ss)", "Submitted At"]
        + [f"Q{i}" for i in range(1, len(questions) + 1)]
    )
    for r in rows:
        pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
        try:
            ans = json.loads(r["answers"]) if r["answers"] else None
        except ValueError:
            ans = None
        marks = []
        for q in questions:  # 1 = correct, 0 = wrong or skipped, blank = no data
            if ans is None:
                marks.append("")
            else:
                marks.append(1 if ans.get(str(q["id"])) == q["correct_option"] else 0)
        writer.writerow(
            [
                csv_safe(r["student_name"]),
                csv_safe(r["student_class"]),
                csv_safe(r["semester"]),
                r["phone"],
                r["score"],
                r["total"],
                pct,
                grade_for(pct)[0],
                fmt_clock(r["time_taken"]),
                r["submitted_at"],
            ]
            + marks
        )

    filename = "test_results_" + datetime.now(IST).strftime("%Y%m%d_%H%M") + ".csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),  # BOM so Excel opens it correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
