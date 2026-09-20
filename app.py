"""
Student Test Portal (v4) - single-file Flask app: SQLite + HTML + CSS + JavaScript.

STUDENT FLOW
    Details -> Instructions -> Timed test (one question at a time) -> Report card

ADMIN FLOW  (/admin)
    Overview (analysis + share link) | Questions (add / bulk add / edit / backup)
    Results (search, sort, per-student answers) | Settings

Design notes (v4)
    Palette and type are built from exam-hall stationery: ballpoint blue, red-pen
    corrections, pencil-grey rules on warm paper. Fraunces sets headlines, IBM Plex
    Sans sets everything else, IBM Plex Mono is kept for the clock only.
    Light and dark are one token set; the switch in the header remembers itself.

Optional environment variables (everything else is set from Admin > Settings):
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
    abort,
    flash,
    g,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # correct https links on Render
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
                correct_option TEXT NOT NULL,
                explanation TEXT
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
                answers TEXT,
                focus_lost INTEGER
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # upgrade databases created by older versions
        q_cols = {r["name"] for r in conn.execute("PRAGMA table_info(questions)")}
        if "explanation" not in q_cols:
            conn.execute("ALTER TABLE questions ADD COLUMN explanation TEXT")
        r_cols = {r["name"] for r in conn.execute("PRAGMA table_info(results)")}
        for col, ddl in (("time_taken", "INTEGER"), ("answers", "TEXT"), ("focus_lost", "INTEGER")):
            if col not in r_cols:
                conn.execute(f"ALTER TABLE results ADD COLUMN {col} {ddl}")
        conn.commit()


init_db()

DEFAULT_SETTINGS = {
    "school_name": "Student Test Portal",
    "test_title": "Class Test",
    "minutes": "15",
    "is_open": "1",
    "shuffle": "1",
    "allow_retake": "0",
    "show_stats": "1",
    "track_focus": "1",
    "note": "",
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
        "show_stats": raw["show_stats"] == "1",
        "track_focus": raw["track_focus"] == "1",
        "note": raw["note"],
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
# SHARED LAYOUT + DESIGN SYSTEM
# ----------------------------------------------------------------------
LAYOUT_TOP = """{% macro icon(name) %}<svg class="ic" aria-hidden="true"><use href="#i-{{ name }}"/></svg>{% endmacro %}<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#FBFAF7">
<title>{{ cfg.school_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<script>
(function () {
  try {
    var t = localStorage.getItem('stp_theme');
    if (!t) t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();
</script>
<style>
:root{
  /* paper and ink */
  --paper:#FBFAF7;
  --surface:#FFFFFF;
  --surface-2:#F5F3EC;
  --ink:#141A2C;
  --ink-2:#575E77;
  --ink-3:#8A8FA3;
  --rule:#E5E1D6;
  --rule-soft:#F0ECE2;
  /* ballpoint blue, red pen, green tick, pencil amber */
  --blue:#2E44C4;
  --blue-deep:#223196;
  --blue-wash:#ECEEFA;
  --on-blue:#FFFFFF;
  --red:#CE4130;
  --red-wash:#FBEBE7;
  --green:#15764A;
  --green-wash:#E6F3EC;
  --amber:#B4801A;
  --amber-wash:#F9F0DA;
  --shadow-sm:0 1px 2px rgba(20,26,44,.06);
  --shadow:0 2px 4px rgba(20,26,44,.05), 0 14px 32px -18px rgba(20,26,44,.35);
  --r-lg:20px;
  --r-md:13px;
  --r-sm:9px;
  --sans:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --serif:'Fraunces',Georgia,'Times New Roman',serif;
  --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}
[data-theme=dark]{
  --paper:#101320;
  --surface:#171B29;
  --surface-2:#1E2334;
  --ink:#ECEEF6;
  --ink-2:#A5ABC1;
  --ink-3:#7B8197;
  --rule:#2A3043;
  --rule-soft:#222738;
  --blue:#8496FF;
  --blue-deep:#A7B3FF;
  --blue-wash:#1E2440;
  --on-blue:#111527;
  --red:#F08268;
  --red-wash:#3A2119;
  --green:#5FCB97;
  --green-wash:#14301F;
  --amber:#E7B65A;
  --amber-wash:#332714;
  --shadow-sm:0 1px 2px rgba(0,0,0,.4);
  --shadow:0 2px 4px rgba(0,0,0,.3), 0 18px 40px -22px rgba(0,0,0,.9);
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;scroll-padding-top:env(safe-area-inset-top,0px);}
body{
  margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--sans);font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;
  padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);
}
a{color:var(--blue);text-decoration:none;}
a:hover{text-decoration:underline;}
[hidden]{display:none !important;}
.ic{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;}
::selection{background:var(--blue-wash);}

/* ---------- page frame ---------- */
.topbar{
  position:sticky;top:0;z-index:30;background:var(--paper);
  border-bottom:1px solid var(--rule);
  padding-top:env(safe-area-inset-top,0px);
}
.topbar-in{
  max-width:1060px;margin:0 auto;padding:12px 20px;
  display:flex;align-items:center;justify-content:space-between;gap:16px;
}
.topbar-in.wide,.wrap.wide{max-width:1220px;}
.brand{display:flex;align-items:center;gap:11px;min-width:0;color:inherit;}
.brand:hover{text-decoration:none;}
.mark{
  flex:none;width:34px;height:34px;border-radius:50%;background:var(--blue);color:var(--on-blue);
  display:grid;place-items:center;box-shadow:0 0 0 4px var(--blue-wash);
}
.mark .ic{width:17px;height:17px;stroke-width:3;}
.brand-text{min-width:0;}
.brand-name{
  font-family:var(--serif);font-weight:600;font-size:1.06rem;line-height:1.15;letter-spacing:-.01em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.brand-sub{font-size:.78rem;color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.topbar-right{display:flex;align-items:center;gap:8px;flex:none;}
.icon-btn{
  width:38px;height:38px;border-radius:11px;border:1px solid var(--rule);background:var(--surface);
  color:var(--ink-2);display:grid;place-items:center;cursor:pointer;padding:0;
}
.icon-btn:hover{color:var(--ink);border-color:var(--ink-3);}
[data-theme=light] .moon{display:block;}
[data-theme=light] .sun{display:none;}
[data-theme=dark] .moon{display:none;}
[data-theme=dark] .sun{display:block;}
.wrap{max-width:1060px;margin:0 auto;padding:30px 20px 64px;}
.narrow{max-width:700px;margin:0 auto;}
.foot{
  border-top:1px solid var(--rule);margin-top:10px;
}
.foot-in{
  max-width:1060px;margin:0 auto;padding:22px 20px;display:flex;justify-content:space-between;
  gap:12px;flex-wrap:wrap;color:var(--ink-3);font-size:.84rem;
}

/* ---------- type ---------- */
h1{font-family:var(--serif);font-size:2rem;line-height:1.14;margin:0 0 10px;font-weight:600;letter-spacing:-.02em;}
h2{font-family:var(--serif);font-size:1.22rem;line-height:1.25;margin:0 0 4px;font-weight:600;letter-spacing:-.01em;}
h3{font-size:1rem;margin:0 0 4px;font-weight:600;}
.lead{color:var(--ink-2);margin:0 0 24px;max-width:58ch;}
.muted{color:var(--ink-2);font-size:.9rem;}
.tabular{font-variant-numeric:tabular-nums;}

/* ---------- cards, pills ---------- */
.card{
  border:1px solid var(--rule);border-radius:var(--r-lg);padding:28px;margin-bottom:20px;
  background:var(--surface);box-shadow:var(--shadow-sm);
}
.pill{
  display:inline-flex;align-items:center;gap:7px;padding:5px 13px;border-radius:999px;
  background:var(--blue-wash);color:var(--blue-deep);font-weight:600;font-size:.83rem;
}
.pill.ok{background:var(--green-wash);color:var(--green);}
.pill.bad{background:var(--red-wash);color:var(--red);}
.pill .ic{width:14px;height:14px;}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none;}

/* ---------- forms ---------- */
.field{display:block;margin-bottom:16px;}
.field > span{display:block;font-weight:600;font-size:.88rem;margin-bottom:6px;}
.field > span small{font-weight:400;color:var(--ink-2);}
input[type=text],input[type=tel],input[type=password],input[type=number],select,textarea{
  width:100%;padding:12px 14px;border:1px solid var(--rule);border-radius:var(--r-md);
  font:inherit;font-size:16px;color:var(--ink);background:var(--surface);outline:none;
  transition:border-color .15s, box-shadow .15s;
}
[data-theme=dark] input,[data-theme=dark] select,[data-theme=dark] textarea{background:var(--surface-2);}
textarea{resize:vertical;line-height:1.55;}
input::placeholder,textarea::placeholder{color:var(--ink-3);}
input:focus,select:focus,textarea:focus{border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-wash);}
input[readonly]{background:var(--surface-2);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;}
.check{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;cursor:pointer;}
.check input{width:20px;height:20px;margin-top:2px;accent-color:var(--blue);flex:none;}
.check b{display:block;font-size:.94rem;font-weight:600;}
.check small{color:var(--ink-2);}
.pw{position:relative;}
.pw input{padding-right:48px;}
.pw button{position:absolute;right:5px;top:5px;width:38px;height:38px;border:none;background:none;color:var(--ink-3);cursor:pointer;border-radius:9px;display:grid;place-items:center;}
.pw button:hover{background:var(--surface-2);color:var(--ink);}

/* ---------- buttons ---------- */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:12px 20px;border-radius:var(--r-md);border:1px solid var(--blue);
  background:var(--blue);color:var(--on-blue);font:inherit;font-weight:600;cursor:pointer;
  text-decoration:none;transition:background .15s, border-color .15s, transform .08s;
}
.btn:hover{background:var(--blue-deep);border-color:var(--blue-deep);text-decoration:none;}
.btn:active{transform:translateY(1px);}
.btn .ic{width:17px;height:17px;}
.btn:focus-visible,.pb:focus-visible,.nav-item:focus-visible,.icon-btn:focus-visible,.pw button:focus-visible,.seg button:focus-visible{outline:2px solid var(--blue);outline-offset:2px;}
.btn-full{width:100%;margin-top:4px;}
.btn-ghost{background:var(--surface);color:var(--ink);border-color:var(--rule);}
.btn-ghost:hover{background:var(--surface-2);border-color:var(--ink-3);color:var(--ink);}
.btn-ghost.on{background:var(--amber-wash);border-color:var(--amber);color:var(--amber);}
.btn-danger{background:var(--surface);color:var(--red);border-color:var(--rule);}
.btn-danger:hover{background:var(--red-wash);border-color:var(--red);color:var(--red);}
.btn-sm{padding:8px 13px;font-size:.87rem;border-radius:var(--r-sm);}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn.busy{position:relative;color:transparent !important;pointer-events:none;}
.btn.busy .ic{opacity:0;}
.btn.busy::after{
  content:"";position:absolute;left:50%;top:50%;width:17px;height:17px;margin:-8.5px 0 0 -8.5px;
  border-radius:50%;border:2.5px solid var(--on-blue);border-right-color:transparent;animation:spin .7s linear infinite;
}
.btn-ghost.busy::after{border-color:var(--ink-2);border-right-color:transparent;}
.btn-danger.busy::after{border-color:var(--red);border-right-color:transparent;}
@keyframes spin{to{transform:rotate(360deg);}}
.small-link{display:block;text-align:center;margin-top:14px;font-size:.88rem;}
.back{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:.88rem;margin-bottom:16px;}

/* ---------- flash + toasts ---------- */
.flash{
  display:flex;gap:10px;align-items:flex-start;padding:12px 15px;border-radius:var(--r-md);
  margin-bottom:18px;font-size:.93rem;background:var(--blue-wash);color:var(--blue-deep);
  border-left:3px solid var(--blue);transition:opacity .4s;
}
.flash.error{background:var(--red-wash);color:var(--red);border-left-color:var(--red);}
.flash.success{background:var(--green-wash);color:var(--green);border-left-color:var(--green);}
.toasts{position:fixed;left:0;right:0;bottom:calc(20px + env(safe-area-inset-bottom,0px));display:flex;flex-direction:column;align-items:center;gap:8px;z-index:100;pointer-events:none;padding:0 16px;}
.toast{
  background:var(--ink);color:var(--paper);padding:11px 18px;border-radius:var(--r-md);font-weight:500;font-size:.92rem;
  box-shadow:var(--shadow);transition:opacity .3s, transform .3s;max-width:420px;text-align:center;
}
.toast.warn{background:var(--amber);color:#1B1405;}
.toast.out{opacity:0;transform:translateY(8px);}

/* ---------- landing ---------- */
.hero{display:grid;grid-template-columns:1fr;gap:34px;align-items:start;}
@media (min-width:900px){.hero{grid-template-columns:1.05fr .95fr;gap:64px;padding-top:14px;}}
.hero h1{font-size:2.6rem;margin:16px 0 12px;}
.hero .lead{font-size:1.05rem;}
.spec{margin:26px 0 0;border-top:1px solid var(--rule);}
.spec div{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:11px 0;border-bottom:1px solid var(--rule);}
.spec dt{color:var(--ink-2);font-size:.92rem;}
.spec dd{margin:0;font-weight:600;text-align:right;}
.steps{list-style:none;margin:28px 0 0;padding:0;counter-reset:s;}
.steps li{display:flex;gap:14px;position:relative;padding-bottom:18px;}
.steps li:last-child{padding-bottom:0;}
.steps li::after{content:"";position:absolute;left:14px;top:31px;bottom:0;width:1px;background:var(--rule);}
.steps li:last-child::after{display:none;}
.steps .n{
  counter-increment:s;flex:none;width:29px;height:29px;border-radius:50%;
  border:1px solid var(--rule);background:var(--surface);color:var(--ink-2);
  display:grid;place-items:center;font-size:.82rem;font-weight:600;
}
.steps .n::before{content:counter(s);}
.steps b{display:block;font-weight:600;line-height:1.35;padding-top:3px;}
.steps small{color:var(--ink-2);}

/* the one bold element: an exam pad */
.pad{
  position:relative;background:var(--surface);border:1px solid var(--rule);
  border-radius:var(--r-lg);box-shadow:var(--shadow);overflow:hidden;
}
.pad-top{
  padding:18px 26px 16px 46px;border-bottom:1px solid var(--rule);background:var(--surface-2);
  display:flex;justify-content:space-between;align-items:center;gap:12px;
}
.pad-top h2{margin:0;}
.pad-top small{display:block;color:var(--ink-2);font-size:.84rem;}
.pad-body{padding:24px 26px 26px 46px;position:relative;}
.pad::before{
  content:"";position:absolute;top:0;bottom:0;left:30px;width:1px;background:var(--red);opacity:.35;
}
.holes{position:absolute;top:0;bottom:0;left:0;width:30px;display:flex;flex-direction:column;justify-content:space-evenly;align-items:center;padding:26px 0;}
.holes i{width:9px;height:9px;border-radius:50%;background:var(--paper);box-shadow:inset 0 1px 2px rgba(20,26,44,.25);}
.bubbles{display:flex;gap:7px;align-items:center;}
.bubbles i{width:15px;height:15px;border-radius:50%;border:1.5px solid var(--rule);}
.bubbles i.on{background:var(--blue);border-color:var(--blue);}
.empty{text-align:center;padding:10px 0 4px;}
.empty .ic{width:30px;height:30px;color:var(--ink-3);margin-bottom:8px;}

/* ---------- instructions ---------- */
.who{display:flex;align-items:center;gap:14px;margin-bottom:22px;padding-bottom:20px;border-bottom:1px solid var(--rule);}
.avatar{
  width:46px;height:46px;border-radius:50%;background:var(--blue-wash);color:var(--blue-deep);
  display:grid;place-items:center;font-family:var(--serif);font-weight:600;font-size:1.2rem;flex:none;
}
.who-name{font-weight:600;font-size:1.05rem;line-height:1.25;}
.rules{list-style:none;margin:18px 0 22px;padding:0;}
.rules li{display:flex;gap:12px;padding:8px 0;border-bottom:1px dashed var(--rule-soft);}
.rules li:last-child{border-bottom:none;}
.rules .ic{color:var(--blue);margin-top:2px;width:18px;height:18px;}
.note{
  background:var(--amber-wash);border-left:3px solid var(--amber);border-radius:var(--r-sm);
  padding:12px 15px;margin:0 0 20px;white-space:pre-line;color:var(--ink);font-size:.95rem;
}

/* ---------- test ---------- */
.testbar{
  position:sticky;top:0;z-index:20;background:var(--paper);
  margin:0 -20px 18px;padding:calc(12px + env(safe-area-inset-top,0px)) 20px 10px;border-bottom:1px solid var(--rule);
}
.tb-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.tb-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw;}
.tb-meta{font-size:.8rem;color:var(--ink-2);}
.timer{
  display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-weight:600;font-size:1.02rem;
  white-space:nowrap;background:var(--surface);border:1px solid var(--rule);color:var(--ink);
  padding:7px 14px;border-radius:999px;
}
.timer .ic{width:16px;height:16px;color:var(--ink-2);}
.timer.warn{background:var(--amber-wash);border-color:var(--amber);color:var(--amber);}
.timer.warn .ic{color:var(--amber);}
.timer.low{background:var(--red-wash);border-color:var(--red);color:var(--red);}
.timer.low .ic{color:var(--red);}
.track{height:4px;background:var(--rule);border-radius:999px;overflow:hidden;margin-top:11px;}
.bar{height:100%;width:0;background:var(--blue);border-radius:999px;transition:width .25s;}
.tb-count{display:flex;justify-content:space-between;font-size:.79rem;color:var(--ink-2);margin-top:6px;}
.tb-count .saved{display:inline-flex;align-items:center;gap:5px;}
.tb-count .saved .ic{width:13px;height:13px;color:var(--green);}

.palette{border:1px solid var(--rule);border-radius:var(--r-lg);padding:15px 17px;margin-bottom:16px;background:var(--surface);}
.pal-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:13px;}
.pal-top b{font-size:.93rem;font-weight:600;}
.pal-grid{display:flex;flex-wrap:wrap;gap:7px;}
.pb{
  position:relative;width:37px;height:37px;border-radius:50%;border:1px solid var(--rule);background:var(--surface);
  font:inherit;font-weight:600;font-size:.88rem;color:var(--ink-3);cursor:pointer;transition:border-color .12s;
}
.pb:hover{border-color:var(--ink-3);}
.pb.answered{background:var(--blue-wash);border-color:var(--blue-wash);color:var(--blue-deep);}
.pb.current{background:var(--blue);border-color:var(--blue);color:var(--on-blue);}
.pb.flagged::after{content:"";position:absolute;top:-2px;right:-2px;width:11px;height:11px;border-radius:50%;background:var(--amber);border:2px solid var(--surface);}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:.79rem;color:var(--ink-2);margin-top:13px;}
.legend i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;vertical-align:-1px;border:1px solid var(--rule);}
.legend .l-ans{background:var(--blue-wash);border-color:var(--blue-wash);}
.legend .l-cur{background:var(--blue);border-color:var(--blue);}
.legend .l-flag{background:var(--amber);border-color:var(--amber);}

