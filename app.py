"""
Student Test Portal (v5.1) - single-file Flask app: SQLite + HTML + CSS + JavaScript.

STUDENT FLOW
    Details -> Instructions -> Timed arena (one question at a time) -> Report card
    Extras: leaderboard, past-result lookup by phone, printable certificate.

ADMIN FLOW  (/admin)
    Overview (stats, share link, topic + question analysis)
    Question bank (single add / bulk add / edit / filter / backup / restore)
    Results (search, sort, per-student answer sheet, CSV)
    Settings (marking scheme, proctoring, student-facing toggles)

What is new in v5.1
    Flag for review : a small pill toggle on each question card (top right) instead of a
                      button between Previous and Next. No layout jumping, works on phones.
    Mobile          : Previous / Next bar sticks to the bottom of the screen on phones.
    Code questions  : teachers can write ```code blocks``` and `inline code` inside questions,
                      options and explanations. Students see a clean dark code box.
                      Everything is escaped first, so it is safe from HTML injection.

What came with v5
    Marking      : per-question marks, difficulty, topic, optional negative marking,
                   pass mark, weighted percentage.
    Analysis     : topic-wise strengths on the report card, topic + difficulty
                   breakdowns for the teacher, distractor analysis.
    Integrity    : option shuffling, tab-switch counting with an optional auto-submit
                   limit, copy/right-click lock, honour-code checkbox.
    Delight      : an arena-style test screen with a live timer ring, momentum
                   counter, question drawer, sound cues, shortcut sheet, and a
                   report card that animates the score, grade and topic bars.

Optional environment variables (everything else lives in Admin > Settings):
    SECRET_KEY       long random text (keeps sessions and result links secure)
    ADMIN_PASSWORD   admin login password (default: admin123)  <- change this!
    DB_PATH          location of the SQLite file (default: ./test_app.db)

Run locally :  python app.py
Run on Render: gunicorn app:app
"""
import csv
import hashlib
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
from markupsafe import Markup, escape
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
LETTERS = ["A", "B", "C", "D"]
DIFFICULTIES = ("easy", "medium", "hard")


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
                explanation TEXT,
                topic TEXT,
                difficulty TEXT,
                marks REAL
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
                focus_lost INTEGER,
                points REAL,
                max_points REAL
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # upgrade databases created by older versions
        q_cols = {r["name"] for r in conn.execute("PRAGMA table_info(questions)")}
        for col, ddl in (("explanation", "TEXT"), ("topic", "TEXT"), ("difficulty", "TEXT"), ("marks", "REAL")):
            if col not in q_cols:
                conn.execute(f"ALTER TABLE questions ADD COLUMN {col} {ddl}")
        r_cols = {r["name"] for r in conn.execute("PRAGMA table_info(results)")}
        for col, ddl in (
            ("time_taken", "INTEGER"),
            ("answers", "TEXT"),
            ("focus_lost", "INTEGER"),
            ("points", "REAL"),
            ("max_points", "REAL"),
        ):
            if col not in r_cols:
                conn.execute(f"ALTER TABLE results ADD COLUMN {col} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_phone ON results (phone)")
        conn.commit()


init_db()

DEFAULT_SETTINGS = {
    "school_name": "Student Test Portal",
    "test_title": "Class Test",
    "minutes": "15",
    "is_open": "1",
    "shuffle": "1",
    "shuffle_options": "1",
    "allow_retake": "0",
    "show_stats": "1",
    "track_focus": "1",
    "max_switches": "0",
    "negative": "0",
    "pass_mark": "40",
    "show_answers": "1",
    "leaderboard": "1",
    "lookup": "1",
    "certificate": "1",
    "sounds": "1",
    "note": "",
}

BOOL_KEYS = (
    "is_open",
    "shuffle",
    "shuffle_options",
    "allow_retake",
    "show_stats",
    "track_focus",
    "show_answers",
    "leaderboard",
    "lookup",
    "certificate",
    "sounds",
)


def _as_int(raw, default, low, high):
    try:
        return max(low, min(high, int(float(raw))))
    except (TypeError, ValueError):
        return default


def load_settings(conn):
    raw = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        raw[row["key"]] = row["value"]
    out = {
        "school_name": raw["school_name"],
        "test_title": raw["test_title"],
        "note": raw["note"],
        "minutes": _as_int(raw["minutes"], 15, 1, 240),
        "max_switches": _as_int(raw["max_switches"], 0, 0, 50),
        "negative": _as_int(raw["negative"], 0, 0, 100),
        "pass_mark": _as_int(raw["pass_mark"], 40, 0, 100),
    }
    for key in BOOL_KEYS:
        out[key] = raw[key] == "1"
    return out


def cfg():
    """Settings for the current request (loaded once)."""
    if "cfg" not in g:
        with closing(get_db()) as conn:
            g.cfg = load_settings(conn)
    return g.cfg


@app.context_processor
def inject_cfg():
    return {"cfg": cfg(), "fmt_points": fmt_points}