.q{display:none;border:1px solid var(--rule);border-radius:var(--r-lg);padding:26px;background:var(--surface);box-shadow:var(--shadow-sm);}
.q.active{display:block;}
.q-head{display:flex;gap:14px;margin-bottom:20px;}
.q-num{
  flex:none;width:31px;height:31px;border-radius:50%;background:var(--ink);color:var(--paper);
  display:grid;place-items:center;font-weight:600;font-size:.87rem;
}
.q-text{font-family:var(--serif);font-weight:500;font-size:1.22rem;line-height:1.38;padding-top:1px;}
.opt{
  position:relative;display:flex;align-items:center;gap:13px;padding:13px 15px;
  border:1px solid var(--rule);border-radius:var(--r-md);margin-bottom:9px;cursor:pointer;
  transition:border-color .15s, background .15s;
}
.opt:last-child{margin-bottom:0;}
.opt:hover{border-color:var(--ink-3);}
.opt input{position:absolute;opacity:0;pointer-events:none;}
.letter{
  flex:none;width:28px;height:28px;border-radius:50%;background:var(--surface);border:1.5px solid var(--rule);
  display:grid;place-items:center;font-weight:600;font-size:.82rem;color:var(--ink-3);
  transition:background .15s, color .15s, border-color .15s;
}
.opt:has(input:checked){border-color:var(--blue);background:var(--blue-wash);}
.opt input:checked + .letter{background:var(--blue);border-color:var(--blue);color:var(--on-blue);}
.opt:has(input:focus-visible){outline:2px solid var(--blue);outline-offset:2px;}
.qnav{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;margin-top:16px;}
.qnav .prev{justify-self:start;}
.qnav .next{justify-self:end;}
.hint{text-align:center;color:var(--ink-3);font-size:.8rem;margin-top:16px;}

dialog.dlg{
  border:1px solid var(--rule);border-radius:var(--r-lg);padding:26px;max-width:470px;width:calc(100% - 32px);
  color:var(--ink);background:var(--surface);font-family:inherit;box-shadow:var(--shadow);
}
dialog.dlg::backdrop{background:rgba(12,16,28,.5);}
.dlg-sec{margin:16px 0 4px;}
.dlg-sec b{display:block;font-size:.86rem;margin-bottom:8px;font-weight:600;}
.jump{display:flex;flex-wrap:wrap;gap:6px;}
.jump button{
  min-width:34px;height:34px;padding:0 9px;border-radius:var(--r-sm);border:1px solid var(--rule);background:var(--surface);
  font:inherit;font-weight:600;font-size:.84rem;cursor:pointer;color:var(--ink);
}
.jump button:hover{border-color:var(--blue);color:var(--blue);}
.dlg .btn-row{margin-top:22px;flex-wrap:nowrap;}
.dlg .btn-row .btn{flex:1;}

/* ---------- report card ---------- */
.hero-report{text-align:center;}
.ring{
  --pct:0;width:152px;height:152px;border-radius:50%;margin:22px auto 18px;
  background:conic-gradient(var(--blue) calc(var(--pct) * 1%), var(--rule) 0);
  display:grid;place-items:center;
}
.ring span{
  width:124px;height:124px;border-radius:50%;background:var(--surface);
  display:grid;place-items:center;font-family:var(--serif);font-size:2.1rem;font-weight:600;color:var(--ink);
}
.grade{
  display:inline-flex;align-items:center;gap:7px;padding:5px 16px;border-radius:999px;
  background:var(--ink);color:var(--paper);font-weight:600;font-size:.88rem;margin:10px 0 12px;
}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:10px;margin-top:26px;}
.chip{border:1px solid var(--rule);border-radius:var(--r-md);padding:13px 8px;background:var(--surface);}
.chip b{display:block;font-family:var(--serif);font-size:1.3rem;font-weight:600;line-height:1.3;}
.chip small{color:var(--ink-2);font-size:.79rem;}
.chip.ok b{color:var(--green);}
.chip.bad b{color:var(--red);}
.cmp{margin-top:26px;padding-top:22px;border-top:1px solid var(--rule);text-align:left;}
.cmp-top{display:flex;justify-content:space-between;align-items:baseline;}
.cmp-top b{font-weight:600;}
.cmp-track{position:relative;height:5px;background:var(--rule);border-radius:999px;margin:24px 9px 16px;}
.cmp-track i{position:absolute;top:50%;width:16px;height:16px;border-radius:50%;transform:translate(-50%,-50%);border:3px solid var(--surface);}
.cmp-track .you,.k.you{background:var(--blue);}
.cmp-track .avg,.k.avg{background:var(--amber);}
.cmp-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:.86rem;}
.cmp-legend .k{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;}
.rv-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
.seg{display:inline-flex;border:1px solid var(--rule);border-radius:999px;padding:3px;background:var(--surface);}
.seg button{border:none;background:none;padding:6px 13px;border-radius:999px;font:inherit;font-weight:500;font-size:.84rem;color:var(--ink-2);cursor:pointer;}
.seg button.on{background:var(--ink);color:var(--paper);}
.rv{display:flex;gap:13px;padding:15px 0;border-bottom:1px solid var(--rule-soft);}
.rv-list .rv:last-child{border-bottom:none;}
.rv-mark{
  flex:none;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;
  font-weight:600;font-size:.82rem;margin-top:2px;
}
.rv-mark.ok{background:var(--green-wash);color:var(--green);}
.rv-mark.bad{background:var(--red-wash);color:var(--red);}
.rv-mark.skip{background:var(--surface-2);color:var(--ink-3);}
.rv-q{font-weight:600;}
.rv-a{font-size:.9rem;color:var(--ink-2);}
.rv-a b{color:var(--ink);font-weight:600;}
.rv-a .good{color:var(--green);}
.rv-x{margin-top:9px;padding:10px 13px;background:var(--surface-2);border-radius:var(--r-sm);font-size:.9rem;color:var(--ink-2);}
.print-only{display:none;}
.cf{position:fixed;top:-16px;width:8px;height:13px;border-radius:2px;pointer-events:none;z-index:60;animation:fall linear forwards;}
@keyframes fall{to{transform:translate(var(--dx),108vh) rotate(720deg);}}

/* ---------- admin ---------- */
.dash-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:24px;}
.dash-top h1{margin-bottom:8px;}
.admin{display:grid;grid-template-columns:200px minmax(0,1fr);gap:34px;align-items:start;}
.side{position:sticky;top:78px;}
.navlist{display:flex;flex-direction:column;gap:2px;}
.nav-item{
  display:flex;align-items:center;gap:11px;width:100%;padding:10px 13px;border:none;background:none;border-radius:var(--r-md);
  font:inherit;font-weight:500;color:var(--ink-2);cursor:pointer;text-align:left;
}
.nav-item:hover{background:var(--surface-2);color:var(--ink);}
.nav-item.active{background:var(--ink);color:var(--paper);}
.panel{display:none;}
.panel.active{display:block;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:20px;}
.stat{display:flex;gap:13px;align-items:center;border:1px solid var(--rule);border-radius:var(--r-lg);padding:15px 17px;background:var(--surface);}
.stat .ico{flex:none;width:38px;height:38px;border-radius:var(--r-md);background:var(--blue-wash);color:var(--blue-deep);display:grid;place-items:center;}
.stat b{display:block;font-family:var(--serif);font-size:1.5rem;font-weight:600;line-height:1.2;}
.stat span{font-size:.81rem;color:var(--ink-2);}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
.two > .card{margin-bottom:20px;}
.share{display:flex;gap:18px;justify-content:space-between;align-items:center;flex-wrap:wrap;}
.share-row{display:flex;gap:10px;flex-wrap:wrap;flex:1;min-width:280px;justify-content:flex-end;}
.share-row input{flex:1;min-width:200px;font-size:.9rem;}
.hist{display:flex;align-items:flex-end;gap:6px;height:152px;margin-top:16px;}
.hcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:.73rem;color:var(--ink-3);gap:4px;}
.hbar{width:100%;background:var(--blue);border-radius:6px 6px 2px 2px;min-height:2px;}
.hbar.zero{background:var(--rule);}
.gchips{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;}
.gchips span{padding:5px 12px;border-radius:999px;border:1px solid var(--rule);font-size:.84rem;color:var(--ink-2);}
.gchips b{color:var(--ink);font-weight:600;}
.irow{display:grid;grid-template-columns:1fr 140px 42px;gap:14px;align-items:center;padding:11px 0;border-bottom:1px solid var(--rule-soft);font-size:.92rem;}
.irow:last-child{border-bottom:none;}
.irow .qt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.irow .sub{grid-column:1 / -1;margin-top:-6px;font-size:.81rem;color:var(--ink-3);}
.irow .pc{font-weight:600;text-align:right;font-variant-numeric:tabular-nums;}
.irow.two-col{grid-template-columns:1fr 42px;}
.meter{height:7px;background:var(--surface-2);border-radius:999px;overflow:hidden;}
.meter i{display:block;height:100%;background:var(--blue);border-radius:999px;}
.meter.good i{background:var(--green);}
.meter.mid i{background:var(--amber);}
.meter.low i{background:var(--red);}
.qrow{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--rule-soft);}
.qrow:last-child{border-bottom:none;}
.qrow b{font-weight:600;}
.qrow .opts{font-size:.89rem;color:var(--ink-2);margin-top:5px;}
.qrow .opts .right{color:var(--green);font-weight:600;}
.qrow .acts{display:flex;gap:8px;flex:none;}
.tools{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;margin-bottom:16px;}
.tools .filters{display:flex;gap:10px;flex-wrap:wrap;}
.tools input{width:220px;}
.tools select{width:150px;}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;font-size:.91rem;min-width:780px;}
th{text-align:left;color:var(--ink-2);font-weight:500;font-size:.81rem;padding:8px 10px;border-bottom:1px solid var(--rule);white-space:nowrap;user-select:none;}
th[data-dir=asc]::after{content:" \\2191";}
th[data-dir=desc]::after{content:" \\2193";}
td{padding:11px 10px;border-bottom:1px solid var(--rule-soft);white-space:nowrap;}
td a{font-weight:600;}
td.warn{color:var(--amber);font-weight:600;}
code.fmt{
  display:block;background:var(--surface-2);border:1px solid var(--rule);border-radius:var(--r-sm);
  padding:10px 12px;font-family:var(--mono);font-size:.79rem;margin:0 0 14px;overflow-x:auto;white-space:nowrap;color:var(--ink-2);
}
.danger-card{border-color:var(--red);}