# ----------------------------------------------------------------------
# SHARED LAYOUT + DESIGN SYSTEM
# ----------------------------------------------------------------------
LAYOUT_TOP = """{% macro icon(name) %}<svg class="ic" aria-hidden="true"><use href="#i-{{ name }}"/></svg>{% endmacro %}<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#F4F5FA">
<title>{{ page_title or cfg.school_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=JetBrains+Mono:wght@500;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
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
  --paper:#F4F5FA;
  --surface:#FFFFFF;
  --surface-2:#EDEFF8;
  --ink:#0F1327;
  --ink-2:#4C5270;
  --ink-3:#848BA8;
  --line:#E1E4F0;
  --line-soft:#EDEFF7;
  --g1:#4F46E5;
  --g2:#7C3AED;
  --g3:#06B6D4;
  --accent:#4F46E5;
  --accent-deep:#3730A3;
  --accent-wash:#EDEBFF;
  --on-accent:#FFFFFF;
  --good:#0E9F6E;
  --good-wash:#E3F5EE;
  --bad:#E11D48;
  --bad-wash:#FDE8ED;
  --gold:#C2860A;
  --gold-wash:#FBF0D8;
  --grad:linear-gradient(118deg,var(--g1) 0%,var(--g2) 52%,var(--g3) 100%);
  --ring-track:#E1E4F0;
  --glass:rgba(255,255,255,.72);
  --shadow-1:0 1px 2px rgba(15,19,39,.06);
  --shadow-2:0 2px 6px rgba(15,19,39,.05), 0 18px 40px -22px rgba(15,19,39,.45);
  --shadow-pop:0 24px 60px -24px rgba(79,70,229,.55);
  --r-xl:26px;
  --r-lg:18px;
  --r-md:12px;
  --r-sm:9px;
  --sans:'Plus Jakarta Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --display:'Bricolage Grotesque','Plus Jakarta Sans',system-ui,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}
[data-theme=dark]{
  --paper:#080B15;
  --surface:#111629;
  --surface-2:#182039;
  --ink:#E9EDFB;
  --ink-2:#A3AAC8;
  --ink-3:#747C9C;
  --line:#232B47;
  --line-soft:#1B2239;
  --g1:#6D65FF;
  --g2:#A855F7;
  --g3:#22D3EE;
  --accent:#8B93FF;
  --accent-deep:#AEB3FF;
  --accent-wash:#1B1F41;
  --on-accent:#0A0E1C;
  --good:#34D399;
  --good-wash:#0D2A22;
  --bad:#FB7185;
  --bad-wash:#331420;
  --gold:#F0B429;
  --gold-wash:#2E2310;
  --ring-track:#232B47;
  --glass:rgba(17,22,41,.74);
  --shadow-1:0 1px 2px rgba(0,0,0,.5);
  --shadow-2:0 2px 6px rgba(0,0,0,.4), 0 22px 48px -26px rgba(0,0,0,.95);
  --shadow-pop:0 24px 60px -26px rgba(109,101,255,.6);
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;scroll-padding-top:calc(env(safe-area-inset-top,0px) + 76px);}
body{
  margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--sans);font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased;
  padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);
  overflow-x:hidden;
}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
[hidden]{display:none !important;}
.ic{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;}
::selection{background:var(--accent-wash);color:var(--ink);}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;}

/* ---------- page frame ---------- */
.topbar{
  position:sticky;top:0;z-index:40;background:var(--glass);backdrop-filter:blur(14px);
  -webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line);
  padding-top:env(safe-area-inset-top,0px);
}
.topbar-in{
  max-width:1080px;margin:0 auto;padding:11px 20px;
  display:flex;align-items:center;justify-content:space-between;gap:16px;
}
.topbar-in.wide,.wrap.wide{max-width:1260px;}
.brand{display:flex;align-items:center;gap:11px;min-width:0;color:inherit;}
.brand:hover{text-decoration:none;}
.brand-mark{
  flex:none;width:36px;height:36px;border-radius:12px;background:var(--grad);color:#fff;
  display:grid;place-items:center;box-shadow:var(--shadow-pop);
}
.brand-mark .ic{width:18px;height:18px;stroke-width:2.6;}
.brand-text{min-width:0;}
.brand-name{
  font-family:var(--display);font-weight:700;font-size:1.04rem;line-height:1.2;letter-spacing:-.02em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.brand-sub{font-size:.78rem;color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.3;}
.topbar-right{display:flex;align-items:center;gap:8px;flex:none;}
.icon-btn{
  width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:var(--surface);
  color:var(--ink-2);display:grid;place-items:center;cursor:pointer;padding:0;
  transition:color .15s,border-color .15s,transform .12s;
}
.icon-btn:hover{color:var(--ink);border-color:var(--ink-3);transform:translateY(-1px);}
[data-theme=light] .moon{display:block;}
[data-theme=light] .sun{display:none;}
[data-theme=dark] .moon{display:none;}
[data-theme=dark] .sun{display:block;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 72px;position:relative;z-index:1;}
.narrow{max-width:720px;margin:0 auto;}
.foot{border-top:1px solid var(--line);}
.foot-in{
  max-width:1080px;margin:0 auto;padding:22px 20px;display:flex;justify-content:space-between;
  gap:14px;flex-wrap:wrap;color:var(--ink-3);font-size:.84rem;
}
.foot-in a{color:var(--ink-2);}

/* the single bold flourish: a soft aurora behind the first screen */
.aurora{position:fixed;inset:0;z-index:0;pointer-events:none;overflow:hidden;opacity:.55;}
.aurora i{position:absolute;display:block;border-radius:50%;filter:blur(90px);}
.aurora i:nth-child(1){width:46vw;height:46vw;left:-12vw;top:-16vw;background:var(--g1);opacity:.3;}
.aurora i:nth-child(2){width:38vw;height:38vw;right:-10vw;top:-6vw;background:var(--g3);opacity:.22;}
.aurora i:nth-child(3){width:40vw;height:40vw;left:36vw;top:28vw;background:var(--g2);opacity:.16;}
[data-theme=dark] .aurora{opacity:.75;}

/* ---------- type ---------- */
h1{font-family:var(--display);font-size:2.1rem;line-height:1.08;margin:0 0 12px;font-weight:700;letter-spacing:-.035em;}
h2{font-family:var(--display);font-size:1.24rem;line-height:1.22;margin:0 0 4px;font-weight:700;letter-spacing:-.02em;}
h3{font-size:1rem;margin:0 0 4px;font-weight:700;}
.lead{color:var(--ink-2);margin:0 0 26px;max-width:62ch;}
.muted{color:var(--ink-2);font-size:.9rem;}
.tab-num{font-variant-numeric:tabular-nums;}

/* ---------- code in questions ---------- */
code{font-family:var(--mono);font-size:.88em;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:1px 6px;}
pre.code{
  margin:14px 0 18px;padding:14px 18px;background:#0D1226;color:#E6EAFB;
  border:1px solid var(--line);border-radius:var(--r-md);overflow-x:auto;-webkit-overflow-scrolling:touch;
  white-space:pre;tab-size:4;font-family:var(--mono);font-size:.86rem;line-height:1.65;
  font-weight:500;letter-spacing:0;text-align:left;
}
pre.code code{background:none;border:0;padding:0;font-size:inherit;color:inherit;}
.code-lang{display:block;margin-bottom:8px;font:700 .68rem var(--mono);text-transform:uppercase;letter-spacing:.08em;opacity:.55;}
textarea.code-area{font-family:var(--mono);font-size:.9rem;tab-size:4;}
.q-text,.rv-q,.qrow-q,.rv-x{white-space:pre-line;overflow-wrap:anywhere;}
.qrow-q{font-weight:600;}
.qrow > div:first-child,.rv > div:last-child{min-width:0;flex:1;}
.opt-text{min-width:0;overflow-wrap:anywhere;}

/* ---------- cards, pills ---------- */
.card{
  border:1px solid var(--line);border-radius:var(--r-xl);padding:28px;margin-bottom:20px;
  background:var(--surface);box-shadow:var(--shadow-1);
}
.card.tight{padding:22px;}
.pill{
  display:inline-flex;align-items:center;gap:7px;padding:5px 13px;border-radius:999px;
  background:var(--accent-wash);color:var(--accent-deep);font-weight:700;font-size:.8rem;letter-spacing:.01em;
}
.pill.ok{background:var(--good-wash);color:var(--good);}
.pill.bad{background:var(--bad-wash);color:var(--bad);}
.pill.gold{background:var(--gold-wash);color:var(--gold);}
.pill .ic{width:14px;height:14px;}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none;}
.dot.live{animation:pulse 1.8s ease-out infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 currentColor;opacity:1;}70%{box-shadow:0 0 0 7px transparent;}100%{box-shadow:0 0 0 0 transparent;}}

/* ---------- forms ---------- */
.field{display:block;margin-bottom:16px;}
.field > span{display:block;font-weight:600;font-size:.87rem;margin-bottom:6px;}
.field > span small{font-weight:400;color:var(--ink-2);}
input[type=text],input[type=tel],input[type=password],input[type=number],select,textarea{
  width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:var(--r-md);
  font:inherit;font-size:16px;color:var(--ink);background:var(--surface);outline:none;
  transition:border-color .15s, box-shadow .15s;
}
[data-theme=dark] input,[data-theme=dark] select,[data-theme=dark] textarea{background:var(--surface-2);}
textarea{resize:vertical;line-height:1.55;}
input::placeholder,textarea::placeholder{color:var(--ink-3);}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-wash);}
input[readonly]{background:var(--surface-2);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 14px;}
.check{display:flex;gap:12px;align-items:flex-start;margin-bottom:15px;cursor:pointer;}
.check input{width:20px;height:20px;margin-top:2px;accent-color:var(--accent);flex:none;}
.check b{display:block;font-size:.93rem;font-weight:700;}
.check small{color:var(--ink-2);}
.pw{position:relative;}
.pw input{padding-right:48px;}
.pw button{position:absolute;right:5px;top:5px;width:38px;height:38px;border:none;background:none;color:var(--ink-3);cursor:pointer;border-radius:9px;display:grid;place-items:center;}
.pw button:hover{background:var(--surface-2);color:var(--ink);}

/* ---------- buttons ---------- */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:12px 20px;border-radius:var(--r-md);border:1px solid transparent;
  background:var(--grad);color:#fff;font:inherit;font-weight:700;cursor:pointer;
  text-decoration:none;transition:transform .12s, box-shadow .18s, filter .15s;
  box-shadow:0 10px 24px -14px rgba(79,70,229,.9);
}
.btn:hover{text-decoration:none;color:#fff;filter:saturate(1.12) brightness(1.04);box-shadow:var(--shadow-pop);transform:translateY(-1px);}
.btn:active{transform:translateY(1px);}
.btn .ic{width:17px;height:17px;}
.btn:focus-visible,.pb:focus-visible,.nav-item:focus-visible,.icon-btn:focus-visible,.pw button:focus-visible,.seg button:focus-visible,.opt:focus-within{outline:2px solid var(--accent);outline-offset:3px;}
.btn-full{width:100%;margin-top:4px;}
.btn-ghost{background:var(--surface);color:var(--ink);border-color:var(--line);box-shadow:var(--shadow-1);}
.btn-ghost:hover{background:var(--surface-2);border-color:var(--ink-3);color:var(--ink);filter:none;}
.btn-ghost.on{background:var(--gold-wash);border-color:var(--gold);color:var(--gold);}
.btn-danger{background:var(--surface);color:var(--bad);border-color:var(--line);box-shadow:none;}
.btn-danger:hover{background:var(--bad-wash);border-color:var(--bad);color:var(--bad);filter:none;}
.btn-sm{padding:8px 13px;font-size:.86rem;border-radius:var(--r-sm);}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.btn:disabled{opacity:.5;cursor:not-allowed;transform:none;}
.btn.busy{position:relative;color:transparent !important;pointer-events:none;}
.btn.busy .ic{opacity:0;}
.btn.busy::after{
  content:"";position:absolute;left:50%;top:50%;width:17px;height:17px;margin:-8.5px 0 0 -8.5px;
  border-radius:50%;border:2.5px solid #fff;border-right-color:transparent;animation:spin .7s linear infinite;
}
.btn-ghost.busy::after{border-color:var(--ink-2);border-right-color:transparent;}
.btn-danger.busy::after{border-color:var(--bad);border-right-color:transparent;}
@keyframes spin{to{transform:rotate(360deg);}}
.small-link{display:block;text-align:center;margin-top:14px;font-size:.88rem;}
.back{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:.88rem;margin-bottom:16px;}

/* ---------- flash + toasts ---------- */
.flash{
  display:flex;gap:10px;align-items:flex-start;padding:12px 15px;border-radius:var(--r-md);
  margin-bottom:18px;font-size:.93rem;background:var(--accent-wash);color:var(--accent-deep);
  border-left:3px solid var(--accent);transition:opacity .4s;
}
.flash.error{background:var(--bad-wash);color:var(--bad);border-left-color:var(--bad);}
.flash.success{background:var(--good-wash);color:var(--good);border-left-color:var(--good);}
.toasts{position:fixed;left:0;right:0;bottom:calc(22px + env(safe-area-inset-bottom,0px));display:flex;flex-direction:column;align-items:center;gap:8px;z-index:100;pointer-events:none;padding:0 16px;}
.toast{
  background:var(--ink);color:var(--paper);padding:11px 18px;border-radius:999px;font-weight:600;font-size:.9rem;
  box-shadow:var(--shadow-2);transition:opacity .3s, transform .3s;max-width:420px;text-align:center;
}
.toast.warn{background:var(--gold);color:#1B1405;}
.toast.bad{background:var(--bad);color:#fff;}
.toast.out{opacity:0;transform:translateY(8px);}

/* ---------- landing ---------- */
.hero{display:grid;grid-template-columns:1fr;gap:36px;align-items:start;}
@media (min-width:920px){.hero{grid-template-columns:1.02fr .98fr;gap:60px;padding-top:12px;}}
.hero h1{font-size:clamp(2.3rem,6vw,3.4rem);margin:18px 0 14px;font-weight:800;}
.hero .lead{font-size:1.06rem;}
.spec{margin:28px 0 0;display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line);border-radius:var(--r-lg);overflow:hidden;}
.spec div{background:var(--surface);padding:14px 16px;}
.spec dt{color:var(--ink-2);font-size:.82rem;margin-bottom:2px;}
.spec dd{margin:0;font-weight:700;font-family:var(--display);font-size:1.05rem;letter-spacing:-.01em;}
.steps{list-style:none;margin:30px 0 0;padding:0;counter-reset:s;}
.steps li{display:flex;gap:14px;position:relative;padding-bottom:18px;}
.steps li:last-child{padding-bottom:0;}
.steps li::after{content:"";position:absolute;left:14px;top:32px;bottom:0;width:1px;background:var(--line);}
.steps li:last-child::after{display:none;}
.steps .n{
  counter-increment:s;flex:none;width:29px;height:29px;border-radius:50%;
  border:1px solid var(--line);background:var(--surface);color:var(--ink-2);
  display:grid;place-items:center;font-size:.8rem;font-weight:700;font-family:var(--mono);
}
.steps .n::before{content:counter(s);}
.steps b{display:block;font-weight:700;line-height:1.35;padding-top:3px;}
.steps small{color:var(--ink-2);}
.panel-card{
  position:relative;background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-xl);box-shadow:var(--shadow-2);overflow:hidden;
}
.panel-card::before{content:"";position:absolute;left:0;right:0;top:0;height:4px;background:var(--grad);}
.panel-top{
  padding:20px 26px 16px;border-bottom:1px solid var(--line);background:var(--surface-2);
  display:flex;justify-content:space-between;align-items:center;gap:12px;
}
.panel-top h2{margin:0;}
.panel-top small{display:block;color:var(--ink-2);font-size:.83rem;}
.panel-body{padding:24px 26px 26px;}
.bubbles{display:flex;gap:7px;align-items:center;}
.bubbles i{width:15px;height:15px;border-radius:50%;border:1.5px solid var(--line);}
.bubbles i.on{background:var(--grad);border-color:transparent;}
.empty{text-align:center;padding:14px 0 6px;}
.empty .ic{width:30px;height:30px;color:var(--ink-3);margin-bottom:8px;}
.mini-board{margin-top:14px;}
.mini-row{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid var(--line-soft);font-size:.92rem;}
.mini-row:last-child{border-bottom:none;}
.rank{flex:none;width:26px;height:26px;border-radius:9px;display:grid;place-items:center;font-family:var(--mono);font-size:.78rem;font-weight:700;background:var(--surface-2);color:var(--ink-2);}
.rank.r1{background:var(--grad);color:#fff;}
.rank.r2,.rank.r3{background:var(--accent-wash);color:var(--accent-deep);}
.mini-row .who{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;}
.mini-row .sc{font-family:var(--mono);font-weight:700;}

/* ---------- instructions ---------- */
.who-strip{display:flex;align-items:center;gap:14px;margin-bottom:22px;padding-bottom:20px;border-bottom:1px solid var(--line);}
.avatar{
  width:48px;height:48px;border-radius:15px;background:var(--grad);color:#fff;
  display:grid;place-items:center;font-family:var(--display);font-weight:700;font-size:1.25rem;flex:none;
}
.who-name{font-weight:700;font-size:1.06rem;line-height:1.25;}
.rules{list-style:none;margin:18px 0 22px;padding:0;}
.rules li{display:flex;gap:12px;padding:10px 0;border-bottom:1px dashed var(--line-soft);}
.rules li:last-child{border-bottom:none;}
.rules .ic{color:var(--accent);margin-top:3px;width:18px;height:18px;}
.note{
  background:var(--gold-wash);border-left:3px solid var(--gold);border-radius:var(--r-sm);
  padding:12px 15px;margin:0 0 20px;white-space:pre-line;color:var(--ink);font-size:.95rem;
}

/* ---------- the arena (test screen) ---------- */
.hud{
  position:sticky;top:0;z-index:25;background:var(--glass);backdrop-filter:blur(14px);
  -webkit-backdrop-filter:blur(14px);
  margin:0 -20px 18px;padding:calc(12px + env(safe-area-inset-top,0px)) 20px 12px;border-bottom:1px solid var(--line);
}
.hud-row{display:flex;align-items:center;justify-content:space-between;gap:14px;}
.hud-who{min-width:0;}
.hud-name{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:44vw;line-height:1.25;}
.hud-meta{font-size:.78rem;color:var(--ink-2);}
.clock{position:relative;width:62px;height:62px;flex:none;}
.clock svg{transform:rotate(-90deg);width:62px;height:62px;}
.clock circle{fill:none;stroke-width:5;stroke-linecap:round;}
.clock .bgc{stroke:var(--ring-track);}
.clock .fgc{stroke:url(#gradstroke);transition:stroke-dashoffset .5s linear;}
.clock.warn .fgc{stroke:var(--gold);}
.clock.low .fgc{stroke:var(--bad);}
.clock b{
  position:absolute;inset:0;display:grid;place-items:center;font-family:var(--mono);
  font-size:.86rem;font-weight:700;letter-spacing:-.04em;
}
.clock.low b{color:var(--bad);}
.hud-stats{display:flex;gap:8px;align-items:center;}
.chipstat{
  display:flex;flex-direction:column;align-items:center;justify-content:center;min-width:56px;
  padding:6px 10px;border-radius:var(--r-md);background:var(--surface);border:1px solid var(--line);line-height:1.15;
}
.chipstat b{font-family:var(--mono);font-size:.95rem;font-weight:700;}
.chipstat small{font-size:.64rem;color:var(--ink-3);letter-spacing:.02em;}
.chipstat.hot b{color:var(--gold);}
.track{height:5px;background:var(--ring-track);border-radius:999px;overflow:hidden;margin-top:12px;}
.bar{height:100%;width:0;background:var(--grad);border-radius:999px;transition:width .35s cubic-bezier(.22,1,.36,1);}
.hud-count{display:flex;justify-content:space-between;font-size:.76rem;color:var(--ink-2);margin-top:6px;}
.hud-count .saved{display:inline-flex;align-items:center;gap:5px;}
.hud-count .saved .ic{width:13px;height:13px;color:var(--good);}
.hud-count .saved.flash-save{animation:savepop .6s ease;}
@keyframes savepop{0%{opacity:.35;}40%{opacity:1;}100%{opacity:1;}}

.drawer{border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 16px;margin-bottom:16px;background:var(--surface);}
.drawer-top{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;}
.drawer-top b{font-size:.92rem;font-weight:700;}
.pal-grid{display:flex;flex-wrap:wrap;gap:7px;margin-top:13px;}
.pb{
  position:relative;width:38px;height:38px;border-radius:12px;border:1px solid var(--line);background:var(--surface);
  font:inherit;font-family:var(--mono);font-weight:700;font-size:.82rem;color:var(--ink-3);cursor:pointer;
  transition:border-color .12s, transform .12s;
}
.pb:hover{border-color:var(--ink-3);transform:translateY(-1px);}
.pb.answered{background:var(--accent-wash);border-color:transparent;color:var(--accent-deep);}
.pb.current{background:var(--grad);border-color:transparent;color:#fff;box-shadow:0 8px 18px -10px rgba(79,70,229,.9);}
.pb.flagged::after{content:"";position:absolute;top:-3px;right:-3px;width:11px;height:11px;border-radius:50%;background:var(--gold);border:2px solid var(--surface);}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:.77rem;color:var(--ink-2);margin-top:13px;}
.legend i{display:inline-block;width:11px;height:11px;border-radius:4px;margin-right:6px;vertical-align:-1px;border:1px solid var(--line);}
.legend .l-ans{background:var(--accent-wash);border-color:transparent;}
.legend .l-cur{background:var(--grad);border-color:transparent;}
.legend .l-flag{background:var(--gold);border-color:transparent;}

.q{display:none;border:1px solid var(--line);border-radius:var(--r-xl);padding:28px;background:var(--surface);box-shadow:var(--shadow-1);}
.q.active{display:block;animation:qin .34s cubic-bezier(.22,1,.36,1);}
@keyframes qin{from{opacity:0;transform:translateY(10px) scale(.995);}to{opacity:1;transform:none;}}
.q-top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px;margin-bottom:16px;}
.q-tags{display:flex;gap:6px;flex-wrap:wrap;min-width:0;}
.q-side{display:flex;align-items:center;gap:10px;flex:none;}
.tag{font-size:.72rem;font-weight:700;padding:3px 9px;border-radius:999px;background:var(--surface-2);color:var(--ink-2);}
.tag.easy{background:var(--good-wash);color:var(--good);}
.tag.medium{background:var(--gold-wash);color:var(--gold);}
.tag.hard{background:var(--bad-wash);color:var(--bad);}
.q-count{font-family:var(--mono);font-size:.8rem;color:var(--ink-3);font-weight:700;}

/* flag for review: a small pill on the question card */
.flag-toggle{
  display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px 0 10px;border-radius:999px;
  border:1px solid var(--line);background:var(--surface);color:var(--ink-2);
  font:inherit;font-size:.8rem;font-weight:700;cursor:pointer;
  transition:background .15s,border-color .15s,color .15s;
}
.flag-toggle .ic{width:15px;height:15px;}
.flag-toggle .l-on{display:none;}
.flag-toggle:hover{border-color:var(--gold);color:var(--gold);}
.flag-toggle[aria-pressed=true]{background:var(--gold-wash);border-color:var(--gold);color:var(--gold);}
.flag-toggle[aria-pressed=true] .ic{fill:currentColor;}
.flag-toggle[aria-pressed=true] .l-on{display:inline;}
.flag-toggle[aria-pressed=true] .l-off{display:none;}
.flag-toggle:focus-visible{outline:2px solid var(--accent);outline-offset:3px;}

.q-text{font-family:var(--display);font-weight:600;font-size:1.3rem;line-height:1.34;letter-spacing:-.02em;margin-bottom:20px;}
.q-text code{font-family:var(--mono);font-weight:500;letter-spacing:0;}
.opt{
  position:relative;display:flex;align-items:center;gap:13px;padding:14px 16px;
  border:1px solid var(--line);border-radius:var(--r-md);margin-bottom:9px;cursor:pointer;
  transition:border-color .15s, background .15s, transform .12s;
}
.opt:last-child{margin-bottom:0;}
.opt:hover{border-color:var(--ink-3);transform:translateX(2px);}
.opt input{position:absolute;opacity:0;pointer-events:none;}
.letter{
  flex:none;width:28px;height:28px;border-radius:9px;background:var(--surface);border:1.5px solid var(--line);
  display:grid;place-items:center;font-family:var(--mono);font-weight:700;font-size:.78rem;color:var(--ink-3);
  transition:background .15s, color .15s, border-color .15s;
}
.opt:has(input:checked){border-color:var(--accent);background:var(--accent-wash);}
.opt input:checked + .letter{background:var(--grad);border-color:transparent;color:#fff;animation:pop .28s cubic-bezier(.3,1.6,.5,1);}
@keyframes pop{0%{transform:scale(.8);}60%{transform:scale(1.14);}100%{transform:scale(1);}}
.qnav{display:flex;justify-content:space-between;gap:12px;margin-top:18px;}
.qnav .btn{min-width:130px;}
.hint{text-align:center;color:var(--ink-3);font-size:.79rem;margin-top:16px;}
kbd{font-family:var(--mono);font-size:.74rem;background:var(--surface-2);border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 5px;color:var(--ink-2);}

dialog.dlg{
  border:1px solid var(--line);border-radius:var(--r-xl);padding:26px;max-width:480px;width:calc(100% - 32px);
  color:var(--ink);background:var(--surface);font-family:inherit;box-shadow:var(--shadow-2);
}
dialog.dlg::backdrop{background:rgba(6,9,20,.62);backdrop-filter:blur(3px);}
.dlg-sec{margin:16px 0 4px;}
.dlg-sec b{display:block;font-size:.85rem;margin-bottom:8px;font-weight:700;}
.jump{display:flex;flex-wrap:wrap;gap:6px;}
.jump button{
  min-width:34px;height:34px;padding:0 9px;border-radius:var(--r-sm);border:1px solid var(--line);background:var(--surface);
  font:inherit;font-family:var(--mono);font-weight:700;font-size:.82rem;cursor:pointer;color:var(--ink);
}
.jump button:hover{border-color:var(--accent);color:var(--accent);}
.dlg .btn-row{margin-top:22px;flex-wrap:nowrap;}
.dlg .btn-row .btn{flex:1;}
.keys{display:grid;grid-template-columns:auto 1fr;gap:9px 16px;font-size:.9rem;align-items:center;margin-top:14px;}
.keys span{color:var(--ink-2);}

/* ---------- report card ---------- */
.report-hero{text-align:center;position:relative;overflow:hidden;}
.report-hero::before{content:"";position:absolute;inset:0 0 auto;height:5px;background:var(--grad);}
.ring{position:relative;width:176px;height:176px;margin:24px auto 18px;}
.ring svg{transform:rotate(-90deg);width:176px;height:176px;}
.ring circle{fill:none;stroke-width:12;stroke-linecap:round;}
.ring .bgc{stroke:var(--ring-track);}
.ring .fgc{stroke:url(#gradstroke);}
.ring .val{
  position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  font-family:var(--display);font-size:2.5rem;font-weight:800;letter-spacing:-.04em;line-height:1;
}
.ring .val small{font-family:var(--sans);font-size:.72rem;font-weight:600;color:var(--ink-3);letter-spacing:0;margin-top:6px;}
.grade{
  display:inline-flex;align-items:center;gap:8px;padding:7px 18px;border-radius:999px;
  background:var(--grad);color:#fff;font-family:var(--display);font-weight:700;font-size:1rem;margin:8px 0 12px;
  box-shadow:var(--shadow-pop);
}
.verdict{display:inline-flex;align-items:center;gap:7px;font-weight:700;font-size:.88rem;margin-bottom:6px;}
.verdict.pass{color:var(--good);}
.verdict.fail{color:var(--bad);}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(102px,1fr));gap:10px;margin-top:26px;}
.chip{border:1px solid var(--line);border-radius:var(--r-lg);padding:14px 8px;background:var(--surface);}
.chip b{display:block;font-family:var(--display);font-size:1.32rem;font-weight:700;line-height:1.3;letter-spacing:-.02em;}
.chip small{color:var(--ink-2);font-size:.77rem;}
.chip.ok b{color:var(--good);}
.chip.bad b{color:var(--bad);}
.topics{margin-top:8px;}
.trow{display:grid;grid-template-columns:1fr 78px 46px;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line-soft);font-size:.93rem;}
.trow:last-child{border-bottom:none;}
.trow .tname{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.trow .pc{text-align:right;font-family:var(--mono);font-weight:700;font-size:.85rem;}
.meter{height:8px;background:var(--surface-2);border-radius:999px;overflow:hidden;}
.meter i{display:block;height:100%;width:0;background:var(--grad);border-radius:999px;transition:width .9s cubic-bezier(.22,1,.36,1);}
.meter.good i{background:var(--good);}
.meter.mid i{background:var(--gold);}
.meter.low i{background:var(--bad);}
.cmp{margin-top:26px;padding-top:22px;border-top:1px solid var(--line);text-align:left;}
.cmp-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px;}
.cmp-track{position:relative;height:6px;background:var(--ring-track);border-radius:999px;margin:26px 9px 16px;}
.cmp-track i{position:absolute;top:50%;width:17px;height:17px;border-radius:50%;transform:translate(-50%,-50%);border:3px solid var(--surface);}
.cmp-track .you,.k.you{background:var(--accent);}
.cmp-track .avg,.k.avg{background:var(--gold);}
.cmp-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:.86rem;}
.cmp-legend .k{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;}
.rv-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:3px;background:var(--surface);}
.seg button{border:none;background:none;padding:6px 13px;border-radius:999px;font:inherit;font-weight:600;font-size:.83rem;color:var(--ink-2);cursor:pointer;}
.seg button.on{background:var(--ink);color:var(--paper);}
.rv{display:flex;gap:13px;padding:16px 0;border-bottom:1px solid var(--line-soft);}
.rv-list .rv:last-child{border-bottom:none;}
.rv-mark{
  flex:none;width:27px;height:27px;border-radius:9px;display:grid;place-items:center;
  font-weight:700;font-size:.8rem;margin-top:2px;
}
.rv-mark.ok{background:var(--good-wash);color:var(--good);}
.rv-mark.bad{background:var(--bad-wash);color:var(--bad);}
.rv-mark.skip{background:var(--surface-2);color:var(--ink-3);}
.rv-q{font-weight:700;}
.rv-meta{font-size:.76rem;color:var(--ink-3);margin-bottom:4px;}
.rv-a{font-size:.9rem;color:var(--ink-2);}
.rv-a b{color:var(--ink);font-weight:700;}
.rv-a .good{color:var(--good);}
.rv-x{margin-top:9px;padding:10px 13px;background:var(--surface-2);border-radius:var(--r-sm);font-size:.89rem;color:var(--ink-2);}
.print-only{display:none;}
.cf{position:fixed;top:-16px;width:9px;height:14px;border-radius:2px;pointer-events:none;z-index:60;animation:fall linear forwards;}
@keyframes fall{to{transform:translate(var(--dx),110vh) rotate(760deg);}}

/* certificate */
.cert{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--r-xl);
  padding:8px;box-shadow:var(--shadow-2);
}
.cert-in{border:2px solid var(--accent);border-radius:var(--r-lg);padding:44px 34px;text-align:center;position:relative;}
.cert-in::before,.cert-in::after{content:"";position:absolute;width:56px;height:56px;border:3px solid var(--accent);opacity:.3;}
.cert-in::before{top:12px;left:12px;border-right:none;border-bottom:none;border-radius:12px 0 0 0;}
.cert-in::after{bottom:12px;right:12px;border-left:none;border-top:none;border-radius:0 0 12px 0;}
.cert h1{font-size:2.1rem;margin:14px 0 6px;}
.cert .name{font-family:var(--display);font-size:2.1rem;font-weight:800;letter-spacing:-.03em;margin:22px 0 8px;
  background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;}
.cert .seal{width:72px;height:72px;border-radius:50%;background:var(--grad);color:#fff;display:grid;place-items:center;margin:26px auto 0;box-shadow:var(--shadow-pop);}
.cert .seal .ic{width:32px;height:32px;stroke-width:2.2;}
.cert-meta{display:flex;justify-content:center;gap:28px;flex-wrap:wrap;margin-top:26px;padding-top:20px;border-top:1px solid var(--line);font-size:.86rem;color:var(--ink-2);}
.cert-meta b{display:block;color:var(--ink);font-family:var(--mono);}

/* ---------- leaderboard ---------- */
.board{border:1px solid var(--line);border-radius:var(--r-xl);overflow:hidden;background:var(--surface);}
.brow{display:grid;grid-template-columns:44px 1fr auto auto;gap:14px;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line-soft);}
.brow:last-child{border-bottom:none;}
.brow.me{background:var(--accent-wash);}
.brow .nm{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.brow .cl{font-size:.8rem;color:var(--ink-2);}
.brow .sc{font-family:var(--mono);font-weight:700;font-size:1.02rem;}
.brow .tm{font-family:var(--mono);font-size:.82rem;color:var(--ink-3);min-width:52px;text-align:right;}
.podium{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px;align-items:end;}
.pod{border:1px solid var(--line);border-radius:var(--r-lg);padding:16px 12px;text-align:center;background:var(--surface);}
.pod.first{background:var(--grad);color:#fff;border-color:transparent;box-shadow:var(--shadow-pop);padding-top:24px;padding-bottom:24px;}
.pod.first .p-sub{color:rgba(255,255,255,.82);}
.pod .p-rank{font-family:var(--mono);font-size:.78rem;font-weight:700;opacity:.8;}
.pod .p-name{font-family:var(--display);font-weight:700;font-size:1rem;margin:6px 0 2px;letter-spacing:-.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.pod .p-sub{font-size:.78rem;color:var(--ink-2);}
.pod .p-score{font-family:var(--mono);font-weight:700;font-size:1.3rem;margin-top:8px;}

/* ---------- admin ---------- */
.dash-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:24px;}
.dash-top h1{margin-bottom:8px;}
.admin{display:grid;grid-template-columns:208px minmax(0,1fr);gap:34px;align-items:start;}
.side{position:sticky;top:82px;}
.navlist{display:flex;flex-direction:column;gap:3px;}
.nav-item{
  display:flex;align-items:center;gap:11px;width:100%;padding:10px 13px;border:none;background:none;border-radius:var(--r-md);
  font:inherit;font-weight:600;color:var(--ink-2);cursor:pointer;text-align:left;
}
.nav-item:hover{background:var(--surface-2);color:var(--ink);}
.nav-item.active{background:var(--ink);color:var(--paper);}
.panel{display:none;}
.panel.active{display:block;animation:qin .28s ease;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(166px,1fr));gap:12px;margin-bottom:20px;}
.stat{display:flex;gap:13px;align-items:center;border:1px solid var(--line);border-radius:var(--r-lg);padding:15px 17px;background:var(--surface);}
.stat .ico{flex:none;width:40px;height:40px;border-radius:13px;background:var(--accent-wash);color:var(--accent-deep);display:grid;place-items:center;}
.stat b{display:block;font-family:var(--display);font-size:1.55rem;font-weight:700;line-height:1.2;letter-spacing:-.03em;}
.stat span{font-size:.8rem;color:var(--ink-2);}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
.two > .card{margin-bottom:20px;}
.share{display:flex;gap:18px;justify-content:space-between;align-items:center;flex-wrap:wrap;}
.share-row{display:flex;gap:10px;flex-wrap:wrap;flex:1;min-width:280px;justify-content:flex-end;}
.share-row input{flex:1;min-width:190px;font-size:.9rem;font-family:var(--mono);}
.hist{display:flex;align-items:flex-end;gap:6px;height:150px;margin-top:16px;}
.hcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:.7rem;color:var(--ink-3);gap:4px;font-family:var(--mono);}
.hbar{width:100%;background:var(--grad);border-radius:7px 7px 3px 3px;min-height:3px;}
.hbar.zero{background:var(--line);}
.gchips{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;}
.gchips span{padding:5px 12px;border-radius:999px;border:1px solid var(--line);font-size:.83rem;color:var(--ink-2);}
.gchips b{color:var(--ink);font-weight:700;font-family:var(--mono);}
.irow{display:grid;grid-template-columns:1fr 140px 46px;gap:14px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line-soft);font-size:.92rem;}
.irow:last-child{border-bottom:none;}
.irow .qt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.irow .sub{grid-column:1 / -1;margin-top:-6px;font-size:.79rem;color:var(--ink-3);}
.irow .pc{font-weight:700;text-align:right;font-family:var(--mono);font-size:.85rem;}
.irow.two-col{grid-template-columns:1fr 46px;}
.qrow{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--line-soft);}
.qrow:last-child{border-bottom:none;}
.qrow b{font-weight:700;}
.qrow .opts{font-size:.88rem;color:var(--ink-2);margin-top:5px;}
.qrow .opts .right{color:var(--good);font-weight:700;}
.qrow .qtags{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
.qrow .acts{display:flex;gap:8px;flex:none;}
.tools{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;margin-bottom:16px;}
.tools .filters{display:flex;gap:10px;flex-wrap:wrap;}
.tools input{width:220px;}
.tools select{width:152px;}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;font-size:.9rem;min-width:820px;}
th{text-align:left;color:var(--ink-2);font-weight:600;font-size:.79rem;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;user-select:none;}
th[data-dir=asc]::after{content:" \\2191";}
th[data-dir=desc]::after{content:" \\2193";}
td{padding:11px 10px;border-bottom:1px solid var(--line-soft);white-space:nowrap;}
td a{font-weight:700;}
td.warn{color:var(--gold);font-weight:700;}
code.fmt{
  display:block;background:var(--surface-2);border:1px solid var(--line);border-radius:var(--r-sm);
  padding:10px 12px;font-family:var(--mono);font-size:.76rem;margin:0 0 14px;overflow-x:auto;white-space:nowrap;color:var(--ink-2);
}
.danger-card{border-color:var(--bad);}

@media (max-width:900px){
  .admin{grid-template-columns:1fr;gap:16px;}
  .side{position:static;}
  .navlist{flex-direction:row;overflow-x:auto;border-bottom:1px solid var(--line);padding-bottom:8px;gap:6px;}
  .nav-item{width:auto;white-space:nowrap;}
  .two{grid-template-columns:1fr;}
}
@media (max-width:560px){
  .grid2,.grid3{grid-template-columns:1fr;}
  .card{padding:22px 18px;}
  h1{font-size:1.75rem;}
  .panel-top,.panel-body{padding-left:18px;padding-right:18px;}
  .btn-row .btn{width:100%;}
  .dlg .btn-row .btn{width:auto;}
  .qrow{flex-direction:column;}
  .hud-name{max-width:34vw;}
  .brand-sub{display:none;}
  .q{padding:20px 18px;}
  .irow{grid-template-columns:1fr 46px;}
  .irow .meter{grid-column:1 / -1;order:3;}
  .tools input,.tools select{width:100%;}
  .tools .filters{width:100%;}
  .share-row .btn{flex:1;}
  .chipstat{min-width:48px;padding:5px 8px;}
  .podium{grid-template-columns:1fr;}
  .cert-in{padding:30px 18px;}
  .hint{font-size:.76rem;line-height:2.1;}
  pre.code{font-size:.8rem;padding:12px 14px;}
  /* Previous / Next stick to the bottom of the phone screen */
  .qnav{
    position:sticky;bottom:0;z-index:20;margin:18px -20px 0;
    padding:12px 20px calc(12px + env(safe-area-inset-bottom,0px));
    background:var(--glass);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
    border-top:1px solid var(--line);
  }
  .qnav .btn{flex:1;min-width:0;min-height:46px;}
  .q-tags .tag:nth-child(3){display:none;}
}
@media (max-width:480px){
  .chipstat.hot{display:none;}
  .chipstat{min-width:46px;}
  .hud-name{max-width:30vw;}
}
@media (max-width:400px){
  .wrap{padding-left:15px;padding-right:15px;}
  .hud{margin-left:-15px;margin-right:-15px;padding-left:15px;padding-right:15px;}
  .q{padding:18px 15px;}
  .q-text{font-size:1.12rem;}
  .opt{padding:12px 13px;gap:11px;}
  .pb{width:34px;height:34px;font-size:.82rem;}
  .card{padding:20px 15px;}
  .chips{grid-template-columns:1fr 1fr;}
  .qnav{margin-left:-15px;margin-right:-15px;padding-left:15px;padding-right:15px;}
}
@media (hover:none){
  .hint{display:none;}
  #keys-btn{display:none;}
}
@media print{
  body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .no-print,.aurora,.topbar,.foot{display:none !important;}
  .print-only{display:block;}
  .card,.cert{border-color:#ccc;box-shadow:none;break-inside:avoid;}
  .cf,.toasts{display:none;}
  .wrap{padding-top:0;}
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
window.beep = function (kind) {
  if (!window.STP_SOUND) return;
  try {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    window.__ac = window.__ac || new Ctx();
    var ac = window.__ac;
    var map = { pick: [660, 0.05], move: [420, 0.04], warn: [300, 0.16], done: [880, 0.2] };
    var it = map[kind] || map.pick;
    var o = ac.createOscillator(), gnode = ac.createGain();
    o.type = 'sine';
    o.frequency.value = it[0];
    gnode.gain.setValueAtTime(0.0001, ac.currentTime);
    gnode.gain.exponentialRampToValueAtTime(0.07, ac.currentTime + 0.01);
    gnode.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + it[1]);
    o.connect(gnode); gnode.connect(ac.destination);
    o.start(); o.stop(ac.currentTime + it[1] + 0.02);
  } catch (e) {}
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
  [].forEach.call(document.querySelectorAll('[data-code-for]'), function (b) {
    b.addEventListener('click', function () {
      var t = document.getElementById(b.dataset.codeFor);
      if (!t) return;
      var s = t.selectionStart, e = t.selectionEnd, sel = t.value.slice(s, e);
      t.setRangeText('\\n```python\\n' + (sel || '# code yahan likho') + '\\n```\\n', s, e, 'end');
      t.focus();
    });
  });
});
window.addEventListener('pageshow', function (e) {
  if (e.persisted) [].forEach.call(document.querySelectorAll('.btn.busy'), function (b) { b.classList.remove('busy'); b.disabled = false; });
});
</script>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="gradstroke" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="var(--g1)"/><stop offset="55%" stop-color="var(--g2)"/><stop offset="100%" stop-color="var(--g3)"/>
    </linearGradient>
  </defs>
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
  <symbol id="i-bolt" viewBox="0 0 24 24"><path d="M13 3L5 14h6l-1 7 8-11h-6z"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></symbol>
  <symbol id="i-award" viewBox="0 0 24 24"><circle cx="12" cy="9" r="5.5"/><path d="M8.5 13.5L7 21l5-2.5L17 21l-1.5-7.5"/></symbol>
  <symbol id="i-share" viewBox="0 0 24 24"><circle cx="18" cy="5" r="2.6"/><circle cx="6" cy="12" r="2.6"/><circle cx="18" cy="19" r="2.6"/><path d="M8.3 10.8l7.4-4.3M8.3 13.2l7.4 4.3"/></symbol>
  <symbol id="i-layers" viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></symbol>
</svg>
<div class="aurora no-print" aria-hidden="true"><i></i><i></i><i></i></div>
<header class="topbar no-print">
  <div class="topbar-in{{ ' wide' if wide }}">
    <a class="brand" href="{{ url_for('home') }}">
      <span class="brand-mark">{{ icon('bolt') }}</span>
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
    <span>
      {% if cfg.leaderboard %}<a href="{{ url_for('leaderboard') }}">Leaderboard</a> &nbsp;&middot;&nbsp; {% endif %}
      {% if cfg.lookup %}<a href="{{ url_for('lookup') }}">Find my result</a> &nbsp;&middot;&nbsp; {% endif %}
      <a href="{{ url_for('admin_login') }}">Teacher login</a>
    </span>
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


def fmt_points(value):
    """1.0 -> '1', 1.5 -> '1.5'."""
    value = round(float(value or 0), 2)
    return str(int(value)) if value == int(value) else str(value)


def q_marks(row):
    try:
        value = float(row["marks"])
    except (TypeError, ValueError):
        return 1.0
    return value if value > 0 else 1.0


def q_topic(row):
    return (row["topic"] or "General").strip() or "General"


def q_difficulty(row):
    value = (row["difficulty"] or "medium").strip().lower()
    return value if value in DIFFICULTIES else "medium"


# ---- rich text: ```code blocks``` and `inline code` (everything is escaped first) ----
_CODE_BLOCK = re.compile(r"[ \t]*\n?```([A-Za-z0-9+#_-]*)[ \t]*\n(.*?)\n?```[ \t]*\n?", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _inline(chunk):
    return _INLINE_CODE.sub(r"<code>\1</code>", str(escape(chunk)))


@app.template_filter("rich")
def rich(text):
    """Safe formatting: ```code blocks``` and `inline code`. Everything else is escaped."""
    src = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out, pos = [], 0
    for m in _CODE_BLOCK.finditer(src):
        out.append(_inline(src[pos:m.start()]))
        lang = m.group(1).lower()
        label = f'<span class="code-lang">{escape(lang)}</span>' if lang else ""
        out.append(f'<pre class="code">{label}<code>{escape(m.group(2))}</code></pre>')
        pos = m.end()
    out.append(_inline(src[pos:]))
    return Markup("".join(out))


def bulk_escape(value):
    """Keep newlines and | inside one backup line."""
    v = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace("|", "\\p")


def bulk_unescape(value):
    return re.sub(r"\\([\\np])", lambda m: {"\\": "\\", "n": "\n", "p": "|"}[m.group(1)], value)


def sign_result(rid):
    return hmac.new(app.secret_key.encode(), f"result:{rid}".encode(), hashlib.sha256).hexdigest()[:20]


def check_result_key(rid, key):
    return bool(key) and hmac.compare_digest(sign_result(rid), key)


def ordered_questions(conn):
    """Questions in this student's own (shuffled) order, or by id."""
    rows = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    order = session.get("order")
    if not order:
        return rows
    pos = {qid: i for i, qid in enumerate(order)}
    return sorted(rows, key=lambda q: (pos.get(q["id"], 10**9), q["id"]))


def laid_out(questions):
    """Questions with their options in this student's own order, ready for the template."""
    layout = session.get("opts") or {}
    out = []
    for i, q in enumerate(questions, start=1):
        keys = layout.get(str(q["id"])) or ["a", "b", "c", "d"]
        keys = [k for k in keys if k in OPTION_KEYS] or ["a", "b", "c", "d"]
        out.append(
            {
                "id": q["id"],
                "number": i,
                "text": q["question_text"],
                "topic": q_topic(q),
                "difficulty": q_difficulty(q),
                "marks": fmt_points(q_marks(q)),
                "options": [
                    {"key": k, "letter": LETTERS[j], "text": q[OPTION_KEYS[k]]} for j, k in enumerate(keys)
                ],
            }
        )
    return out


def evaluate(questions, answers, negative=0):
    """Return a full result dict for answers shaped like {'12': 'b'}."""
    penalty = max(0, min(100, negative)) / 100.0
    score = 0
    points = 0.0
    max_points = 0.0
    review = []
    topics = {}
    for i, q in enumerate(questions, start=1):
        marks = q_marks(q)
        topic = q_topic(q)
        max_points += marks
        slot = topics.setdefault(topic, {"name": topic, "got": 0.0, "out_of": 0.0, "correct": 0, "total": 0})
        slot["out_of"] += marks
        slot["total"] += 1

        chosen = answers.get(str(q["id"]))
        correct = q["correct_option"]
        if chosen not in OPTION_KEYS:
            status, delta = "skip", 0.0
        elif chosen == correct:
            status, delta = "ok", marks
            score += 1
            slot["correct"] += 1
        else:
            status, delta = "bad", -(marks * penalty)
        points += delta
        slot["got"] += max(delta, 0.0)

        review.append(
            {
                "number": i,
                "question": q["question_text"],
                "topic": topic,
                "difficulty": q_difficulty(q),
                "marks": fmt_points(marks),
                "your": q[OPTION_KEYS[chosen]] if chosen in OPTION_KEYS else "Not answered",
                "correct": q[OPTION_KEYS[correct]],
                "explanation": q["explanation"] or "",
                "status": status,
            }
        )

    points = max(0.0, round(points, 2))
    max_points = round(max_points, 2)
    pct = round(points / max_points * 100) if max_points else 0
    topic_list = []
    for slot in topics.values():
        t_pct = round(slot["got"] / slot["out_of"] * 100) if slot["out_of"] else 0
        topic_list.append(
            {
                "name": slot["name"],
                "pct": t_pct,
                "correct": slot["correct"],
                "total": slot["total"],
                "level": "good" if t_pct >= 70 else "mid" if t_pct >= 40 else "low",
            }
        )
    topic_list.sort(key=lambda t: (-t["pct"], t["name"]))
    return {
        "score": score,
        "total": len(questions),
        "points": points,
        "max_points": max_points,
        "pct": pct,
        "review": review,
        "topics": topic_list,
        "wrong": sum(1 for r in review if r["status"] == "bad"),
        "skipped": sum(1 for r in review if r["status"] == "skip"),
    }