@media (max-width:900px){
  .admin{grid-template-columns:1fr;gap:16px;}
  .side{position:static;}
  .navlist{flex-direction:row;overflow-x:auto;border-bottom:1px solid var(--rule);padding-bottom:8px;gap:6px;}
  .nav-item{width:auto;white-space:nowrap;}
  .two{grid-template-columns:1fr;}
}
@media (max-width:560px){
  .grid2{grid-template-columns:1fr;}
  .card{padding:22px 18px;}
  h1{font-size:1.65rem;}
  .hero h1{font-size:2.05rem;}
  .pad-top{padding:16px 18px 14px 40px;}
  .pad-body{padding:20px 18px 22px 40px;}
  .btn-row .btn{width:100%;}
  .dlg .btn-row .btn{width:auto;}
  .qrow{flex-direction:column;}
  .tb-name{max-width:38vw;}
  .brand-sub{display:none;}
  .q{padding:20px 18px;}
  .qnav{grid-template-columns:1fr 1fr;}
  .qnav .mark{grid-column:1 / -1;order:-1;}
  .irow{grid-template-columns:1fr 42px;}
  .irow .meter{grid-column:1 / -1;order:3;}
  .tools input,.tools select{width:100%;}
  .tools .filters{width:100%;}
  .share-row .btn{flex:1;}
}
@media print{
  body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .no-print{display:none !important;}
  .print-only{display:block;}
  .card{border-color:#ccc;box-shadow:none;break-inside:avoid;}
  .cf,.toasts{display:none;}
}
@media (prefers-reduced-motion:reduce){*{transition:none !important;animation:none !important;}}
</style>
<script>
window.toast = function (msg, kind) {
  var box = document.getElementById('toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    box.className = 'toasts';
    box.setAttribute('role', 'status');
    box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  var t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 300); }, 4200);
};
document.addEventListener('DOMContentLoaded', function () {
  var tt = document.getElementById('theme-btn');
  if (tt) tt.addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('stp_theme', next); } catch (e) {}
  });
  [].forEach.call(document.querySelectorAll('form'), function (f) {
    f.addEventListener('submit', function (e) {
      if (e.defaultPrevented || f.hasAttribute('data-nobusy')) return;
      var b = f.querySelector('button[type=submit]');
      if (!b) return;
      setTimeout(function () { b.classList.add('busy'); b.disabled = true; }, 0);
    });
  });
  [].forEach.call(document.querySelectorAll('.flash.success'), function (el) {
    setTimeout(function () { el.style.opacity = '0'; setTimeout(function () { el.hidden = true; }, 400); }, 4500);
  });
});
window.addEventListener('pageshow', function (e) {
  if (e.persisted) [].forEach.call(document.querySelectorAll('.btn.busy'), function (b) { b.classList.remove('busy'); b.disabled = false; });
});
</script>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
  <symbol id="i-users" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.6-3.6 3.1-5.5 6.5-5.5s5.9 1.9 6.5 5.5"/><path d="M16.5 4.6a3.5 3.5 0 010 6.8M18.5 14.8c1.7.7 2.8 2.3 3.1 5.2"/></symbol>
  <symbol id="i-trophy" viewBox="0 0 24 24"><path d="M8 4h8v5a4 4 0 01-8 0V4z"/><path d="M8 6H4v1a4 4 0 004 4M16 6h4v1a4 4 0 01-4 4M12 13v4M8 20h8"/></symbol>
  <symbol id="i-target" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/></symbol>
  <symbol id="i-list" viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/></symbol>
  <symbol id="i-chart" viewBox="0 0 24 24"><path d="M5 20V11M12 20V4M19 20v-6M3 20h18"/></symbol>
  <symbol id="i-sliders" viewBox="0 0 24 24"><path d="M4 7h9M19 7h1M4 17h1M11 17h9"/><circle cx="16" cy="7" r="2.2"/><circle cx="8" cy="17" r="2.2"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h6"/></symbol>
  <symbol id="i-help" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.5a2.5 2.5 0 015 .6c0 1.6-2.6 2-2.6 3.6M12 17h.01"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M12 4v11M7 11l5 5 5-5M5 20h14"/></symbol>
  <symbol id="i-link" viewBox="0 0 24 24"><path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/></symbol>
  <symbol id="i-out" viewBox="0 0 24 24"><path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/></symbol>
  <symbol id="i-flag" viewBox="0 0 24 24"><path d="M5 21V4M5 4h11l-2 4 2 4H5"/></symbol>
  <symbol id="i-eye" viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></symbol>
  <symbol id="i-edit" viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4zM13 7l4 4"/></symbol>
  <symbol id="i-arrow-left" viewBox="0 0 24 24"><path d="M19 12H5M11 6l-6 6 6 6"/></symbol>
  <symbol id="i-print" viewBox="0 0 24 24"><path d="M7 9V3h10v6M7 17H4v-7h16v7h-3M7 14h10v7H7z"/></symbol>
  <symbol id="i-external" viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z"/></symbol>
  <symbol id="i-sun" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></symbol>
  <symbol id="i-lock" viewBox="0 0 24 24"><rect x="4.5" y="10" width="15" height="10" rx="2.5"/><path d="M8 10V7.5a4 4 0 018 0V10"/></symbol>
  <symbol id="i-inbox" viewBox="0 0 24 24"><path d="M4 13h4l2 3h4l2-3h4"/><path d="M5.5 5h13l2.5 8v6H3v-6z"/></symbol>
</svg>
<header class="topbar no-print">
  <div class="topbar-in{{ ' wide' if wide }}">
    <a class="brand" href="{{ url_for('home') }}">
      <span class="mark">{{ icon('check') }}</span>
      <span class="brand-text">
        <span class="brand-name">{{ cfg.school_name }}</span>
        <span class="brand-sub">{{ cfg.test_title }}</span>
      </span>
    </a>
    <div class="topbar-right">
      {% if header_action %}{{ header_action }}{% endif %}
      <button class="icon-btn" type="button" id="theme-btn" aria-label="Switch between light and dark">
        <svg class="ic moon" aria-hidden="true"><use href="#i-moon"/></svg>
        <svg class="ic sun" aria-hidden="true"><use href="#i-sun"/></svg>
      </button>
    </div>
  </div>
</header>
<main class="wrap{{ ' wide' if wide }}">
{% for cat, msg in get_flashed_messages(with_categories=true) %}
  <div class="flash {{ cat }}" role="status">{{ msg }}</div>
{% endfor %}
"""

LAYOUT_BOTTOM = """
</main>
<footer class="foot no-print">
  <div class="foot-in">
    <span>{{ cfg.school_name }}</span>
    <a href="{{ url_for('admin_login') }}">Teacher login</a>
  </div>
</footer>
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
                "explanation": q["explanation"] or "",
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


def comparison(conn, pct):
    """Class average and percentile, shown only once at least 5 students have submitted."""
    rows = conn.execute("SELECT score, total FROM results WHERE total > 0").fetchall()
    vals = [round(r["score"] / r["total"] * 100) for r in rows]
    if len(vals) < 5:
        return None
    below = sum(1 for v in vals if v < pct)
    return {
        "avg": round(sum(vals) / len(vals)),
        "higher_than": round(below / len(vals) * 100),
        "n": len(vals),
    }