def grade_for(pct, first_name=""):
    who = f", {first_name}" if first_name else ""
    if pct >= 90:
        return "A+", f"Outstanding{who}", "You clearly own this topic. Try the hard questions next time."
    if pct >= 75:
        return "A", f"Strong work{who}", "Close to the top. Fix the few you missed and you are there."
    if pct >= 60:
        return "B", f"Solid effort{who}", "A little more practice on your weak topics will take you higher."
    if pct >= 40:
        return "C", f"Good start{who}", "You are on the right track. Read the review below carefully."
    return "D", f"Keep going{who}", "Every test is practice. Work through the review and come back stronger."


def pct_of(row):
    if row["max_points"] and row["points"] is not None:
        return round(row["points"] / row["max_points"] * 100)
    return round((row["score"] / row["total"]) * 100) if row["total"] else 0


def csv_safe(value):
    """Stop Excel from running text like '=SUM(...)' typed by a student as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def back_to(tab):
    return redirect(url_for("admin_dashboard") + "#" + tab)


def comparison(conn, pct):
    """Class average and percentile, shown only once at least 5 students have submitted."""
    rows = conn.execute("SELECT score, total, points, max_points FROM results WHERE total > 0").fetchall()
    vals = [pct_of(r) for r in rows]
    if len(vals) < 5:
        return None
    below = sum(1 for v in vals if v < pct)
    return {
        "avg": round(sum(vals) / len(vals)),
        "higher_than": round(below / len(vals) * 100),
        "n": len(vals),
    }


def leaderboard_rows(conn, limit=25):
    rows = conn.execute("SELECT * FROM results WHERE total > 0").fetchall()
    ranked = []
    for r in rows:
        ranked.append(
            {
                "id": r["id"],
                "name": r["student_name"],
                "student_class": r["student_class"],
                "pct": pct_of(r),
                "score": r["score"],
                "total": r["total"],
                "time": fmt_clock(r["time_taken"]) or "-",
                "seconds": r["time_taken"] if r["time_taken"] is not None else 10**9,
            }
        )
    ranked.sort(key=lambda x: (-x["pct"], x["seconds"], x["name"].lower()))
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    return ranked[:limit]


# ----------------------------------------------------------------------
# STUDENT FLOW  1) details
# ----------------------------------------------------------------------
HOME_TEMPLATE = """
<div class="hero">
  <section>
    <span class="pill {{ 'ok' if cfg.is_open and n else 'bad' }}"><span class="dot {{ 'live' if cfg.is_open and n }}"></span>{{ 'Open now' if cfg.is_open and n else 'Not accepting answers' }}</span>
    <h1>{{ cfg.test_title }}</h1>
    <p class="lead">
      {% if n %}Enter your details, read the rules, and the clock starts only when you press Begin. Your report card opens the second you submit, with the answers and your weak topics.{% else %}Your teacher is still building the paper. Check back a little later.{% endif %}
    </p>

    {% if n %}
    <dl class="spec">
      <div><dt>Questions</dt><dd>{{ n }} multiple choice</dd></div>
      <div><dt>Time limit</dt><dd>{{ cfg.minutes }} minutes</dd></div>
      <div><dt>Total marks</dt><dd>{{ total_marks }}</dd></div>
      <div><dt>Marking</dt><dd>{{ ('-' ~ cfg.negative ~ '% per wrong') if cfg.negative else 'No negative marking' }}</dd></div>
      <div><dt>Pass mark</dt><dd>{{ cfg.pass_mark }}%</dd></div>
      <div><dt>Result</dt><dd>Instant, with review</dd></div>
    </dl>
    {% endif %}

    <ol class="steps">
      <li><span class="n"></span><span><b>Enter your details</b><small>Name, class, semester and phone number</small></span></li>
      <li><span class="n"></span><span><b>Read the rules</b><small>Nothing starts until you press Begin</small></span></li>
      <li><span class="n"></span><span><b>Answer in the arena</b><small>One question at a time, jump around freely</small></span></li>
      <li><span class="n"></span><span><b>Get your report card</b><small>Score, grade, topic strengths and full answers</small></span></li>
    </ol>
  </section>

  <section>
    <div class="panel-card">
      {% if not cfg.is_open %}
        <div class="panel-top"><div><h2>Test closed</h2><small>No new attempts right now</small></div></div>
        <div class="panel-body empty">
          {{ icon('lock') }}
          <p class="muted" style="margin:0;">Your teacher has paused this test. Ask them when it opens again.</p>
        </div>
      {% elif not n %}
        <div class="panel-top"><div><h2>Questions on the way</h2><small>Nothing to answer yet</small></div></div>
        <div class="panel-body empty">
          {{ icon('inbox') }}
          <p class="muted" style="margin:0;">This paper has no questions yet. Ask your teacher to add them.</p>
        </div>
      {% else %}
        <div class="panel-top">
          <div><h2>Your details</h2><small>Printed on your report card</small></div>
          <div class="bubbles" aria-hidden="true"><i></i><i class="on"></i><i></i><i></i></div>
        </div>
        <div class="panel-body">
          <form method="POST" action="{{ url_for('start_test') }}">
            <label class="field"><span>Full name</span>
              <input type="text" name="name" required maxlength="60" autocomplete="name" placeholder="As written in the register">
            </label>
            <div class="grid2">
              <label class="field"><span>Class</span>
                <input type="text" name="student_class" required maxlength="20" placeholder="10-A">
              </label>
              <label class="field"><span>Semester or term</span>
                <input type="text" name="semester" required maxlength="30" placeholder="Semester 1">
              </label>
            </div>
            <label class="field"><span>Phone number <small>10 digits</small></span>
              <input type="tel" name="phone" required inputmode="numeric" pattern="[0-9]{10}" maxlength="10" autocomplete="tel" placeholder="9876543210">
            </label>
            <button class="btn btn-full" type="submit">Continue</button>
            <p class="muted" style="text-align:center;margin:12px 0 0;font-size:.82rem;">Only your teacher can see these details.</p>
          </form>
        </div>
      {% endif %}
    </div>

    {% if leaders %}
    <div class="card tight" style="margin-top:20px;">
      <div class="rv-head" style="margin-bottom:0;">
        <h2>Top of the class</h2>
        <a class="muted" href="{{ url_for('leaderboard') }}">See all</a>
      </div>
      <div class="mini-board">
        {% for l in leaders %}
        <div class="mini-row">
          <span class="rank r{{ l.rank }}">{{ l.rank }}</span>
          <span class="who">{{ l.name }} <span class="muted">&middot; {{ l.student_class }}</span></span>
          <span class="sc">{{ l.pct }}%</span>
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}
  </section>
</div>
"""


@app.route("/")
def home():
    settings = cfg()
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT marks FROM questions").fetchall()
        leaders = leaderboard_rows(conn, 5) if settings["leaderboard"] else []
    total_marks = fmt_points(sum(q_marks(r) for r in rows))
    return render_template_string(
        page(HOME_TEMPLATE), n=len(rows), total_marks=total_marks, leaders=leaders
    )


@app.route("/start", methods=["POST"])
def start_test():
    settings = cfg()
    if not settings["is_open"]:
        flash("This test is closed right now.", "error")
        return redirect(url_for("home"))

    name = " ".join(request.form.get("name", "").split())[:60]
    student_class = " ".join(request.form.get("student_class", "").split())[:20]
    semester = " ".join(request.form.get("semester", "").split())[:30]
    phone = request.form.get("phone", "").strip()

    if not (name and student_class and semester and phone):
        flash("Fill in every field to continue.", "error")
        return redirect(url_for("home"))
    if not re.fullmatch(r"\d{10}", phone):
        flash("The phone number must be exactly 10 digits.", "error")
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
                flash("You have already submitted this test. Ask your teacher for another attempt.", "error")
                return redirect(url_for("home"))

    if settings["shuffle"]:
        random.shuffle(ids)
    opts = {}
    if settings["shuffle_options"]:
        for qid in ids:
            keys = ["a", "b", "c", "d"]
            random.shuffle(keys)
            opts[str(qid)] = keys

    for key in ("deadline", "started_at", "answers", "time_taken", "submitted_at", "result_id"):
        session.pop(key, None)
    session["student"] = {
        "name": name,
        "student_class": student_class,
        "semester": semester,
        "phone": phone,
    }
    session["submitted"] = False
    session["order"] = ids
    session["opts"] = opts
    return redirect(url_for("instructions"))


# ----------------------------------------------------------------------
# STUDENT FLOW  2) instructions
# ----------------------------------------------------------------------
INSTRUCTIONS_TEMPLATE = """
<div class="narrow">
  <section class="card">
    <div class="who-strip">
      <div class="avatar">{{ student.name[0]|upper }}</div>
      <div>
        <div class="who-name">{{ student.name }}</div>
        <div class="muted">Class {{ student.student_class }} &middot; {{ student.semester }}</div>
      </div>
    </div>
    <h1>Before you begin</h1>
    <p class="lead" style="margin-bottom:0;">Read these once. After this screen the only way back is to start over.</p>
    <ul class="rules">
      <li>{{ icon('list') }}<span><b>{{ n }} question{{ '' if n == 1 else 's' }}</b> worth <b>{{ total_marks }} marks</b>, one correct answer each.</span></li>
      <li>{{ icon('clock') }}<span>You get <b>{{ cfg.minutes }} minutes</b>. The clock starts when you press Begin test, not now.</span></li>
      {% if cfg.negative %}
      <li>{{ icon('bolt') }}<span>A wrong answer costs <b>{{ cfg.negative }}%</b> of that question's marks. A blank answer costs nothing, so skip what you truly do not know.</span></li>
      {% else %}
      <li>{{ icon('bolt') }}<span>Nothing is cut for a wrong answer, so never leave a question blank.</span></li>
      {% endif %}
      <li>{{ icon('flag') }}<span>Move freely between questions, change answers, and flag any question to come back to.</span></li>
      <li>{{ icon('check') }}<span>Answers are saved on this device as you go, so a refresh will not lose them.</span></li>
      <li>{{ icon('target') }}<span>The test submits by itself when the time runs out.</span></li>
      {% if cfg.track_focus %}
      <li>{{ icon('eye') }}<span>Stay on this page. Every switch to another tab or app is counted and your teacher sees the count{% if cfg.max_switches %}, and the test submits itself after <b>{{ cfg.max_switches }}</b> switches{% endif %}.</span></li>
      {% endif %}
      <li>{{ icon('award') }}<span>Pass mark is <b>{{ cfg.pass_mark }}%</b>{% if cfg.certificate %}, and passing earns a printable certificate{% endif %}.</span></li>
    </ul>
    {% if cfg.note %}<div class="note">{{ cfg.note }}</div>{% endif %}
    <form method="POST" action="{{ url_for('begin_test') }}">
      <label class="check"><input type="checkbox" name="honour" required>
        <span><b>I will answer on my own</b><small>No notes, no phone, no help from anyone else.</small></span></label>
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
        rows = conn.execute("SELECT marks FROM questions").fetchall()
    return render_template_string(
        page(INSTRUCTIONS_TEMPLATE),
        student=session["student"],
        n=len(rows),
        total_marks=fmt_points(sum(q_marks(r) for r in rows)),
    )


@app.route("/begin", methods=["POST"])
def begin_test():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    if not request.form.get("honour"):
        flash("Tick the honour box to begin.", "error")
        return redirect(url_for("instructions"))
    if not session.get("deadline"):
        now = int(time.time())
        session["started_at"] = now
        session["deadline"] = now + cfg()["minutes"] * 60
    return redirect(url_for("test_page"))


# ----------------------------------------------------------------------
# STUDENT FLOW  3) the arena
# ----------------------------------------------------------------------
TEST_TEMPLATE = """
<div class="narrow">
<div class="hud">
  <div class="hud-row">
    <div class="hud-who">
      <div class="hud-name">{{ student.name }}</div>
      <div class="hud-meta">Class {{ student.student_class }} &middot; {{ student.semester }}</div>
    </div>
    <div class="hud-stats">
      <div class="chipstat"><b id="stat-ans">0</b><small>answered</small></div>
      <div class="chipstat"><b id="stat-flag">0</b><small>flagged</small></div>
      <div class="chipstat hot"><b id="stat-run">0</b><small>in a row</small></div>
      <div class="clock" id="clock" role="timer" aria-label="Time left">
        <svg viewBox="0 0 62 62" aria-hidden="true">
          <circle class="bgc" cx="31" cy="31" r="27"/>
          <circle class="fgc" id="clock-arc" cx="31" cy="31" r="27" stroke-dasharray="169.65" stroke-dashoffset="0"/>
        </svg>
        <b id="time-left">--:--</b>
      </div>
    </div>
  </div>
  <div class="track"><div class="bar" id="bar"></div></div>
  <div class="hud-count">
    <span><span id="answered">0</span> of {{ questions|length }} answered &middot; {{ total_marks }} marks</span>
    <span class="saved" id="saved">{{ icon('check') }}Saved on this device</span>
  </div>
</div>

<noscript><div class="flash error">Turn on JavaScript in your browser to take this test.</div></noscript>

<div class="drawer">
  <div class="drawer-top">
    <b>Question map</b>
    <div class="btn-row">
      <button class="btn btn-ghost btn-sm" type="button" id="keys-btn" aria-label="Keyboard shortcuts">{{ icon('help') }}Shortcuts</button>
      <button class="btn btn-ghost btn-sm" type="button" id="finish-btn">Review and submit</button>
    </div>
  </div>
  <div class="pal-grid">
    {% for q in questions %}<button type="button" class="pb" data-i="{{ loop.index0 }}" aria-label="Go to question {{ loop.index }}">{{ loop.index }}</button>{% endfor %}
  </div>
  <div class="legend">
    <span><i class="l-ans"></i>Answered</span>
    <span><i class="l-cur"></i>Current</span>
    <span><i class="l-flag"></i>Flagged</span>
  </div>
</div>

<form method="POST" action="{{ url_for('submit_test') }}" id="quiz-form" data-nobusy>
  <input type="hidden" name="focus_lost" id="focus-lost" value="0">
  {% for q in questions %}
  <section class="q" data-i="{{ loop.index0 }}">
    <div class="q-top">
      <div class="q-tags">
        <span class="tag">{{ q.topic }}</span>
        <span class="tag {{ q.difficulty }}">{{ q.difficulty }}</span>
        <span class="tag">{{ q.marks }} mark{{ '' if q.marks == '1' else 's' }}</span>
      </div>
      <div class="q-side">
        <span class="q-count">{{ loop.index }} / {{ questions|length }}</span>
        <button type="button" class="flag-toggle" aria-pressed="false" title="Flag for review (F)">
          {{ icon('flag') }}<span class="l-off">Flag</span><span class="l-on">Flagged</span>
        </button>
      </div>
    </div>
    <div class="q-text">{{ q.text|rich }}</div>
    {% for o in q.options %}
    <label class="opt"><input type="radio" name="q_{{ q.id }}" value="{{ o.key }}"><span class="letter">{{ o.letter }}</span><span class="opt-text">{{ o.text|rich }}</span></label>
    {% endfor %}
  </section>
  {% endfor %}

  <div class="qnav">
    <button class="btn btn-ghost prev" type="button" id="prev-btn">{{ icon('arrow-left') }}Previous</button>
    <button class="btn next" type="button" id="next-btn">Next</button>
  </div>
  <p class="hint"><kbd>1</kbd>&ndash;<kbd>4</kbd> pick an option &middot; <kbd>&larr;</kbd> <kbd>&rarr;</kbd> move &middot; <kbd>F</kbd> flag &middot; <kbd>?</kbd> shortcuts</p>
</form>

<dialog class="dlg" id="summary">
  <h2>Ready to submit?</h2>
  <p class="muted" id="sum-text" style="margin:6px 0 0;"></p>
  <div class="dlg-sec" id="sum-un" hidden><b>Still blank, tap a number to go there</b><div class="jump" id="sum-un-list"></div></div>
  <div class="dlg-sec" id="sum-fl" hidden><b>Flagged for review</b><div class="jump" id="sum-fl-list"></div></div>
  <div class="btn-row">
    <button class="btn btn-ghost" type="button" id="keep-btn">Keep working</button>
    <button class="btn" type="button" id="confirm-btn">Submit test</button>
  </div>
</dialog>

<dialog class="dlg" id="keysheet">
  <h2>Keyboard shortcuts</h2>
  <div class="keys">
    <kbd>1</kbd><span>Pick option A (2, 3, 4 for B, C, D)</span>
    <kbd>&rarr;</kbd><span>Next question</span>
    <kbd>&larr;</kbd><span>Previous question</span>
    <kbd>F</kbd><span>Flag this question</span>
    <kbd>S</kbd><span>Open the submit summary</span>
    <kbd>Esc</kbd><span>Close this box</span>
  </div>
  <div class="btn-row"><button class="btn btn-ghost" type="button" id="keys-close">Close</button></div>
</dialog>
</div>

<script>
(function () {
  var TOTAL = {{ questions|length }};
  var INITIAL = {{ remaining }};
  var LIMIT = {{ limit_seconds }};
  var TRACK = {{ 'true' if cfg.track_focus else 'false' }};
  var MAXSW = {{ cfg.max_switches }};
  window.STP_SOUND = {{ 'true' if cfg.sounds else 'false' }};
  var endAt = Date.now() + INITIAL * 1000;
  var storeKey = 'stp_answers_{{ deadline }}';
  var CIRC = 169.65;

  var form = document.getElementById('quiz-form');
  var qs = [].slice.call(document.querySelectorAll('.q'));
  var pbs = [].slice.call(document.querySelectorAll('.pb'));
  var timeEl = document.getElementById('time-left');
  var clockEl = document.getElementById('clock');
  var arc = document.getElementById('clock-arc');
  var bar = document.getElementById('bar');
  var countEl = document.getElementById('answered');
  var savedEl = document.getElementById('saved');
  var statAns = document.getElementById('stat-ans');
  var statFlag = document.getElementById('stat-flag');
  var statRun = document.getElementById('stat-run');
  var prevBtn = document.getElementById('prev-btn');
  var nextBtn = document.getElementById('next-btn');
  var dlg = document.getElementById('summary');
  var keysheet = document.getElementById('keysheet');
  var state = { cur: 0, flags: {}, lost: 0 };
  var sent = false, allow = false, warned5 = false, warned1 = false;

  function answered(i) { return !!qs[i].querySelector('input:checked'); }
  function counts() {
    var n = 0, run = 0, best = 0, f = 0;
    for (var i = 0; i < TOTAL; i++) {
      if (answered(i)) { n++; run++; if (run > best) best = run; } else { run = 0; }
      if (state.flags[i]) f++;
    }
    return { answered: n, streak: best, flags: f };
  }

  function save(ping) {
    try {
      var ans = {};
      [].forEach.call(form.querySelectorAll('input[type=radio]:checked'), function (r) { ans[r.name] = r.value; });
      localStorage.setItem(storeKey, JSON.stringify({ cur: state.cur, flags: state.flags, lost: state.lost, ans: ans }));
      if (ping) {
        savedEl.classList.remove('flash-save');
        void savedEl.offsetWidth;
        savedEl.classList.add('flash-save');
      }
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
    var c = counts();
    countEl.textContent = c.answered;
    statAns.textContent = c.answered;
    statFlag.textContent = c.flags;
    statRun.textContent = c.streak;
    bar.style.width = (TOTAL ? (c.answered / TOTAL) * 100 : 0) + '%';
    prevBtn.disabled = state.cur === 0;
    nextBtn.textContent = state.cur === TOTAL - 1 ? 'Review and submit' : 'Next';
    qs.forEach(function (q, i) {
      var fb = q.querySelector('.flag-toggle');
      if (fb) fb.setAttribute('aria-pressed', state.flags[i] ? 'true' : 'false');
    });
  }

  function go(i, quiet) {
    var next = Math.min(Math.max(i, 0), TOTAL - 1);
    if (next !== state.cur && !quiet) window.beep('move');
    state.cur = next;
    render();
    save();
    window.scrollTo(0, 0);
  }

  function toggleFlag(i) {
    if (i < 0) return;
    if (state.flags[i]) delete state.flags[i]; else state.flags[i] = true;
    render(); save(true);
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
    window.beep('done');
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
    var frac = LIMIT ? left / LIMIT : 0;
    arc.style.strokeDashoffset = (CIRC * (1 - Math.max(0, Math.min(1, frac)))).toFixed(2);
    clockEl.classList.toggle('warn', left <= 300 && left > 60);
    clockEl.classList.toggle('low', left <= 60);
    if (!warned5 && left <= 300 && left > 60 && INITIAL > 300) { warned5 = true; window.beep('warn'); window.toast('5 minutes left', 'warn'); }
    if (!warned1 && left <= 60 && left > 0 && INITIAL > 60) { warned1 = true; window.beep('warn'); window.toast('1 minute left', 'warn'); }
    if (left === 0) { clearInterval(iv); if (dlg.open) dlg.close(); send(); }
  }

  prevBtn.addEventListener('click', function () { go(state.cur - 1); });
  nextBtn.addEventListener('click', function () {
    if (state.cur === TOTAL - 1) openSummary(); else go(state.cur + 1);
  });
  form.addEventListener('click', function (e) {
    var fb = e.target.closest && e.target.closest('.flag-toggle');
    if (fb) toggleFlag(qs.indexOf(fb.closest('.q')));
  });
  pbs.forEach(function (b) { b.addEventListener('click', function () { go(parseInt(b.dataset.i, 10)); }); });
  document.getElementById('finish-btn').addEventListener('click', openSummary);
  document.getElementById('keep-btn').addEventListener('click', function () { dlg.close(); });
  document.getElementById('confirm-btn').addEventListener('click', send);
  document.getElementById('keys-btn').addEventListener('click', function () { if (keysheet.showModal) keysheet.showModal(); });
  document.getElementById('keys-close').addEventListener('click', function () { keysheet.close(); });
  form.addEventListener('change', function () { window.beep('pick'); render(); save(true); });
  form.addEventListener('submit', function (e) { if (!allow) { e.preventDefault(); openSummary(); } });

  document.addEventListener('keydown', function (e) {
    if (dlg.open || keysheet.open || e.ctrlKey || e.metaKey || e.altKey) return;
    var k = e.key.toLowerCase();
    var idx = { '1': 0, '2': 1, '3': 2, '4': 3 }[k];
    if (idx !== undefined) {
      var radios = qs[state.cur].querySelectorAll('input[type=radio]');
      if (radios[idx]) { radios[idx].checked = true; radios[idx].dispatchEvent(new Event('change', { bubbles: true })); }
    } else if (e.key === 'ArrowRight') { go(state.cur + 1); }
    else if (e.key === 'ArrowLeft') { go(state.cur - 1); }
    else if (k === 'f') { toggleFlag(state.cur); }
    else if (k === 's') { openSummary(); }
    else if (k === '?') { if (keysheet.showModal) keysheet.showModal(); }
  });

  var wasHidden = false;
  document.addEventListener('visibilitychange', function () {
    if (!TRACK || sent) return;
    if (document.hidden) { wasHidden = true; state.lost++; save(); }
    else if (wasHidden) {
      wasHidden = false;
      if (MAXSW && state.lost >= MAXSW) {
        window.toast('You left the page ' + state.lost + ' times. Submitting now.', 'bad');
        setTimeout(send, 900);
        return;
      }
      var msg = 'You left the test page. That is recorded for your teacher.';
      if (MAXSW) msg += ' ' + (MAXSW - state.lost) + ' switch(es) left before auto-submit.';
      window.beep('warn');
      window.toast(msg, 'warn');
    }
  });
  ['copy', 'cut', 'contextmenu'].forEach(function (evt) {
    document.addEventListener(evt, function (e) { if (!sent) e.preventDefault(); });
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
        rows = ordered_questions(conn)
    if not rows:
        return redirect(url_for("home"))
    deadline = int(session["deadline"])
    started = int(session.get("started_at", deadline))
    return render_template_string(
        page(TEST_TEMPLATE),
        student=session["student"],
        questions=laid_out(rows),
        total_marks=fmt_points(sum(q_marks(r) for r in rows)),
        remaining=max(0, deadline - int(time.time())),
        limit_seconds=max(1, deadline - started),
        deadline=deadline,
    )


@app.route("/submit", methods=["POST"])
def submit_test():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    if not session.get("deadline"):
        return redirect(url_for("instructions"))

    settings = cfg()
    now = int(time.time())
    started = int(session.get("started_at", now))
    limit = max(0, int(session["deadline"]) - started)
    time_taken = max(0, min(now - started, limit))

    try:
        focus_lost = max(0, min(999, int(request.form.get("focus_lost", "0"))))
    except ValueError:
        focus_lost = 0
    if not settings["track_focus"]:
        focus_lost = 0

    with closing(get_db()) as conn:
        questions = ordered_questions(conn)
        answers = {}
        for q in questions:
            value = request.form.get(f"q_{q['id']}")
            if value in OPTION_KEYS:
                answers[str(q["id"])] = value

        outcome = evaluate(questions, answers, settings["negative"])
        student = session["student"]
        submitted_at = now_str()
        cur = conn.execute(
            "INSERT INTO results (student_name, student_class, semester, phone, score, total, "
            "submitted_at, time_taken, answers, focus_lost, points, max_points) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                student["name"],
                student["student_class"],
                student["semester"],
                student["phone"],
                outcome["score"],
                outcome["total"],
                submitted_at,
                time_taken,
                json.dumps(answers),
                focus_lost,
                outcome["points"],
                outcome["max_points"],
            ),
        )
        conn.commit()
        rid = cur.lastrowid

    session["answers"] = answers
    session["time_taken"] = time_taken
    session["submitted_at"] = submitted_at
    session["submitted"] = True
    session["result_id"] = rid
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

  <section class="card report-hero">
    <span class="pill no-print">Report card</span>
    <h1 style="margin-top:14px;">{{ headline }}</h1>
    <p class="lead" style="margin:0 auto;"><b style="color:var(--ink);">{{ student.name }}</b><br>Class {{ student.student_class }} &middot; {{ student.semester }}</p>
    <div class="ring" id="ring" data-pct="{{ o.pct }}">
      <svg viewBox="0 0 176 176" aria-hidden="true">
        <circle class="bgc" cx="88" cy="88" r="80"/>
        <circle class="fgc" id="ring-arc" cx="88" cy="88" r="80" stroke-dasharray="502.65" stroke-dashoffset="502.65"/>
      </svg>
      <div class="val"><span id="pct-val">0%</span><small>{{ o.points }} of {{ o.max_points }} marks</small></div>
    </div>
    <div class="grade">{{ icon('award') }}Grade {{ grade }}</div>
    <div>
      <span class="verdict {{ 'pass' if passed else 'fail' }}">{{ icon('check') if passed else icon('target') }}{{ 'Passed' if passed else 'Below the pass mark of ' ~ cfg.pass_mark ~ '%' }}</span>
    </div>
    <p class="muted" style="margin:4px 0 0;">{{ message }}</p>
    <div class="chips">
      <div class="chip ok"><b>{{ o.score }}</b><small>Correct</small></div>
      <div class="chip bad"><b>{{ o.wrong }}</b><small>Wrong</small></div>
      <div class="chip"><b>{{ o.skipped }}</b><small>Skipped</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
    </div>
    {% if cmp %}
    <div class="cmp">
      <div class="cmp-top"><b>How you compare</b><span class="muted">{{ cmp.n }} students so far</span></div>
      <div class="cmp-track" aria-hidden="true"><i class="avg" style="left:{{ cmp.avg }}%;"></i><i class="you" style="left:{{ o.pct }}%;"></i></div>
      <div class="cmp-legend"><span><i class="k you"></i>You {{ o.pct }}%</span><span><i class="k avg"></i>Class average {{ cmp.avg }}%</span></div>
      {% if cmp.higher_than > 0 %}<p class="muted" style="margin:10px 0 0;">You scored higher than {{ cmp.higher_than }}% of them.</p>{% endif %}
    </div>
    {% endif %}
    <div class="btn-row no-print" style="margin-top:24px;justify-content:center;">
      {% if cert_url %}<a class="btn" href="{{ cert_url }}">{{ icon('award') }}Get certificate</a>{% endif %}
      <button class="btn btn-ghost" type="button" onclick="window.print()">{{ icon('print') }}Save as PDF</button>
      {% if share_url %}<button class="btn btn-ghost" type="button" id="share-btn" data-url="{{ share_url }}">{{ icon('share') }}Share result</button>{% endif %}
    </div>
  </section>

  {% if o.topics|length > 1 %}
  <section class="card">
    <h2>Topic by topic</h2>
    <p class="muted" style="margin:0 0 6px;">Start your revision at the bottom of this list.</p>
    <div class="topics">
      {% for t in o.topics %}
      <div class="trow">
        <div class="tname">{{ t.name }} <span class="muted">&middot; {{ t.correct }}/{{ t.total }}</span></div>
        <div class="meter {{ t.level }}"><i data-w="{{ t.pct }}"></i></div>
        <div class="pc">{{ t.pct }}%</div>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  {% if show_answers %}
  <section class="card">
    <div class="rv-head">
      <h2>Answer review</h2>
      <div class="seg no-print" role="group" aria-label="Filter answers">
        <button type="button" class="on" data-filter="all">All</button>
        <button type="button" data-filter="review">To review</button>
      </div>
    </div>
    <div class="rv-list">
      {% for r in o.review %}
      <div class="rv" data-status="{{ r.status }}">
        <div class="rv-mark {{ r.status }}">{% if r.status == 'ok' %}&#10003;{% elif r.status == 'bad' %}&#10005;{% else %}&ndash;{% endif %}</div>
        <div>
          <div class="rv-meta">{{ r.topic }} &middot; {{ r.difficulty }} &middot; {{ r.marks }} mark{{ '' if r.marks == '1' else 's' }}</div>
          <div class="rv-q">{{ r.number }}. {{ r.question|rich }}</div>
          <div class="rv-a">Your answer: <b>{{ r.your|rich }}</b></div>
          {% if r.status != 'ok' %}<div class="rv-a">Correct answer: <b class="good">{{ r.correct|rich }}</b></div>{% endif %}
          {% if r.status != 'ok' and r.explanation %}<div class="rv-x">{{ r.explanation|rich }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  </section>
  {% else %}
  <section class="card">
    <h2>Answers are hidden</h2>
    <p class="muted" style="margin:0;">Your teacher has turned off the answer review for this test.</p>
  </section>
  {% endif %}

  <div class="btn-row no-print" style="margin-bottom:10px;">
    <a class="btn btn-ghost" href="{{ url_for('home') }}">Back to start</a>
    {% if cfg.leaderboard %}<a class="btn btn-ghost" href="{{ url_for('leaderboard') }}">{{ icon('trophy') }}Leaderboard</a>{% endif %}
  </div>
</div>

<script>
(function () {
  var ring = document.getElementById('ring');
  var arc = document.getElementById('ring-arc');
  var val = document.getElementById('pct-val');
  var target = parseInt(ring.dataset.pct, 10) || 0;
  var CIRC = 502.65;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var CELEBRATE = {{ 'true' if celebrate else 'false' }};

  function paint(p) {
    arc.style.strokeDashoffset = (CIRC * (1 - p / 100)).toFixed(2);
    val.textContent = Math.round(p) + '%';
  }
  function bars() {
    [].forEach.call(document.querySelectorAll('.meter i'), function (el) {
      el.style.width = (el.dataset.w || 0) + '%';
    });
  }
  function confetti() {
    var css = getComputedStyle(document.documentElement);
    var colors = ['g1', 'g2', 'g3', 'gold'].map(function (n) { return css.getPropertyValue('--' + n).trim(); });
    for (var i = 0; i < 70; i++) {
      var d = document.createElement('div');
      d.className = 'cf';
      d.style.left = Math.random() * 100 + 'vw';
      d.style.background = colors[i % colors.length];
      d.style.setProperty('--dx', (Math.random() * 180 - 90) + 'px');
      d.style.animationDuration = (2 + Math.random() * 1.6) + 's';
      d.style.animationDelay = (Math.random() * 0.5) + 's';
      document.body.appendChild(d);
      (function (el) { setTimeout(function () { el.remove(); }, 4600); })(d);
    }
  }

  if (reduce || !CELEBRATE) {
    paint(target);
    bars();
  } else {
    var t0 = null, dur = 1000;
    paint(0);
    var step = function (ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      paint(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
      else { bars(); if (target >= {{ cfg.pass_mark }}) confetti(); }
    };
    requestAnimationFrame(step);
    setTimeout(bars, 350);
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

  var share = document.getElementById('share-btn');
  if (share) share.addEventListener('click', function () {
    var url = share.dataset.url;
    if (navigator.share) { navigator.share({ title: 'My result', url: url }).catch(function () {}); }
    else if (navigator.clipboard) { navigator.clipboard.writeText(url).then(function () { window.toast('Result link copied'); }); }
    else { window.prompt('Copy this link', url); }
  });
})();
</script>
"""