# ----------------------------------------------------------------------
# STUDENT FLOW  1) details
# ----------------------------------------------------------------------
HOME_TEMPLATE = """
<div class="hero">
  <section>
    <span class="pill {{ 'ok' if cfg.is_open and n else 'bad' }}"><span class="dot"></span>{{ 'Open now' if cfg.is_open and n else 'Not accepting answers' }}</span>
    <h1>{{ cfg.test_title }}</h1>
    <p class="lead">
      {% if n %}Fill in your details, read the rules, and the timer starts only when you press Begin. Your report card opens the moment you submit.{% else %}Your teacher is still adding the questions. Check back a little later.{% endif %}
    </p>

    {% if n %}
    <dl class="spec">
      <div><dt>Questions</dt><dd>{{ n }} multiple choice</dd></div>
      <div><dt>Time limit</dt><dd>{{ cfg.minutes }} minutes</dd></div>
      <div><dt>Marking</dt><dd>1 mark each, no negative marking</dd></div>
      <div><dt>Result</dt><dd>Shown immediately, with answers</dd></div>
    </dl>
    {% endif %}

    <ol class="steps">
      <li><span class="n"></span><span><b>Enter your details</b><small>Name, class, semester and phone number</small></span></li>
      <li><span class="n"></span><span><b>Read the rules</b><small>Nothing starts until you press Begin</small></span></li>
      <li><span class="n"></span><span><b>Answer the questions</b><small>One at a time, with a map to jump around</small></span></li>
      <li><span class="n"></span><span><b>Get your report card</b><small>Score, grade and a full answer review</small></span></li>
    </ol>
  </section>

  <section>
    <div class="pad">
      <div class="holes" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
      {% if not cfg.is_open %}
        <div class="pad-top"><div><h2>Test closed</h2><small>No new attempts right now</small></div></div>
        <div class="pad-body empty">
          {{ icon('lock') }}
          <p class="muted" style="margin:0;">Your teacher has paused this test. Ask them when it opens again.</p>
        </div>
      {% elif not n %}
        <div class="pad-top"><div><h2>Questions on the way</h2><small>Nothing to answer yet</small></div></div>
        <div class="pad-body empty">
          {{ icon('inbox') }}
          <p class="muted" style="margin:0;">This test has no questions yet. Ask your teacher to add them.</p>
        </div>
      {% else %}
        <div class="pad-top">
          <div><h2>Your details</h2><small>Used on your report card</small></div>
          <div class="bubbles" aria-hidden="true"><i></i><i class="on"></i><i></i><i></i></div>
        </div>
        <div class="pad-body">
          <form method="POST" action="{{ url_for('start_test') }}">
            <label class="field"><span>Full name</span>
              <input type="text" name="name" required maxlength="60" autocomplete="name" placeholder="As written in the register">
            </label>
            <div class="grid2">
              <label class="field"><span>Class</span>
                <input type="text" name="student_class" required maxlength="20" placeholder="6th or 10-A">
              </label>
              <label class="field"><span>Semester or term</span>
                <input type="text" name="semester" required maxlength="30" placeholder="Semester 1">
              </label>
            </div>
            <label class="field"><span>Phone number <small>10 digits</small></span>
              <input type="tel" name="phone" required inputmode="numeric" pattern="[0-9]{10}" maxlength="10" autocomplete="tel" placeholder="9876543210">
            </label>
            <button class="btn btn-full" type="submit">Continue</button>
            <p class="muted" style="text-align:center;margin:12px 0 0;font-size:.83rem;">Only your teacher can see these details.</p>
          </form>
        </div>
      {% endif %}
    </div>
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
        <div class="muted">Class {{ student.student_class }} &middot; {{ student.semester }}</div>
      </div>
    </div>
    <h1>Before you begin</h1>
    <p class="lead" style="margin-bottom:0;">Read these once. You can come back only by starting over.</p>
    <ul class="rules">
      <li>{{ icon('list') }}<span><b>{{ n }} question{{ '' if n == 1 else 's' }}</b>, each with exactly one correct answer. No marks are cut for a wrong answer.</span></li>
      <li>{{ icon('clock') }}<span>You get <b>{{ cfg.minutes }} minutes</b>. The clock starts when you press Begin test, not now.</span></li>
      <li>{{ icon('flag') }}<span>Move freely between questions, change answers, and mark any question to come back to.</span></li>
      <li>{{ icon('check') }}<span>Answers are saved on this device as you go, so a refresh will not lose them.</span></li>
      <li>{{ icon('target') }}<span>The test submits by itself when time runs out.</span></li>
      {% if cfg.track_focus %}<li>{{ icon('eye') }}<span>Stay on this page. Each time you switch to another tab or app it is counted, and your teacher sees the count.</span></li>{% endif %}
    </ul>
    {% if cfg.note %}<div class="note">{{ cfg.note }}</div>{% endif %}
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
      <div class="tb-meta">Class {{ student.student_class }} &middot; {{ student.semester }}</div>
    </div>
    <div id="timer" class="timer" role="timer">{{ icon('clock') }}<span id="time-left">--:--</span></div>
  </div>
  <div class="track"><div class="bar" id="bar"></div></div>
  <div class="tb-count"><span><span id="answered">0</span> of {{ questions|length }} answered</span><span class="saved">{{ icon('check') }}Saved on this device</span></div>
</div>

<noscript><div class="flash error">Turn on JavaScript in your browser to take this test.</div></noscript>

<div class="palette">
  <div class="pal-top">
    <b>Question map</b>
    <button class="btn btn-ghost btn-sm" type="button" id="finish-btn">Review and submit</button>
  </div>
  <div class="pal-grid">
    {% for q in questions %}<button type="button" class="pb" data-i="{{ loop.index0 }}" aria-label="Go to question {{ loop.index }}">{{ loop.index }}</button>{% endfor %}
  </div>
  <div class="legend">
    <span><i class="l-ans"></i>Answered</span>
    <span><i class="l-cur"></i>Current</span>
    <span><i class="l-flag"></i>Marked to review</span>
  </div>
</div>

<form method="POST" action="{{ url_for('submit_test') }}" id="quiz-form" data-nobusy>
  <input type="hidden" name="focus_lost" id="focus-lost" value="0">
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
    <button class="btn btn-ghost mark" type="button" id="mark-btn">{{ icon('flag') }}<span>Mark to review</span></button>
    <button class="btn next" type="button" id="next-btn">Next</button>
  </div>
  <p class="hint">Press 1 to 4 to pick an option, arrow keys to move between questions.</p>
</form>

<dialog class="dlg" id="summary">
  <h2>Ready to submit?</h2>
  <p class="muted" id="sum-text" style="margin:6px 0 0;"></p>
  <div class="dlg-sec" id="sum-un" hidden><b>Not answered yet, tap to go there</b><div class="jump" id="sum-un-list"></div></div>
  <div class="dlg-sec" id="sum-fl" hidden><b>Marked to review</b><div class="jump" id="sum-fl-list"></div></div>
  <div class="btn-row">
    <button class="btn btn-ghost" type="button" id="keep-btn">Keep working</button>
    <button class="btn" type="button" id="confirm-btn">Submit test</button>
  </div>
</dialog>
</div>

<script>
(function () {
  var TOTAL = {{ questions|length }};
  var INITIAL = {{ remaining }};
  var TRACK = {{ 'true' if cfg.track_focus else 'false' }};
  var endAt = Date.now() + INITIAL * 1000;
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
  var markLabel = markBtn.querySelector('span');
  var dlg = document.getElementById('summary');
  var state = { cur: 0, flags: {}, lost: 0 };
  var sent = false;
  var allow = false;
  var warned5 = false, warned1 = false;

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
      localStorage.setItem(storeKey, JSON.stringify({ cur: state.cur, flags: state.flags, lost: state.lost, ans: ans }));
    } catch (e) {}
  }
  function load() {
    try {
      var s = JSON.parse(localStorage.getItem(storeKey) || 'null');
      if (!s) return;
      state.cur = Math.min(Math.max(s.cur || 0, 0), TOTAL - 1);
      state.flags = s.flags || {};
      state.lost = s.lost || 0;
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
    markLabel.textContent = on ? 'Marked to review' : 'Mark to review';
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
      'You answered ' + (TOTAL - un.length) + ' of ' + TOTAL + ' questions. Answers cannot be changed after you submit.';
    makeJump('sum-un-list', 'sum-un', un);
    makeJump('sum-fl-list', 'sum-fl', fl);
    if (dlg.showModal) dlg.showModal();
    else if (confirm('Submit your test now?')) send();
  }

  function send() {
    if (sent) return;
    sent = true;
    allow = true;
    document.getElementById('focus-lost').value = state.lost;
    try { localStorage.removeItem(storeKey); } catch (e) {}
    var cb = document.getElementById('confirm-btn');
    cb.disabled = true;
    cb.textContent = 'Submitting...';
    form.submit();
  }

  function tick() {
    var left = Math.max(0, Math.round((endAt - Date.now()) / 1000));
    var m = String(Math.floor(left / 60)).padStart(2, '0');
    var s = String(left % 60).padStart(2, '0');
    timeEl.textContent = m + ':' + s;
    timerEl.classList.toggle('warn', left <= 300 && left > 60);
    timerEl.classList.toggle('low', left <= 60);
    if (!warned5 && left <= 300 && left > 60 && INITIAL > 300) { warned5 = true; window.toast('5 minutes left', 'warn'); }
    if (!warned1 && left <= 60 && left > 0 && INITIAL > 60) { warned1 = true; window.toast('1 minute left', 'warn'); }
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

  var wasHidden = false;
  document.addEventListener('visibilitychange', function () {
    if (!TRACK || sent) return;
    if (document.hidden) { wasHidden = true; state.lost++; save(); }
    else if (wasHidden) { wasHidden = false; window.toast('You left the test page. That is recorded for your teacher.', 'warn'); }
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

    try:
        focus_lost = max(0, min(999, int(request.form.get("focus_lost", "0"))))
    except ValueError:
        focus_lost = 0
    if not cfg()["track_focus"]:
        focus_lost = 0

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
            "submitted_at, time_taken, answers, focus_lost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                focus_lost,
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
  <div class="print-only" style="text-align:center;margin-bottom:16px;">
    <h2>{{ cfg.school_name }}</h2>
    <div class="muted">{{ cfg.test_title }} &middot; Report card &middot; {{ submitted_at }}</div>
  </div>

  <section class="card hero-report">
    <span class="pill no-print">Report card</span>
    <h1 style="margin-top:14px;">{{ headline }}</h1>
    <p class="lead" style="margin:0 auto;"><b style="color:var(--ink);">{{ student.name }}</b><br>Class {{ student.student_class }} &middot; {{ student.semester }}</p>
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
    {% if cmp %}
    <div class="cmp">
      <div class="cmp-top"><b>How you compare</b><span class="muted">{{ cmp.n }} students so far</span></div>
      <div class="cmp-track" aria-hidden="true"><i class="avg" style="left:{{ cmp.avg }}%;"></i><i class="you" style="left:{{ pct }}%;"></i></div>
      <div class="cmp-legend"><span><i class="k you"></i>You {{ pct }}%</span><span><i class="k avg"></i>Class average {{ cmp.avg }}%</span></div>
      {% if cmp.higher_than > 0 %}<p class="muted" style="margin:10px 0 0;">You scored higher than {{ cmp.higher_than }}% of them.</p>{% endif %}
    </div>
    {% endif %}
  </section>

  <section class="card">
    <div class="rv-head">
      <h2>Answer review</h2>
      <div class="seg no-print" role="group" aria-label="Filter answers">
        <button type="button" class="on" data-filter="all">All</button>
        <button type="button" data-filter="review">To review</button>
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
          {% if r.status != 'ok' and r.explanation %}<div class="rv-x">{{ r.explanation }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    <div class="btn-row no-print" style="margin-top:22px;">
      <button class="btn" type="button" onclick="window.print()">{{ icon('print') }}Print or save as PDF</button>
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

  function confetti() {
    var css = getComputedStyle(document.documentElement);
    var colors = ['blue', 'amber', 'green', 'red'].map(function (n) { return css.getPropertyValue('--' + n).trim(); });
    for (var i = 0; i < 60; i++) {
      var d = document.createElement('div');
      d.className = 'cf';
      d.style.left = Math.random() * 100 + 'vw';
      d.style.background = colors[i % colors.length];
      d.style.setProperty('--dx', (Math.random() * 160 - 80) + 'px');
      d.style.animationDuration = (2 + Math.random() * 1.6) + 's';
      d.style.animationDelay = (Math.random() * 0.5) + 's';
      document.body.appendChild(d);
      (function (el) { setTimeout(function () { el.remove(); }, 4500); })(d);
    }
  }

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
      if (p < 1) requestAnimationFrame(step); else if (target >= 75) confetti();
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
        cmp_data = comparison(conn, pct) if cfg()["show_stats"] else None

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
        cmp=cmp_data,
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
<div class="narrow" style="max-width:420px;">
  <section class="card">
    <h1>Teacher login</h1>
    <p class="lead">Manage questions, settings and results.</p>
    <form method="POST">
      <label class="field"><span>Password</span>
        <div class="pw">
          <input type="password" id="pw" name="password" required autocomplete="current-password" placeholder="Admin password">
          <button type="button" id="pw-toggle" aria-label="Show password">{{ icon('eye') }}</button>
        </div>
      </label>
      <button class="btn btn-full" type="submit">Log in</button>
    </form>
  </section>
  <a class="small-link muted" href="{{ url_for('home') }}">Back to the test</a>
</div>
<script>
document.getElementById('pw-toggle').addEventListener('click', function () {
  var i = document.getElementById('pw');
  var show = i.type === 'password';
  i.type = show ? 'text' : 'password';
  this.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
});
</script>
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
        flash("That password did not match. Try again.", "error")
    return render_template_string(page(LOGIN_TEMPLATE))


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


DASHBOARD_TEMPLATE = """
<div class="dash-top">
  <div>
    <h1>Dashboard</h1>
    <span class="pill {{ 'ok' if cfg.is_open else 'bad' }}"><span class="dot"></span>{{ 'Open to students' if cfg.is_open else 'Closed to students' }}</span>
  </div>
  <div class="btn-row">
    <a class="btn" href="{{ url_for('download_csv') }}">{{ icon('download') }}Download results</a>
    <a class="btn btn-ghost" href="{{ url_for('home') }}" target="_blank" rel="noopener">{{ icon('external') }}Student page</a>
    <a class="btn btn-ghost" href="{{ url_for('admin_logout') }}">{{ icon('out') }}Log out</a>
  </div>