def render_report(student, outcome, time_taken, submitted_at, rid, cmp_data, celebrate):
    settings = cfg()
    grade, headline, message = grade_for(outcome["pct"], student["name"].split()[0] if student["name"] else "")
    passed = outcome["pct"] >= settings["pass_mark"]
    key = sign_result(rid) if rid else None
    return render_template_string(
        page(REPORT_TEMPLATE),
        student=student,
        o=outcome,
        grade=grade,
        headline=headline,
        message=message,
        passed=passed,
        time_text=fmt_duration(time_taken),
        submitted_at=submitted_at,
        cmp=cmp_data,
        celebrate=celebrate,
        show_answers=settings["show_answers"],
        cert_url=url_for("certificate", rid=rid, k=key) if (settings["certificate"] and passed and key) else None,
        share_url=url_for("view_result", rid=rid, k=key, _external=True) if key else None,
        page_title="Report card",
    )


@app.route("/report")
def report():
    if "student" not in session or not session.get("submitted"):
        return redirect(url_for("home"))
    settings = cfg()
    with closing(get_db()) as conn:
        questions = ordered_questions(conn)
        outcome = evaluate(questions, session.get("answers", {}), settings["negative"])
        cmp_data = comparison(conn, outcome["pct"]) if settings["show_stats"] else None
    return render_report(
        session["student"],
        outcome,
        session.get("time_taken"),
        session.get("submitted_at", ""),
        session.get("result_id"),
        cmp_data,
        celebrate=True,
    )


@app.route("/result/<int:rid>")
def view_result(rid):
    """A student's own report card, opened again from a signed link."""
    if not check_result_key(rid, request.args.get("k", "")):
        abort(404)
    settings = cfg()
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM results WHERE id = ?", (rid,)).fetchone()
        if r is None:
            abort(404)
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
        try:
            answers = json.loads(r["answers"]) if r["answers"] else {}
        except ValueError:
            answers = {}
        outcome = evaluate(questions, answers, settings["negative"])
        cmp_data = comparison(conn, pct_of(r)) if settings["show_stats"] else None
    outcome["pct"] = pct_of(r)
    student = {"name": r["student_name"], "student_class": r["student_class"], "semester": r["semester"]}
    return render_report(student, outcome, r["time_taken"], r["submitted_at"], rid, cmp_data, celebrate=False)


CERT_TEMPLATE = """
<div class="narrow">
  <a class="back no-print" href="{{ back_url }}">{{ icon('arrow-left') }}Back to the report card</a>
  <div class="cert">
    <div class="cert-in">
      <div class="muted" style="font-size:.9rem;">{{ cfg.school_name }}</div>
      <h1>Certificate of achievement</h1>
      <p class="muted" style="margin:0;">This is presented to</p>
      <div class="name">{{ r.student_name }}</div>
      <p class="muted" style="max-width:46ch;margin:0 auto;">
        of class {{ r.student_class }} ({{ r.semester }}) for completing {{ cfg.test_title }}
        with a score of {{ pct }}% and grade {{ grade }}.
      </p>
      <div class="seal">{{ icon('award') }}</div>
      <div class="cert-meta">
        <div>Score<b>{{ r.score }}/{{ r.total }}</b></div>
        <div>Grade<b>{{ grade }}</b></div>
        <div>Date<b>{{ r.submitted_at[:10] }}</b></div>
      </div>
    </div>
  </div>
  <div class="btn-row no-print" style="margin-top:18px;justify-content:center;">
    <button class="btn" type="button" onclick="window.print()">{{ icon('print') }}Print or save as PDF</button>
  </div>
</div>
"""


@app.route("/certificate/<int:rid>")
def certificate(rid):
    if not cfg()["certificate"] or not check_result_key(rid, request.args.get("k", "")):
        abort(404)
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM results WHERE id = ?", (rid,)).fetchone()
    if r is None:
        abort(404)
    pct = pct_of(r)
    if pct < cfg()["pass_mark"]:
        abort(404)
    return render_template_string(
        page(CERT_TEMPLATE),
        r=r,
        pct=pct,
        grade=grade_for(pct)[0],
        back_url=url_for("view_result", rid=rid, k=sign_result(rid)),
        page_title="Certificate",
    )


# ----------------------------------------------------------------------
# STUDENT EXTRAS: leaderboard + result lookup
# ----------------------------------------------------------------------
LEADERBOARD_TEMPLATE = """
<div class="narrow">
  <h1>Leaderboard</h1>
  <p class="lead">Ranked by percentage, then by who finished quicker.</p>
  {% if rows %}
  <div class="podium">
    {% for l in rows[:3] %}
    <div class="pod {{ 'first' if loop.index == 1 }}">
      <div class="p-rank">{{ '1st' if loop.index == 1 else ('2nd' if loop.index == 2 else '3rd') }}</div>
      <div class="p-name">{{ l.name }}</div>
      <div class="p-sub">Class {{ l.student_class }} &middot; {{ l.time }}</div>
      <div class="p-score">{{ l.pct }}%</div>
    </div>
    {% endfor %}
  </div>
  <div class="board">
    {% for l in rows %}
    <div class="brow">
      <span class="rank r{{ l.rank }}">{{ l.rank }}</span>
      <span><span class="nm">{{ l.name }}</span><span class="cl">Class {{ l.student_class }}</span></span>
      <span class="sc">{{ l.pct }}%</span>
      <span class="tm">{{ l.time }}</span>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="card"><h2>Nobody has finished yet</h2><p class="muted" style="margin:0;">Be the first name on this board.</p></div>
  {% endif %}
  <div class="btn-row" style="margin-top:20px;"><a class="btn btn-ghost" href="{{ url_for('home') }}">Back to start</a></div>
</div>
"""