</div>

<div class="admin">
  <nav class="side" aria-label="Dashboard sections">
    <div class="navlist" role="tablist">
      <button class="nav-item" type="button" data-tab="overview">{{ icon('chart') }}Overview</button>
      <button class="nav-item" type="button" data-tab="questions">{{ icon('help') }}Questions</button>
      <button class="nav-item" type="button" data-tab="results">{{ icon('file') }}Results</button>
      <button class="nav-item" type="button" data-tab="settings">{{ icon('sliders') }}Settings</button>
    </div>
  </nav>

  <div class="content">

<!-- OVERVIEW -->
<div class="panel" data-panel="overview">
  <div class="stats">
    <div class="stat"><div class="ico">{{ icon('users') }}</div><div><b>{{ stats.count }}</b><span>Submissions</span></div></div>
    <div class="stat"><div class="ico">{{ icon('target') }}</div><div><b>{{ stats.avg }}%</b><span>Average score</span></div></div>
    <div class="stat"><div class="ico">{{ icon('trophy') }}</div><div><b>{{ stats.best }}%</b><span>Highest score</span></div></div>
    <div class="stat"><div class="ico">{{ icon('clock') }}</div><div><b>{{ stats.avg_time }}</b><span>Average time</span></div></div>
  </div>

  <section class="card share">
    <div><h2>Share the test link</h2><span class="muted">Send it on WhatsApp or write it on the board.</span></div>
    <div class="share-row">
      <input type="text" id="share-url" readonly value="{{ share_url }}" aria-label="Test link">
      <button class="btn btn-ghost" type="button" id="copy-btn">{{ icon('link') }}Copy link</button>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/?text={{ (cfg.test_title ~ ': ' ~ share_url)|urlencode }}">WhatsApp</a>
    </div>
  </section>

  <div class="two">
    <section class="card">
      <h2>Score distribution</h2>
      <p class="muted" style="margin:0;">Students in each 10-point band.</p>
      <div class="hist" role="img" aria-label="Score distribution chart">
        {% for b in hist %}
        <div class="hcol"><span>{{ b.count if b.count else '' }}</span><div class="hbar {{ 'zero' if not b.count }}" style="height:{{ b.height }}%;"></div><span>{{ b.label }}</span></div>
        {% endfor %}
      </div>
      <div class="gchips">
        {% for g in grades %}<span>Grade {{ g.label }} <b>{{ g.count }}</b></span>{% endfor %}
      </div>
    </section>
    <section class="card">
      <h2>Hardest questions</h2>
      <p class="muted" style="margin:0 0 10px;">Fewest students answered these correctly.</p>
      {% for it in hardest %}
      <div class="irow two-col">
        <div class="qt" title="{{ it.text }}">Q{{ it.number }}. {{ it.text }}</div><div class="pc">{{ it.pct }}%</div>
        {% if it.wrong_note %}<div class="sub">{{ it.wrong_note }}</div>{% endif %}
      </div>
      {% else %}
      <p class="muted" style="margin:14px 0 0;">This fills in after the first submission.</p>
      {% endfor %}
    </section>
  </div>

  <section class="card">
    <h2>Question-wise analysis</h2>
    <p class="muted" style="margin:0 0 10px;">Share of students who got each question right.</p>
    {% for it in items %}
    <div class="irow">
      <div class="qt" title="{{ it.text }}">Q{{ it.number }}. {{ it.text }}</div>
      {% if it.pct is not none %}
      <div class="meter {{ it.level }}"><i style="width:{{ it.pct }}%;"></i></div><div class="pc">{{ it.pct }}%</div>
      {% if it.wrong_note %}<div class="sub">{{ it.wrong_note }}</div>{% endif %}
      {% else %}
      <div class="muted">No answers yet</div><div></div>
      {% endif %}
    </div>
    {% else %}
    <p class="muted" style="margin:8px 0 0;">Add questions to see the analysis.</p>
    {% endfor %}
  </section>
</div>

<!-- QUESTIONS -->
<div class="panel" data-panel="questions">
  <div class="two">
    <section class="card">
      <h2>Add one question</h2>
      <p class="lead" style="margin-bottom:20px;">Four options, one correct answer.</p>
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
        <label class="field"><span>Explanation <small>optional, shown to students who miss it</small></span>
          <textarea name="explanation" rows="2" maxlength="400" placeholder="Why is this the right answer?"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add question</button>
      </form>
    </section>

    <section class="card">
      <h2>Add many at once</h2>
      <p class="lead" style="margin-bottom:14px;">One question per line, parts separated by the | symbol. The explanation at the end is optional.</p>
      <code class="fmt">Question | A | B | C | D | b | Explanation</code>
      <form method="POST" action="{{ url_for('bulk_add') }}">
        <label class="field"><span>Questions</span>
          <textarea name="bulk" rows="10" required placeholder="What is 2 + 2? | 3 | 4 | 5 | 6 | b | Adding two and two gives four.&#10;Capital of India? | Mumbai | Delhi | Chennai | Kolkata | b"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add all questions</button>
      </form>
    </section>
  </div>

  <section class="card">
    <div class="tools">
      <div><h2>All questions ({{ questions|length }})</h2><span class="muted">Edits apply to students who start after you save.</span></div>
      {% if questions %}<a class="btn btn-ghost btn-sm" href="{{ url_for('export_questions') }}">{{ icon('download') }}Download backup</a>{% endif %}
    </div>
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
      <div class="acts">
        <a class="btn btn-ghost btn-sm" href="{{ url_for('edit_question', qid=q.id) }}">{{ icon('edit') }}Edit</a>
        <form method="POST" action="{{ url_for('delete_question', qid=q.id) }}" onsubmit="return confirm('Delete this question?');">
          <button class="btn btn-danger btn-sm" type="submit">Delete</button>
        </form>
      </div>
    </div>
    {% else %}
    <p class="muted" style="margin:8px 0 0;">No questions yet. Add your first one above, or paste a batch.</p>
    {% endfor %}
    {% if questions %}<p class="muted" style="margin:16px 0 0;">On Render's free plan the database can reset when the app redeploys. Keep the backup file, then paste it into "Add many at once" to restore everything.</p>{% endif %}
  </section>
</div>

<!-- RESULTS -->
<div class="panel" data-panel="results">
  <section class="card">
    <div class="tools">
      <div>
        <h2>Results</h2>
        <span class="muted">Showing {{ results|length }} of {{ stats.count }}. Click a column to sort, or a name to see answers.</span>
      </div>
      <div class="filters">
        <select id="class-filter" aria-label="Filter by class">
          <option value="">All classes</option>
          {% for c in classes %}<option value="{{ c|lower }}">{{ c }}</option>{% endfor %}
        </select>
        <input type="text" id="search" placeholder="Search name or phone" aria-label="Search results">
      </div>
    </div>
    {% if results %}
    <div class="tscroll">
      <table id="results-table">
        <thead><tr>
          <th data-sort="text">Name</th><th data-sort="text">Class</th><th data-sort="text">Semester</th><th>Phone</th>
          <th data-sort="num">Score</th><th data-sort="num">%</th><th data-sort="text">Grade</th>
          <th data-sort="num">Time</th><th data-sort="num">Tab switches</th><th data-sort="text">Submitted</th><th></th>
        </tr></thead>
        <tbody>
        {% for r in results %}
        <tr>
          <td data-v="{{ r.student_name|lower }}"><a href="{{ url_for('result_detail', rid=r.id) }}">{{ r.student_name }}</a></td>
          <td data-v="{{ r.student_class|lower }}">{{ r.student_class }}</td>
          <td data-v="{{ r.semester|lower }}">{{ r.semester }}</td>
          <td data-v="{{ r.phone }}">{{ r.phone }}</td>
          <td data-v="{{ r.score }}" class="tabular">{{ r.score }}/{{ r.total }}</td>
          <td data-v="{{ r.pct }}" class="tabular">{{ r.pct }}%</td>
          <td data-v="{{ r.grade }}">{{ r.grade }}</td>
          <td data-v="{{ r.time_taken if r.time_taken is not none else -1 }}" class="tabular">{{ r.time }}</td>
          <td data-v="{{ r.focus_lost or 0 }}" class="tabular {{ 'warn' if (r.focus_lost or 0) >= 3 }}">{{ r.focus_lost if r.focus_lost is not none else '-' }}</td>
          <td data-v="{{ r.submitted_at }}">{{ r.submitted_at }}</td>
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
    <p class="muted" style="margin:8px 0 0;">No submissions yet. Share the link from Overview to get started.</p>
    {% endif %}
  </section>
</div>

<!-- SETTINGS -->
<div class="panel" data-panel="settings">
  <div class="two">
    <section class="card">
      <h2>Test settings</h2>
      <p class="lead" style="margin-bottom:20px;">These apply to the next student who starts.</p>
      <form method="POST" action="{{ url_for('save_settings') }}">
        <label class="field"><span>School or coaching name</span>
          <input type="text" name="school_name" maxlength="60" value="{{ cfg.school_name }}" required>
        </label>
        <label class="field"><span>Test title</span>
          <input type="text" name="test_title" maxlength="80" value="{{ cfg.test_title }}" required>
        </label>
        <label class="field"><span>Time limit <small>minutes</small></span>
          <input type="number" name="minutes" min="1" max="240" value="{{ cfg.minutes }}" required>
        </label>
        <label class="field"><span>Message for students <small>optional, shown before the test</small></span>
          <textarea name="note" rows="3" maxlength="300" placeholder="Best of luck. No calculators.">{{ cfg.note }}</textarea>
        </label>
        <label class="check"><input type="checkbox" name="is_open" {{ 'checked' if cfg.is_open }}>
          <span><b>Test is open</b><small>Turn off to stop new students from starting.</small></span></label>
        <label class="check"><input type="checkbox" name="shuffle" {{ 'checked' if cfg.shuffle }}>
          <span><b>Shuffle question order</b><small>Each student sees a different order, so neighbours can't copy.</small></span></label>
        <label class="check"><input type="checkbox" name="allow_retake" {{ 'checked' if cfg.allow_retake }}>
          <span><b>Allow retakes</b><small>If off, the same name, class, semester and phone can submit only once.</small></span></label>
        <label class="check"><input type="checkbox" name="show_stats" {{ 'checked' if cfg.show_stats }}>
          <span><b>Show class comparison</b><small>Report card shows the class average once 5 students have submitted.</small></span></label>
        <label class="check"><input type="checkbox" name="track_focus" {{ 'checked' if cfg.track_focus }}>
          <span><b>Record tab switches</b><small>Counts how often a student leaves the test page. Students are told about this.</small></span></label>
        <button class="btn btn-full" type="submit">Save settings</button>
      </form>
    </section>

    <section class="card danger-card">
      <h2>Delete data</h2>
      <p class="lead" style="margin-bottom:18px;">These cannot be undone. Download the results CSV and the question backup first.</p>
      <form method="POST" action="{{ url_for('reset_results') }}" onsubmit="return confirm('All results will be deleted permanently. Continue?');" style="margin-bottom:12px;">
        <button class="btn btn-danger" type="submit">Delete all results</button>
      </form>
      <form method="POST" action="{{ url_for('delete_all_questions') }}" onsubmit="return confirm('All questions will be deleted permanently. Continue?');">
        <button class="btn btn-danger" type="submit">Delete all questions</button>
      </form>
    </section>
  </div>
</div>

  </div>
</div>