@app.route("/leaderboard")
def leaderboard():
    if not cfg()["leaderboard"]:
        abort(404)
    with closing(get_db()) as conn:
        rows = leaderboard_rows(conn, 25)
    return render_template_string(page(LEADERBOARD_TEMPLATE), rows=rows, page_title="Leaderboard")


LOOKUP_TEMPLATE = """
<div class="narrow" style="max-width:560px;">
  <section class="card">
    <h1>Find my result</h1>
    <p class="lead">Enter the name and phone number you used for the test.</p>
    <form method="POST" data-nobusy>
      <label class="field"><span>Full name</span><input type="text" name="name" required maxlength="60" value="{{ name or '' }}"></label>
      <label class="field"><span>Phone number</span><input type="tel" name="phone" required inputmode="numeric" pattern="[0-9]{10}" maxlength="10" value="{{ phone or '' }}"></label>
      <button class="btn btn-full" type="submit">{{ icon('search') }}Find my result</button>
    </form>
    {% if rows is not none %}
      <div style="margin-top:22px;">
        {% if rows %}
        <h2>Your attempts</h2>
        {% for r in rows %}
        <div class="mini-row">
          <span class="who">{{ r.submitted_at }}</span>
          <span class="sc">{{ r.pct }}%</span>
          <a class="btn btn-ghost btn-sm" href="{{ r.url }}">Open</a>
        </div>
        {% endfor %}
        {% else %}
        <p class="muted" style="margin:0;">No result found for that name and number. Check the spelling of the name.</p>
        {% endif %}
      </div>
    {% endif %}
  </section>
  <a class="small-link muted" href="{{ url_for('home') }}">Back to start</a>
</div>
"""


@app.route("/my-result", methods=["GET", "POST"])
def lookup():
    if not cfg()["lookup"]:
        abort(404)
    rows = None
    name = phone = ""
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())[:60]
        phone = request.form.get("phone", "").strip()
        rows = []
        if re.fullmatch(r"\d{10}", phone) and name:
            with closing(get_db()) as conn:
                found = conn.execute(
                    "SELECT * FROM results WHERE phone = ? AND lower(student_name) = lower(?) ORDER BY id DESC",
                    (phone, name),
                ).fetchall()
            rows = [
                {
                    "submitted_at": r["submitted_at"],
                    "pct": pct_of(r),
                    "url": url_for("view_result", rid=r["id"], k=sign_result(r["id"])),
                }
                for r in found
            ]
    return render_template_string(
        page(LOOKUP_TEMPLATE), rows=rows, name=name, phone=phone, page_title="Find my result"
    )


# ----------------------------------------------------------------------
# ADMIN
# ----------------------------------------------------------------------
def admin_required():
    return session.get("is_admin", False)


LOGIN_TEMPLATE = """
<div class="narrow" style="max-width:430px;">
  <section class="card">
    <h1>Teacher login</h1>
    <p class="lead">Manage the paper, the settings and every result.</p>
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
    return render_template_string(page(LOGIN_TEMPLATE), page_title="Teacher login")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


def build_dashboard_data(conn):
    questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    rows = conn.execute("SELECT * FROM results ORDER BY id DESC").fetchall()

    band_counts = {"A+": 0, "A": 0, "B": 0, "C": 0, "D": 0}
    bins = [0] * 10
    pcts, times = [], []
    per_q = {q["id"]: {"attempted": 0, "correct": 0, "opts": {"a": 0, "b": 0, "c": 0, "d": 0}} for q in questions}
    topic_stats, diff_stats = {}, {}
    results, classes = [], set()
    passed = 0
    pass_mark = cfg()["pass_mark"]

    for r in rows:
        pct = pct_of(r)
        grade = grade_for(pct)[0]
        band_counts[grade] += 1
        bins[min(pct // 10, 9)] += 1
        pcts.append(pct)
        if pct >= pass_mark:
            passed += 1
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
                right = chosen == q["correct_option"]
                if right:
                    slot["correct"] += 1
                elif chosen in slot["opts"]:
                    slot["opts"][chosen] += 1
                for bucket, key in ((topic_stats, q_topic(q)), (diff_stats, q_difficulty(q))):
                    cell = bucket.setdefault(key, {"correct": 0, "total": 0})
                    cell["total"] += 1
                    cell["correct"] += 1 if right else 0
        if len(results) < 500:
            d = dict(r)
            d["pct"] = pct
            d["grade"] = grade
            d["time"] = fmt_clock(r["time_taken"]) or "-"
            d["points"] = fmt_points(r["points"]) if r["points"] is not None else d["score"]
            results.append(d)

    top = max(bins) if bins else 0
    hist = [
        {"label": str(i * 10), "count": n, "height": round(n / top * 100) if top else 0}
        for i, n in enumerate(bins)
    ]
    grades = [{"label": k, "count": v} for k, v in band_counts.items()]

    def summarise(bucket, order=None):
        out = []
        for name, cell in bucket.items():
            pct = round(cell["correct"] / cell["total"] * 100) if cell["total"] else 0
            out.append(
                {
                    "name": name,
                    "pct": pct,
                    "total": cell["total"],
                    "level": "good" if pct >= 70 else "mid" if pct >= 40 else "low",
                }
            )
        if order:
            out.sort(key=lambda x: order.index(x["name"]) if x["name"] in order else 99)
        else:
            out.sort(key=lambda x: x["pct"])
        return out

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
                wrong_note = f"Most picked wrong option: {letter.upper()} ({round(count / attempted * 100)}% of students)"
        items.append(
            {
                "number": i,
                "text": q["question_text"],
                "pct": pct,
                "level": level,
                "wrong_note": wrong_note,
                "topic": q_topic(q),
            }
        )
    hardest = sorted([x for x in items if x["pct"] is not None], key=lambda x: x["pct"])[:3]

    stats = {
        "count": len(rows),
        "avg": round(sum(pcts) / len(pcts)) if pcts else 0,
        "best": max(pcts) if pcts else 0,
        "avg_time": fmt_clock(sum(times) / len(times)) if times else "-",
        "pass_rate": round(passed / len(pcts) * 100) if pcts else 0,
        "marks": fmt_points(sum(q_marks(q) for q in questions)),
    }
    return {
        "questions": questions,
        "results": results,
        "stats": stats,
        "hist": hist,
        "grades": grades,
        "items": items,
        "hardest": hardest,
        "topics": summarise(topic_stats),
        "diffs": summarise(diff_stats, list(DIFFICULTIES)),
        "topic_names": sorted({q_topic(q) for q in questions}, key=str.lower),
        "classes": sorted(classes, key=str.lower),
    }


DASHBOARD_TEMPLATE = """
<div class="dash-top">
  <div>
    <h1>Dashboard</h1>
    <span class="pill {{ 'ok' if cfg.is_open else 'bad' }}"><span class="dot {{ 'live' if cfg.is_open }}"></span>{{ 'Open to students' if cfg.is_open else 'Closed to students' }}</span>
    <span class="pill">{{ questions|length }} questions &middot; {{ stats.marks }} marks &middot; {{ cfg.minutes }} min</span>
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
      <button class="nav-item" type="button" data-tab="questions">{{ icon('help') }}Question bank</button>
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
    <div class="stat"><div class="ico">{{ icon('award') }}</div><div><b>{{ stats.pass_rate }}%</b><span>Passed ({{ cfg.pass_mark }}% mark)</span></div></div>
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
      <h2>Weakest topics</h2>
      <p class="muted" style="margin:0 0 10px;">Where the class loses the most marks.</p>
      {% for t in topics %}
      <div class="irow">
        <div class="qt">{{ t.name }}</div>
        <div class="meter {{ t.level }}"><i style="width:{{ t.pct }}%;"></i></div>
        <div class="pc">{{ t.pct }}%</div>
      </div>
      {% else %}
      <p class="muted" style="margin:14px 0 0;">This fills in after the first submission.</p>
      {% endfor %}
      {% if diffs %}
      <div class="gchips">
        {% for d in diffs %}<span>{{ d.name }} <b>{{ d.pct }}%</b></span>{% endfor %}
      </div>
      {% endif %}
    </section>
  </div>

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

  <section class="card">
    <h2>Question by question</h2>
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

<!-- QUESTION BANK -->
<div class="panel" data-panel="questions">
  <div class="two">
    <section class="card">
      <h2>Add one question</h2>
      <p class="lead" style="margin-bottom:20px;">Four options, one correct answer.</p>
      <form method="POST" action="{{ url_for('add_question') }}">
        <label class="field"><span>Question <small>for code, start and end with ```</small></span>
          <textarea id="new-q" name="question_text" rows="5" required class="code-area" placeholder="Type the question"></textarea>
        </label>
        <button type="button" class="btn btn-ghost btn-sm" data-code-for="new-q" style="margin:-6px 0 16px;">Insert code block</button>
        <div class="grid2">
          <label class="field"><span>Option A</span><input type="text" name="option_a" required></label>
          <label class="field"><span>Option B</span><input type="text" name="option_b" required></label>
          <label class="field"><span>Option C</span><input type="text" name="option_c" required></label>
          <label class="field"><span>Option D</span><input type="text" name="option_d" required></label>
        </div>
        <div class="grid3">
          <label class="field"><span>Correct option</span>
            <select name="correct_option" required>
              <option value="a">A</option><option value="b">B</option><option value="c">C</option><option value="d">D</option>
            </select>
          </label>
          <label class="field"><span>Difficulty</span>
            <select name="difficulty"><option value="easy">Easy</option><option value="medium" selected>Medium</option><option value="hard">Hard</option></select>
          </label>
          <label class="field"><span>Marks</span>
            <input type="number" name="marks" min="0.5" max="100" step="0.5" value="1">
          </label>
        </div>
        <label class="field"><span>Topic <small>groups the analysis, e.g. Algebra</small></span>
          <input type="text" name="topic" maxlength="40" list="topic-list" placeholder="General">
        </label>
        <datalist id="topic-list">{% for t in topic_names %}<option value="{{ t }}"></option>{% endfor %}</datalist>
        <label class="field"><span>Explanation <small>optional, shown to students who miss it</small></span>
          <textarea name="explanation" rows="2" maxlength="400" placeholder="Why is this the right answer?"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add question</button>
      </form>
    </section>

    <section class="card">
      <h2>Paste many at once</h2>
      <p class="lead" style="margin-bottom:14px;">One question per line, parts separated by the | symbol. Everything after the correct letter is optional.</p>
      <code class="fmt">Question | A | B | C | D | b | Explanation | Topic | easy/medium/hard | 2</code>
      <form method="POST" action="{{ url_for('bulk_add') }}">
        <label class="field"><span>Questions</span>
          <textarea name="bulk" rows="11" required placeholder="What is 2 + 2? | 3 | 4 | 5 | 6 | b | Two and two make four. | Arithmetic | easy | 1&#10;Capital of India? | Mumbai | Delhi | Chennai | Kolkata | b"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add all questions</button>
      </form>
      <p class="muted" style="margin:14px 0 0;">This is also how you restore a backup file: open it, copy everything, paste it here. For code inside a bulk line, write \\n for a new line and \\p for a | symbol. For long code questions the "Add one question" form is easier.</p>
    </section>
  </div>

  <section class="card">
    <div class="tools">
      <div><h2>The paper ({{ questions|length }} questions, {{ stats.marks }} marks)</h2><span class="muted">Edits apply to students who start after you save.</span></div>
      <div class="filters">
        <select id="q-topic" aria-label="Filter by topic">
          <option value="">All topics</option>
          {% for t in topic_names %}<option value="{{ t|lower }}">{{ t }}</option>{% endfor %}
        </select>
        <input type="text" id="q-search" placeholder="Search questions" aria-label="Search questions">
        {% if questions %}<a class="btn btn-ghost btn-sm" href="{{ url_for('export_questions') }}">{{ icon('download') }}Backup</a>{% endif %}
      </div>
    </div>
    <div id="qbank">
    {% for q in questions %}
    <div class="qrow" data-topic="{{ (q.topic or 'General')|lower }}">
      <div>
        <div class="qrow-q"><b>{{ loop.index }}.</b> {{ q.question_text|rich }}</div>
        <div class="opts">
          {% for letter, key in [('A','option_a'),('B','option_b'),('C','option_c'),('D','option_d')] %}
            <span class="{{ 'right' if letter|lower == q.correct_option else '' }}">{{ letter }}. {{ q[key] }}</span>{% if not loop.last %} &nbsp; {% endif %}
          {% endfor %}
        </div>
        <div class="qtags">
          <span class="tag">{{ q.topic or 'General' }}</span>
          <span class="tag {{ q.difficulty or 'medium' }}">{{ q.difficulty or 'medium' }}</span>
          <span class="tag">{{ fmt_points(q.marks or 1) }} marks</span>
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
    </div>
    {% if questions %}<p class="muted" style="margin:16px 0 0;">On Render's free plan the database can reset when the app redeploys. Keep the backup file safe.</p>{% endif %}
  </section>
</div>

<!-- RESULTS -->
<div class="panel" data-panel="results">
  <section class="card">
    <div class="tools">
      <div>
        <h2>Results</h2>
        <span class="muted">Showing {{ results|length }} of {{ stats.count }}. Click a column to sort, or a name to open the answer sheet.</span>
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
          <th data-sort="num">Marks</th><th data-sort="num">%</th><th data-sort="text">Grade</th>
          <th data-sort="num">Time</th><th data-sort="num">Tab switches</th><th data-sort="text">Submitted</th><th></th>
        </tr></thead>
        <tbody>
        {% for r in results %}
        <tr>
          <td data-v="{{ r.student_name|lower }}"><a href="{{ url_for('result_detail', rid=r.id) }}">{{ r.student_name }}</a></td>
          <td data-v="{{ r.student_class|lower }}">{{ r.student_class }}</td>
          <td data-v="{{ r.semester|lower }}">{{ r.semester }}</td>
          <td data-v="{{ r.phone }}">{{ r.phone }}</td>
          <td data-v="{{ r.pct }}" class="tab-num">{{ r.score }}/{{ r.total }}</td>
          <td data-v="{{ r.pct }}" class="tab-num">{{ r.pct }}%</td>
          <td data-v="{{ r.grade }}">{{ r.grade }}</td>
          <td data-v="{{ r.time_taken if r.time_taken is not none else -1 }}" class="tab-num">{{ r.time }}</td>
          <td data-v="{{ r.focus_lost or 0 }}" class="tab-num {{ 'warn' if (r.focus_lost or 0) >= 3 }}">{{ r.focus_lost if r.focus_lost is not none else '-' }}</td>
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
  <form method="POST" action="{{ url_for('save_settings') }}">
  <div class="two">
    <section class="card">
      <h2>The paper</h2>
      <p class="lead" style="margin-bottom:20px;">These apply to the next student who starts.</p>
      <label class="field"><span>School or coaching name</span>
        <input type="text" name="school_name" maxlength="60" value="{{ cfg.school_name }}" required>
      </label>
      <label class="field"><span>Test title</span>
        <input type="text" name="test_title" maxlength="80" value="{{ cfg.test_title }}" required>
      </label>
      <div class="grid3">
        <label class="field"><span>Time limit <small>min</small></span>
          <input type="number" name="minutes" min="1" max="240" value="{{ cfg.minutes }}" required>
        </label>
        <label class="field"><span>Pass mark <small>%</small></span>
          <input type="number" name="pass_mark" min="0" max="100" value="{{ cfg.pass_mark }}" required>
        </label>
        <label class="field"><span>Negative <small>% per wrong</small></span>
          <input type="number" name="negative" min="0" max="100" value="{{ cfg.negative }}" required>
        </label>
      </div>
      <label class="field"><span>Message for students <small>optional, shown before the test</small></span>
        <textarea name="note" rows="3" maxlength="300" placeholder="Best of luck. No calculators.">{{ cfg.note }}</textarea>
      </label>
      <label class="check"><input type="checkbox" name="is_open" {{ 'checked' if cfg.is_open }}>
        <span><b>Test is open</b><small>Turn off to stop new students from starting.</small></span></label>
      <label class="check"><input type="checkbox" name="shuffle" {{ 'checked' if cfg.shuffle }}>
        <span><b>Shuffle the question order</b><small>Each student gets a different order.</small></span></label>
      <label class="check"><input type="checkbox" name="shuffle_options" {{ 'checked' if cfg.shuffle_options }}>
        <span><b>Shuffle the options too</b><small>A and C swap places, so answer keys cannot be passed around.</small></span></label>
      <label class="check"><input type="checkbox" name="allow_retake" {{ 'checked' if cfg.allow_retake }}>
        <span><b>Allow retakes</b><small>If off, the same name, class, semester and phone can submit only once.</small></span></label>
    </section>

    <section class="card">
      <h2>Proctoring and extras</h2>
      <p class="lead" style="margin-bottom:20px;">What students can see and what gets recorded.</p>
      <label class="check"><input type="checkbox" name="track_focus" {{ 'checked' if cfg.track_focus }}>
        <span><b>Record tab switches</b><small>Counts how often a student leaves the test page. Students are told about this.</small></span></label>
      <label class="field"><span>Auto-submit after <small>tab switches, 0 means never</small></span>
        <input type="number" name="max_switches" min="0" max="50" value="{{ cfg.max_switches }}">
      </label>
      <label class="check"><input type="checkbox" name="show_answers" {{ 'checked' if cfg.show_answers }}>
        <span><b>Show the answer review</b><small>Turn off if the same paper runs again later today.</small></span></label>
      <label class="check"><input type="checkbox" name="show_stats" {{ 'checked' if cfg.show_stats }}>
        <span><b>Show the class comparison</b><small>Report card shows the class average once 5 students have submitted.</small></span></label>
      <label class="check"><input type="checkbox" name="leaderboard" {{ 'checked' if cfg.leaderboard }}>
        <span><b>Public leaderboard</b><small>Top 25 by score, then by speed. Names and classes are visible.</small></span></label>
      <label class="check"><input type="checkbox" name="certificate" {{ 'checked' if cfg.certificate }}>
        <span><b>Certificate for students who pass</b><small>A printable certificate on the report card.</small></span></label>
      <label class="check"><input type="checkbox" name="lookup" {{ 'checked' if cfg.lookup }}>
        <span><b>Let students reopen their result</b><small>Using the name and phone number they entered.</small></span></label>
      <label class="check"><input type="checkbox" name="sounds" {{ 'checked' if cfg.sounds }}>
        <span><b>Sound cues during the test</b><small>Soft clicks when an answer is picked and when time runs low.</small></span></label>
      <button class="btn btn-full" type="submit">Save settings</button>
    </section>
  </div>
  </form>

  <section class="card danger-card">
    <h2>Delete data</h2>
    <p class="lead" style="margin-bottom:18px;">These cannot be undone. Download the results CSV and the question backup first.</p>
    <div class="btn-row">
      <form method="POST" action="{{ url_for('reset_results') }}" onsubmit="return confirm('All results will be deleted permanently. Continue?');">
        <button class="btn btn-danger" type="submit">Delete all results</button>
      </form>
      <form method="POST" action="{{ url_for('delete_all_questions') }}" onsubmit="return confirm('All questions will be deleted permanently. Continue?');">
        <button class="btn btn-danger" type="submit">Delete all questions</button>
      </form>
    </div>
  </section>
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

  var qSearch = document.getElementById('q-search');
  var qTopic = document.getElementById('q-topic');
  var bank = document.getElementById('qbank');
  function filterBank() {
    if (!bank) return;
    var q = (qSearch.value || '').toLowerCase();
    var t = qTopic.value;
    [].forEach.call(bank.querySelectorAll('.qrow'), function (row) {
      var okText = row.textContent.toLowerCase().indexOf(q) !== -1;
      var okTopic = !t || row.dataset.topic === t;
      row.hidden = !(okText && okTopic);
    });
  }
  if (qSearch) qSearch.addEventListener('input', filterBank);
  if (qTopic) qTopic.addEventListener('change', filterBank);

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


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        data = build_dashboard_data(conn)
    return render_template_string(
        page(DASHBOARD_TEMPLATE), wide=True, share_url=request.url_root, page_title="Dashboard", **data
    )


RESULT_TEMPLATE = """
<div class="narrow">
  <a class="back no-print" href="{{ url_for('admin_dashboard') }}#results">{{ icon('arrow-left') }}Back to results</a>
  <section class="card">
    <div class="who-strip">
      <div class="avatar">{{ r.student_name[0]|upper }}</div>
      <div>
        <div class="who-name">{{ r.student_name }}</div>
        <div class="muted">Class {{ r.student_class }} &middot; {{ r.semester }} &middot; {{ r.phone }}</div>
      </div>
    </div>
    <div class="chips" style="margin-top:0;">
      <div class="chip"><b>{{ r.score }}/{{ r.total }}</b><small>Correct</small></div>
      <div class="chip"><b>{{ pct }}%</b><small>Percentage</small></div>
      <div class="chip"><b>{{ grade }}</b><small>Grade</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
      <div class="chip {{ 'bad' if (r.focus_lost or 0) >= 3 }}"><b>{{ r.focus_lost if r.focus_lost is not none else '-' }}</b><small>Tab switches</small></div>
    </div>
    <p class="muted" style="margin:16px 0 0;">Submitted {{ r.submitted_at }}</p>
  </section>

  {% if outcome and outcome.topics|length > 1 %}
  <section class="card">
    <h2>Topic by topic</h2>
    <div class="topics">
      {% for t in outcome.topics %}
      <div class="trow">
        <div class="tname">{{ t.name }} <span class="muted">&middot; {{ t.correct }}/{{ t.total }}</span></div>
        <div class="meter {{ t.level }}"><i style="width:{{ t.pct }}%;"></i></div>
        <div class="pc">{{ t.pct }}%</div>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  <section class="card">
    <div class="rv-head"><h2>Answer sheet</h2>
      <button class="btn btn-ghost btn-sm no-print" type="button" onclick="window.print()">{{ icon('print') }}Print</button>
    </div>
    {% if outcome is none %}
      <p class="muted" style="margin:8px 0 0;">Detailed answers were not saved for this result.</p>
    {% else %}
    <div class="rv-list">
      {% for x in outcome.review %}
      <div class="rv">
        <div class="rv-mark {{ x.status }}">{% if x.status == 'ok' %}&#10003;{% elif x.status == 'bad' %}&#10005;{% else %}&ndash;{% endif %}</div>
        <div>
          <div class="rv-meta">{{ x.topic }} &middot; {{ x.difficulty }} &middot; {{ x.marks }} mark{{ '' if x.marks == '1' else 's' }}</div>
          <div class="rv-q">{{ x.number }}. {{ x.question|rich }}</div>
          <div class="rv-a">Student answered: <b>{{ x.your|rich }}</b></div>
          {% if x.status != 'ok' %}<div class="rv-a">Correct answer: <b class="good">{{ x.correct|rich }}</b></div>{% endif %}
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

    outcome = None
    if r["answers"]:
        try:
            outcome = evaluate(questions, json.loads(r["answers"]), cfg()["negative"])
        except ValueError:
            outcome = None
    pct = pct_of(r)
    return render_template_string(
        page(RESULT_TEMPLATE),
        r=r,
        pct=pct,
        grade=grade_for(pct)[0],
        time_text=fmt_duration(r["time_taken"]),
        outcome=outcome,
        page_title=r["student_name"],
    )


EDIT_TEMPLATE = """
<div class="narrow">
  <a class="back" href="{{ url_for('admin_dashboard') }}#questions">{{ icon('arrow-left') }}Back to the question bank</a>
  <section class="card">
    <h1>Edit question</h1>
    <p class="lead">Changes apply to students who start after you save.</p>
    <form method="POST">
      <label class="field"><span>Question <small>for code, start and end with ```</small></span>
        <textarea id="edit-q" name="question_text" rows="6" required class="code-area">{{ q.question_text }}</textarea>
      </label>
      <button type="button" class="btn btn-ghost btn-sm" data-code-for="edit-q" style="margin:-6px 0 16px;">Insert code block</button>
      <div class="grid2">
        <label class="field"><span>Option A</span><input type="text" name="option_a" required value="{{ q.option_a }}"></label>
        <label class="field"><span>Option B</span><input type="text" name="option_b" required value="{{ q.option_b }}"></label>
        <label class="field"><span>Option C</span><input type="text" name="option_c" required value="{{ q.option_c }}"></label>
        <label class="field"><span>Option D</span><input type="text" name="option_d" required value="{{ q.option_d }}"></label>
      </div>
      <div class="grid3">
        <label class="field"><span>Correct option</span>
          <select name="correct_option" required>
            {% for k in ['a','b','c','d'] %}<option value="{{ k }}" {{ 'selected' if q.correct_option == k }}>{{ k|upper }}</option>{% endfor %}
          </select>
        </label>
        <label class="field"><span>Difficulty</span>
          <select name="difficulty">
            {% for d in ['easy','medium','hard'] %}<option value="{{ d }}" {{ 'selected' if (q.difficulty or 'medium') == d }}>{{ d|capitalize }}</option>{% endfor %}
          </select>
        </label>
        <label class="field"><span>Marks</span>
          <input type="number" name="marks" min="0.5" max="100" step="0.5" value="{{ fmt_points(q.marks or 1) }}">
        </label>
      </div>
      <label class="field"><span>Topic</span>
        <input type="text" name="topic" maxlength="40" value="{{ q.topic or '' }}" placeholder="General">
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
    raw = request.form.get("question_text", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "\n".join(line.rstrip() for line in raw.split("\n"))[:4000]
    options = [" ".join(request.form.get(f"option_{k}", "").split()) for k in "abcd"]
    correct = request.form.get("correct_option", "")
    explanation = request.form.get("explanation", "").strip()[:400]
    topic = " ".join(request.form.get("topic", "").split())[:40] or "General"
    difficulty = request.form.get("difficulty", "medium").lower()
    if difficulty not in DIFFICULTIES:
        difficulty = "medium"
    try:
        marks = max(0.5, min(100.0, float(request.form.get("marks", "1"))))
    except ValueError:
        marks = 1.0
    ok = bool(text) and all(options) and correct in OPTION_KEYS
    return ok, (text, *options, correct, explanation, topic, difficulty, marks)


INSERT_Q = (
    "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option, "
    "explanation, topic, difficulty, marks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


@app.route("/admin/add_question", methods=["POST"])
def add_question():
    if not admin_required():
        return redirect(url_for("admin_login"))
    ok, values = read_question_form()
    if not ok:
        flash("Fill in the question, all four options and the correct answer.", "error")
        return back_to("questions")
    with closing(get_db()) as conn:
        conn.execute(INSERT_Q, values)
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
            ok, values = read_question_form()
            if not ok:
                flash("Fill in the question, all four options and the correct answer.", "error")
                return redirect(url_for("edit_question", qid=qid))
            conn.execute(
                "UPDATE questions SET question_text=?, option_a=?, option_b=?, option_c=?, option_d=?, "
                "correct_option=?, explanation=?, topic=?, difficulty=?, marks=? WHERE id=?",
                (*values, qid),
            )
            conn.commit()
            flash("Question updated.", "success")
            return back_to("questions")
    return render_template_string(page(EDIT_TEMPLATE), q=q, page_title="Edit question")


@app.route("/admin/bulk_add", methods=["POST"])
def bulk_add():
    if not admin_required():
        return redirect(url_for("admin_login"))

    rows, bad_lines = [], []
    for number, raw in enumerate(request.form.get("bulk", "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = [bulk_unescape(p.strip()) for p in line.split("|")]
        core, extra = parts[:6], parts[6:]
        if len(core) != 6 or not all(core) or core[5].lower() not in OPTION_KEYS:
            bad_lines.append(str(number))
            continue
        explanation = extra[0][:400] if len(extra) > 0 else ""
        topic = (extra[1][:40] if len(extra) > 1 else "") or "General"
        difficulty = (extra[2].lower() if len(extra) > 2 else "medium")
        if difficulty not in DIFFICULTIES:
            difficulty = "medium"
        try:
            marks = max(0.5, min(100.0, float(extra[3]))) if len(extra) > 3 and extra[3] else 1.0
        except ValueError:
            marks = 1.0
        rows.append((core[0], core[1], core[2], core[3], core[4], core[5].lower(), explanation, topic, difficulty, marks))

    if rows:
        with closing(get_db()) as conn:
            conn.executemany(INSERT_Q, rows)
            conn.commit()
        flash(f"{len(rows)} question(s) added.", "success")
    if bad_lines:
        flash(
            "Skipped line(s) " + ", ".join(bad_lines) + ". Each line needs a question, 4 options and the correct letter.",
            "error",
        )
    if not rows and not bad_lines:
        flash("Nothing to add. Paste at least one question.", "error")
    return back_to("questions")


@app.route("/admin/export_questions")
def export_questions():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

    clean = bulk_escape

    lines = []
    for q in questions:
        lines.append(
            " | ".join(
                [
                    clean(q["question_text"]),
                    clean(q["option_a"]),
                    clean(q["option_b"]),
                    clean(q["option_c"]),
                    clean(q["option_d"]),
                    q["correct_option"],
                    clean(q["explanation"]),
                    clean(q_topic(q)),
                    q_difficulty(q),
                    fmt_points(q_marks(q)),
                ]
            )
        )
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
    values = {
        "school_name": " ".join(request.form.get("school_name", "").split())[:60] or DEFAULT_SETTINGS["school_name"],
        "test_title": " ".join(request.form.get("test_title", "").split())[:80] or DEFAULT_SETTINGS["test_title"],
        "minutes": str(_as_int(request.form.get("minutes"), 15, 1, 240)),
        "pass_mark": str(_as_int(request.form.get("pass_mark"), 40, 0, 100)),
        "negative": str(_as_int(request.form.get("negative"), 0, 0, 100)),
        "max_switches": str(_as_int(request.form.get("max_switches"), 0, 0, 50)),
        "note": request.form.get("note", "").strip()[:300],
    }
    for key in BOOL_KEYS:
        values[key] = "1" if request.form.get(key) else "0"
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
            "Name", "Class", "Semester", "Phone", "Correct", "Questions", "Marks", "Out of",
            "Percentage", "Grade", "Time Taken (m:ss)", "Tab Switches", "Submitted At",
        ]
        + [f"Q{i}" for i in range(1, len(questions) + 1)]
    )
    for r in rows:
        pct = pct_of(r)
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
                fmt_points(r["points"]) if r["points"] is not None else r["score"],
                fmt_points(r["max_points"]) if r["max_points"] is not None else r["total"],
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