<script>
(function () {
  var tabs = [].slice.call(document.querySelectorAll('.nav-item'));
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

  var copyBtn = document.getElementById('copy-btn');
  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var input = document.getElementById('share-url');
      function done() { window.toast('Link copied'); }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).then(done, function () { input.select(); document.execCommand('copy'); done(); });
      } else { input.select(); document.execCommand('copy'); done(); }
    });
  }

  var search = document.getElementById('search');
  var classSel = document.getElementById('class-filter');
  var table = document.getElementById('results-table');
  function applyFilters() {
    if (!table) return;
    var q = search.value.toLowerCase();
    var c = classSel.value;
    [].forEach.call(table.tBodies[0].rows, function (tr) {
      var okText = tr.textContent.toLowerCase().indexOf(q) !== -1;
      var okClass = !c || tr.cells[1].dataset.v === c;
      tr.hidden = !(okText && okClass);
    });
  }
  if (search) search.addEventListener('input', applyFilters);
  if (classSel) classSel.addEventListener('change', applyFilters);

  if (table) {
    var head = table.tHead.rows[0].cells;
    [].forEach.call(head, function (th, idx) {
      if (!th.dataset.sort) return;
      th.style.cursor = 'pointer';
      th.addEventListener('click', function () {
        var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
        [].forEach.call(head, function (x) { delete x.dataset.dir; });
        th.dataset.dir = dir;
        var rows = [].slice.call(table.tBodies[0].rows);
        rows.sort(function (a, b) {
          var x = a.cells[idx].dataset.v, y = b.cells[idx].dataset.v;
          var r = th.dataset.sort === 'num' ? (parseFloat(x) - parseFloat(y)) : x.localeCompare(y);
          return dir === 'asc' ? r : -r;
        });
        rows.forEach(function (r) { table.tBodies[0].appendChild(r); });
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
    bins = [0] * 10
    pcts, times = [], []
    per_q = {q["id"]: {"attempted": 0, "correct": 0, "opts": {"a": 0, "b": 0, "c": 0, "d": 0}} for q in questions}
    results = []
    classes = set()

    for r in rows:
        pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
        grade = grade_for(pct)[0]
        band_counts[grade] += 1
        bins[min(pct // 10, 9)] += 1
        pcts.append(pct)
        classes.add(r["student_class"].strip())
        if r["time_taken"] is not None:
            times.append(r["time_taken"])
        if r["answers"]:
            try:
                ans = json.loads(r["answers"])
            except ValueError:
                ans = {}
            for q in questions:
                slot = per_q[q["id"]]
                slot["attempted"] += 1
                chosen = ans.get(str(q["id"]))
                if chosen == q["correct_option"]:
                    slot["correct"] += 1
                elif chosen in slot["opts"]:
                    slot["opts"][chosen] += 1
        if len(results) < 500:
            d = dict(r)
            d["pct"] = pct
            d["grade"] = grade
            d["time"] = fmt_clock(r["time_taken"]) or "-"
            results.append(d)

    top = max(bins) if bins else 0
    hist = [
        {"label": str(i * 10), "count": n, "height": round(n / top * 100) if top else 0}
        for i, n in enumerate(bins)
    ]
    grades = [{"label": k, "count": v} for k, v in band_counts.items()]

    items = []
    for i, q in enumerate(questions, start=1):
        slot = per_q[q["id"]]
        attempted = slot["attempted"]
        pct = round(slot["correct"] / attempted * 100) if attempted else None
        level = "" if pct is None else ("good" if pct >= 70 else "mid" if pct >= 40 else "low")
        wrong_note = ""
        if attempted:
            letter, count = max(slot["opts"].items(), key=lambda kv: kv[1])
            if count > 0:
                wrong_note = f"Most chosen wrong answer: {letter.upper()} ({round(count / attempted * 100)}% of students)"
        items.append({"number": i, "text": q["question_text"], "pct": pct, "level": level, "wrong_note": wrong_note})
    hardest = sorted([x for x in items if x["pct"] is not None], key=lambda x: x["pct"])[:3]

    stats = {
        "count": len(rows),
        "avg": round(sum(pcts) / len(pcts)) if pcts else 0,
        "best": max(pcts) if pcts else 0,
        "avg_time": fmt_clock(sum(times) / len(times)) if times else "-",
    }
    return {
        "questions": questions,
        "results": results,
        "stats": stats,
        "hist": hist,
        "grades": grades,
        "items": items,
        "hardest": hardest,
        "classes": sorted(classes, key=str.lower),
    }


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        data = build_dashboard_data(conn)
    return render_template_string(
        page(DASHBOARD_TEMPLATE), wide=True, share_url=request.url_root, **data
    )


RESULT_TEMPLATE = """
<div class="narrow">
  <a class="back no-print" href="{{ url_for('admin_dashboard') }}#results">{{ icon('arrow-left') }}Back to results</a>
  <section class="card">
    <div class="who">
      <div class="avatar">{{ r.student_name[0]|upper }}</div>
      <div>
        <div class="who-name">{{ r.student_name }}</div>
        <div class="muted">Class {{ r.student_class }} &middot; {{ r.semester }} &middot; {{ r.phone }}</div>
      </div>
    </div>
    <div class="chips" style="margin-top:0;">
      <div class="chip"><b>{{ r.score }}/{{ r.total }}</b><small>Score</small></div>
      <div class="chip"><b>{{ pct }}%</b><small>Percentage</small></div>
      <div class="chip"><b>{{ grade }}</b><small>Grade</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
      <div class="chip"><b>{{ r.focus_lost if r.focus_lost is not none else '-' }}</b><small>Tab switches</small></div>
    </div>
    <p class="muted" style="margin:16px 0 0;">Submitted {{ r.submitted_at }}</p>
  </section>

  <section class="card">
    <div class="rv-head"><h2>Answers</h2>
      <button class="btn btn-ghost btn-sm no-print" type="button" onclick="window.print()">{{ icon('print') }}Print</button>
    </div>
    {% if review is none %}
      <p class="muted" style="margin:8px 0 0;">Detailed answers were not saved for this result.</p>
    {% else %}
    <div class="rv-list">
      {% for x in review %}
      <div class="rv">
        <div class="rv-mark {{ x.status }}">{% if x.status == 'ok' %}&#10003;{% elif x.status == 'bad' %}&#10005;{% else %}&ndash;{% endif %}</div>
        <div>
          <div class="rv-q">{{ x.number }}. {{ x.question }}</div>
          <div class="rv-a">Student answered: <b>{{ x.your }}</b></div>
          {% if x.status != 'ok' %}<div class="rv-a">Correct answer: <b class="good">{{ x.correct }}</b></div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    {% endif %}
  </section>
</div>
"""


@app.route("/admin/result/<int:rid>")
def result_detail(rid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM results WHERE id = ?", (rid,)).fetchone()
        if r is None:
            abort(404)
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

    review = None
    if r["answers"]:
        try:
            review = evaluate(questions, json.loads(r["answers"]))[1]
        except ValueError:
            review = None
    pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
    return render_template_string(
        page(RESULT_TEMPLATE),
        r=r,
        pct=pct,
        grade=grade_for(pct)[0],
        time_text=fmt_duration(r["time_taken"]),
        review=review,
    )


EDIT_TEMPLATE = """
<div class="narrow">
  <a class="back" href="{{ url_for('admin_dashboard') }}#questions">{{ icon('arrow-left') }}Back to questions</a>
  <section class="card">
    <h1>Edit question</h1>
    <p class="lead">Changes apply to students who start after you save.</p>
    <form method="POST">
      <label class="field"><span>Question</span>
        <input type="text" name="question_text" required value="{{ q.question_text }}">
      </label>
      <div class="grid2">
        <label class="field"><span>Option A</span><input type="text" name="option_a" required value="{{ q.option_a }}"></label>
        <label class="field"><span>Option B</span><input type="text" name="option_b" required value="{{ q.option_b }}"></label>
        <label class="field"><span>Option C</span><input type="text" name="option_c" required value="{{ q.option_c }}"></label>
        <label class="field"><span>Option D</span><input type="text" name="option_d" required value="{{ q.option_d }}"></label>
      </div>
      <label class="field"><span>Correct option</span>
        <select name="correct_option" required>
          {% for k in ['a','b','c','d'] %}<option value="{{ k }}" {{ 'selected' if q.correct_option == k }}>{{ k|upper }}</option>{% endfor %}
        </select>
      </label>
      <label class="field"><span>Explanation <small>optional</small></span>
        <textarea name="explanation" rows="3" maxlength="400">{{ q.explanation or '' }}</textarea>
      </label>
      <button class="btn btn-full" type="submit">Save changes</button>
    </form>
  </section>
</div>
"""


def read_question_form():
    text = request.form.get("question_text", "").strip()
    options = [request.form.get(f"option_{k}", "").strip() for k in "abcd"]
    correct = request.form.get("correct_option", "")
    explanation = request.form.get("explanation", "").strip()[:400]
    ok = bool(text) and all(options) and correct in OPTION_KEYS
    return ok, text, options, correct, explanation


@app.route("/admin/add_question", methods=["POST"])
def add_question():
    if not admin_required():
        return redirect(url_for("admin_login"))
    ok, text, options, correct, explanation = read_question_form()
    if not ok:
        flash("Fill in the question, all four options and the correct answer.", "error")
        return back_to("questions")
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option, explanation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text, *options, correct, explanation),
        )
        conn.commit()
    flash("Question added.", "success")
    return back_to("questions")


@app.route("/admin/edit_question/<int:qid>", methods=["GET", "POST"])
def edit_question(qid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        q = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        if q is None:
            abort(404)
        if request.method == "POST":
            ok, text, options, correct, explanation = read_question_form()
            if not ok:
                flash("Fill in the question, all four options and the correct answer.", "error")
                return redirect(url_for("edit_question", qid=qid))
            conn.execute(
                "UPDATE questions SET question_text=?, option_a=?, option_b=?, option_c=?, option_d=?, "
                "correct_option=?, explanation=? WHERE id=?",
                (text, *options, correct, explanation, qid),
            )
            conn.commit()
            flash("Question updated.", "success")
            return back_to("questions")
    return render_template_string(page(EDIT_TEMPLATE), q=q)


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
        explanation = ""
        if len(parts) == 7:
            explanation = parts.pop()[:400]
        if len(parts) != 6 or not all(parts) or parts[5].lower() not in OPTION_KEYS:
            bad_lines.append(str(number))
            continue
        rows.append((parts[0], parts[1], parts[2], parts[3], parts[4], parts[5].lower(), explanation))

    if rows:
        with closing(get_db()) as conn:
            conn.executemany(
                "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option, explanation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        flash(f"{len(rows)} question(s) added.", "success")
    if bad_lines:
        flash("Skipped line(s) " + ", ".join(bad_lines) + ". Each line needs a question, 4 options and the correct letter.", "error")
    if not rows and not bad_lines:
        flash("Nothing to add. Paste at least one question.", "error")
    return back_to("questions")


@app.route("/admin/export_questions")
def export_questions():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

    def clean(value):
        return str(value or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()

    lines = []
    for q in questions:
        parts = [
            clean(q["question_text"]),
            clean(q["option_a"]),
            clean(q["option_b"]),
            clean(q["option_c"]),
            clean(q["option_d"]),
            q["correct_option"],
        ]
        if q["explanation"]:
            parts.append(clean(q["explanation"]))
        lines.append(" | ".join(parts))
    return Response(
        "\n".join(lines) + "\n",
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=questions_backup.txt"},
    )


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
        "note": request.form.get("note", "").strip()[:300],
        "is_open": "1" if request.form.get("is_open") else "0",
        "shuffle": "1" if request.form.get("shuffle") else "0",
        "allow_retake": "1" if request.form.get("allow_retake") else "0",
        "show_stats": "1" if request.form.get("show_stats") else "0",
        "track_focus": "1" if request.form.get("track_focus") else "0",
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
        [
            "Name", "Class", "Semester", "Phone", "Score", "Total", "Percentage", "Grade",
            "Time Taken (m:ss)", "Tab Switches", "Submitted At",
        ]
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
                "" if r["focus_lost"] is None else r["focus_lost"],
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
