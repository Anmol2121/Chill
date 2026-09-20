"""
Student Test Portal (v5) - single-file Flask app: SQLite + HTML + CSS + JavaScript.

STUDENT FLOW
    Details -> Instructions -> Timed test (server-saved, resumable) -> Report card
    -> Certificate + Leaderboard

ADMIN FLOW  (/admin)
    Overview | Questions (topics, difficulty, marks, import/export) | Results
    | Insights (topic + difficulty + distractor analysis) | Settings

What is new in v5
    * Weighted marks per question, optional negative marking, pass mark
    * Topics and difficulty levels, with topic-wise analysis for the teacher
    * Answers are saved on the server every few seconds, so a dead battery or a
      dropped connection no longer loses a paper - the student simply resumes
    * Option order can be shuffled per student, on top of question order
    * Practice mode that never touches the results table
    * XP, badges, streaks, a leaderboard and a printable certificate
    * Rebuilt interface: "answer sheet" visual language, dark/light, sound cues,
      keyboard-first navigation, full-screen focus mode, reduced-motion support

Optional environment variables (everything else lives in Admin > Settings):
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
import math
import os
import random
import re
import secrets
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
    jsonify,
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
LETTERS = ["a", "b", "c", "d"]
DIFFICULTIES = ["easy", "medium", "hard"]


# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _add_missing(conn, table, columns):
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


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
                topic TEXT DEFAULT '',
                difficulty TEXT DEFAULT 'medium',
                marks REAL DEFAULT 1,
                position INTEGER DEFAULT 0
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
                points REAL DEFAULT 0,
                max_points REAL DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                skipped_count INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0,
                xp INTEGER DEFAULT 0,
                badges TEXT DEFAULT '[]'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attempts (
                token TEXT PRIMARY KEY,
                student TEXT NOT NULL,
                qorder TEXT NOT NULL,
                opt_order TEXT NOT NULL,
                answers TEXT DEFAULT '{}',
                flags TEXT DEFAULT '[]',
                started_at INTEGER,
                deadline INTEGER,
                focus_lost INTEGER DEFAULT 0,
                mode TEXT DEFAULT 'exam',
                submitted INTEGER DEFAULT 0,
                created_at TEXT
            )
            """
        )
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

        # upgrade databases created by older versions
        _add_missing(
            conn,
            "questions",
            [
                ("explanation", "TEXT"),
                ("topic", "TEXT DEFAULT ''"),
                ("difficulty", "TEXT DEFAULT 'medium'"),
                ("marks", "REAL DEFAULT 1"),
                ("position", "INTEGER DEFAULT 0"),
            ],
        )
        _add_missing(
            conn,
            "results",
            [
                ("time_taken", "INTEGER"),
                ("answers", "TEXT"),
                ("focus_lost", "INTEGER"),
                ("points", "REAL DEFAULT 0"),
                ("max_points", "REAL DEFAULT 0"),
                ("wrong_count", "INTEGER DEFAULT 0"),
                ("skipped_count", "INTEGER DEFAULT 0"),
                ("best_streak", "INTEGER DEFAULT 0"),
                ("xp", "INTEGER DEFAULT 0"),
                ("badges", "TEXT DEFAULT '[]'"),
            ],
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_results_sub ON results(submitted_at)")
        conn.commit()


init_db()

DEFAULT_SETTINGS = {
    "school_name": "Student Test Portal",
    "test_title": "Class Test",
    "minutes": "15",
    "is_open": "1",
    "shuffle": "1",
    "shuffle_options": "0",
    "allow_retake": "0",
    "show_stats": "1",
    "track_focus": "1",
    "note": "",
    "negative": "0",
    "pass_pct": "40",
    "show_answers": "1",
    "leaderboard": "1",
    "certificate": "1",
    "practice": "1",
    "focus_mode": "0",
    "sound": "1",
    "accent": "violet",
}

BOOL_KEYS = (
    "is_open", "shuffle", "shuffle_options", "allow_retake", "show_stats",
    "track_focus", "show_answers", "leaderboard", "certificate", "practice",
    "focus_mode", "sound",
)


def load_settings(conn):
    raw = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        raw[row["key"]] = row["value"]

    def as_int(key, lo, hi, fallback):
        try:
            return max(lo, min(hi, int(float(raw[key]))))
        except (ValueError, TypeError):
            return fallback

    def as_float(key, lo, hi, fallback):
        try:
            return max(lo, min(hi, round(float(raw[key]), 2)))
        except (ValueError, TypeError):
            return fallback

    out = {
        "school_name": raw["school_name"],
        "test_title": raw["test_title"],
        "note": raw["note"],
        "minutes": as_int("minutes", 1, 300, 15),
        "negative": as_float("negative", 0, 5, 0.0),
        "pass_pct": as_int("pass_pct", 0, 100, 40),
        "accent": raw["accent"] if raw["accent"] in ACCENTS else "violet",
    }
    for key in BOOL_KEYS:
        out[key] = raw[key] == "1"
    return out


ACCENTS = {
    "violet": ("#5B5BF0", "#8E7CFF"),
    "teal": ("#0E9E8E", "#2ED3B7"),
    "sunset": ("#E0533D", "#FF9E5E"),
    "cobalt": ("#1F6FEB", "#58A6FF"),
    "plum": ("#9333EA", "#D946EF"),
}


def cfg():
    """Settings for the current request (loaded once)."""
    if "cfg" not in g:
        with closing(get_db()) as conn:
            g.cfg = load_settings(conn)
    return g.cfg


@app.context_processor
def inject_cfg():
    settings = cfg()
    return {"cfg": settings, "accent": ACCENTS[settings["accent"]]}


# ----------------------------------------------------------------------
# SHARED LAYOUT + DESIGN SYSTEM
# ----------------------------------------------------------------------
LAYOUT_TOP = """{% macro icon(name) %}<svg class="ic" aria-hidden="true"><use href="#i-{{ name }}"/></svg>{% endmacro %}<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#F6F5FB">
<title>{{ page_title or cfg.test_title }} &middot; {{ cfg.school_name }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700;12..96,800&family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
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
  --accent:{{ accent[0] }};
  --accent-2:{{ accent[1] }};
  --paper:#F6F5FB;
  --surface:#FFFFFF;
  --surface-2:#F1EFF9;
  --ink:#16132E;
  --ink-2:#565277;
  --ink-3:#8C88A8;
  --rule:#E4E1F0;
  --rule-soft:#EFEDF7;
  --accent-wash:color-mix(in srgb, var(--accent) 11%, #FFFFFF);
  --accent-line:color-mix(in srgb, var(--accent) 34%, #FFFFFF);
  --on-accent:#FFFFFF;
  --good:#0F9D6E;
  --good-wash:#E4F6EE;
  --bad:#DE3B4B;
  --bad-wash:#FDEAEC;
  --flag:#D08700;
  --flag-wash:#FDF2DA;
  --shadow-sm:0 1px 2px rgba(22,19,46,.05);
  --shadow:0 1px 2px rgba(22,19,46,.05), 0 18px 40px -26px rgba(22,19,46,.45);
  --glow:0 0 0 4px color-mix(in srgb, var(--accent) 16%, transparent);
  --r-xl:26px; --r-lg:18px; --r-md:12px; --r-sm:8px;
  --display:'Bricolage Grotesque','Segoe UI',system-ui,sans-serif;
  --sans:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
}
[data-theme=dark]{
  --paper:#0D0B1B;
  --surface:#151327;
  --surface-2:#1D1A33;
  --ink:#EFEDFA;
  --ink-2:#A8A3C6;
  --ink-3:#7A7599;
  --rule:#2A2645;
  --rule-soft:#221F3A;
  --accent-wash:color-mix(in srgb, var(--accent) 22%, #151327);
  --accent-line:color-mix(in srgb, var(--accent) 46%, #151327);
  --good:#3FD39B;
  --good-wash:#0F2E24;
  --bad:#FF7A85;
  --bad-wash:#33161D;
  --flag:#F0B429;
  --flag-wash:#332612;
  --shadow-sm:0 1px 2px rgba(0,0,0,.45);
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 22px 46px -28px rgba(0,0,0,.95);
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;scroll-padding-top:calc(74px + env(safe-area-inset-top,0px));}
body{
  margin:0;color:var(--ink);background:var(--paper);
  background-image:radial-gradient(1100px 520px at 88% -8%, color-mix(in srgb, var(--accent) 12%, transparent), transparent 62%),
                   radial-gradient(760px 420px at -6% 4%, color-mix(in srgb, var(--accent-2) 12%, transparent), transparent 60%);
  background-attachment:fixed;
  font-family:var(--sans);font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;
  padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px);
}
a{color:var(--accent);text-decoration:none;}
a:hover{text-decoration:underline;}
[hidden]{display:none !important;}
.ic{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:1.85;stroke-linecap:round;stroke-linejoin:round;}
::selection{background:var(--accent-wash);}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px;}

/* ---------- frame ---------- */
.topbar{
  position:sticky;top:0;z-index:40;border-bottom:1px solid var(--rule);
  background:color-mix(in srgb, var(--paper) 82%, transparent);
  backdrop-filter:saturate(1.6) blur(14px);-webkit-backdrop-filter:saturate(1.6) blur(14px);
  padding-top:env(safe-area-inset-top,0px);
}
.topbar-in{max-width:1080px;margin:0 auto;padding:11px 20px;display:flex;align-items:center;justify-content:space-between;gap:16px;}
.topbar-in.wide,.wrap.wide{max-width:1280px;}
.brand{display:flex;align-items:center;gap:11px;min-width:0;color:inherit;}
.brand:hover{text-decoration:none;}
.mark{
  flex:none;width:36px;height:36px;border-radius:12px;display:grid;place-items:center;
  background:linear-gradient(145deg,var(--accent),var(--accent-2));color:#fff;
  box-shadow:0 6px 16px -8px var(--accent);
}
.mark .ic{width:19px;height:19px;stroke-width:2.6;}
.brand-text{min-width:0;}
.brand-name{font-family:var(--display);font-weight:700;font-size:1.04rem;line-height:1.18;letter-spacing:-.018em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.brand-sub{font-size:.78rem;color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.topbar-right{display:flex;align-items:center;gap:8px;flex:none;}
.icon-btn{
  width:38px;height:38px;border-radius:11px;border:1px solid var(--rule);background:var(--surface);
  color:var(--ink-2);display:grid;place-items:center;cursor:pointer;padding:0;transition:color .15s,border-color .15s;
}
.icon-btn:hover{color:var(--ink);border-color:var(--ink-3);}
.icon-btn.off{color:var(--ink-3);}
[data-theme=light] .moon{display:block;} [data-theme=light] .sun{display:none;}
[data-theme=dark] .moon{display:none;} [data-theme=dark] .sun{display:block;}
.wrap{max-width:1080px;margin:0 auto;padding:34px 20px 72px;}
.narrow{max-width:720px;margin:0 auto;}
.foot{border-top:1px solid var(--rule);margin-top:12px;}
.foot-in{max-width:1080px;margin:0 auto;padding:22px 20px;display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;color:var(--ink-3);font-size:.84rem;}
.foot-in a{color:var(--ink-2);}

/* ---------- type ---------- */
h1{font-family:var(--display);font-size:2.1rem;line-height:1.1;margin:0 0 10px;font-weight:700;letter-spacing:-.03em;}
h2{font-family:var(--display);font-size:1.24rem;line-height:1.24;margin:0 0 4px;font-weight:600;letter-spacing:-.02em;}
h3{font-size:1rem;margin:0 0 4px;font-weight:600;}
.lead{color:var(--ink-2);margin:0 0 24px;max-width:62ch;}
.muted{color:var(--ink-2);font-size:.9rem;}
.tabular,.num{font-variant-numeric:tabular-nums;}

/* ---------- cards, pills ---------- */
.card{border:1px solid var(--rule);border-radius:var(--r-lg);padding:26px;margin-bottom:20px;background:var(--surface);box-shadow:var(--shadow-sm);}
.card.pad-lg{padding:32px;}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 13px;border-radius:999px;background:var(--accent-wash);color:var(--accent);font-weight:600;font-size:.82rem;border:1px solid var(--accent-line);}
.pill.ok{background:var(--good-wash);color:var(--good);border-color:transparent;}
.pill.bad{background:var(--bad-wash);color:var(--bad);border-color:transparent;}
.pill.flagish{background:var(--flag-wash);color:var(--flag);border-color:transparent;}
.pill .ic{width:14px;height:14px;}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none;}
.dot.live{animation:pulse 2s ease-out infinite;}
@keyframes pulse{0%{box-shadow:0 0 0 0 currentColor;opacity:1;}70%{box-shadow:0 0 0 7px transparent;}100%{box-shadow:0 0 0 0 transparent;}}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.73rem;font-weight:600;background:var(--surface-2);color:var(--ink-2);border:1px solid var(--rule);}
.tag.easy{color:var(--good);border-color:color-mix(in srgb,var(--good) 35%,transparent);background:var(--good-wash);}
.tag.medium{color:var(--flag);border-color:color-mix(in srgb,var(--flag) 35%,transparent);background:var(--flag-wash);}
.tag.hard{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 35%,transparent);background:var(--bad-wash);}

/* ---------- forms ---------- */
.field{display:block;margin-bottom:16px;}
.field > span{display:block;font-weight:600;font-size:.87rem;margin-bottom:6px;}
.field > span small{font-weight:400;color:var(--ink-2);}
input[type=text],input[type=tel],input[type=password],input[type=number],input[type=file],select,textarea{
  width:100%;padding:12px 14px;border:1px solid var(--rule);border-radius:var(--r-md);
  font:inherit;font-size:16px;color:var(--ink);background:var(--surface);outline:none;
  transition:border-color .15s, box-shadow .15s;
}
[data-theme=dark] input,[data-theme=dark] select,[data-theme=dark] textarea{background:var(--surface-2);}
textarea{resize:vertical;line-height:1.55;}
input::placeholder,textarea::placeholder{color:var(--ink-3);}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:var(--glow);outline:none;}
input[readonly]{background:var(--surface-2);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 14px;}
.check{display:flex;gap:12px;align-items:flex-start;margin-bottom:14px;cursor:pointer;}
.check input{width:20px;height:20px;margin-top:2px;accent-color:var(--accent);flex:none;}
.check b{display:block;font-size:.94rem;font-weight:600;}
.check small{color:var(--ink-2);}
.pw{position:relative;}
.pw input{padding-right:48px;}
.pw button{position:absolute;right:5px;top:5px;width:38px;height:38px;border:none;background:none;color:var(--ink-3);cursor:pointer;border-radius:9px;display:grid;place-items:center;}
.pw button:hover{background:var(--surface-2);color:var(--ink);}
.swatches{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:16px;}
.swatches label{cursor:pointer;}
.swatches input{position:absolute;opacity:0;}
.sw{display:block;width:34px;height:34px;border-radius:50%;border:2px solid var(--rule);}
.swatches input:checked + .sw{border-color:var(--ink);box-shadow:0 0 0 3px var(--surface),0 0 0 4px var(--ink);}

/* ---------- buttons ---------- */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:12px 20px;
  border-radius:var(--r-md);border:1px solid transparent;font:inherit;font-weight:600;cursor:pointer;
  background:linear-gradient(145deg,var(--accent),var(--accent-2));color:var(--on-accent);
  text-decoration:none;transition:filter .15s, transform .08s, box-shadow .15s;
  box-shadow:0 8px 18px -12px var(--accent);
}
.btn:hover{filter:brightness(1.07);text-decoration:none;color:var(--on-accent);}
.btn:active{transform:translateY(1px);}
.btn .ic{width:17px;height:17px;}
.btn-full{width:100%;margin-top:4px;}
.btn-ghost{background:var(--surface);color:var(--ink);border-color:var(--rule);box-shadow:none;}
.btn-ghost:hover{background:var(--surface-2);border-color:var(--ink-3);color:var(--ink);filter:none;}
.btn-ghost.on{background:var(--flag-wash);border-color:var(--flag);color:var(--flag);}
.btn-danger{background:var(--surface);color:var(--bad);border-color:var(--rule);box-shadow:none;}
.btn-danger:hover{background:var(--bad-wash);border-color:var(--bad);color:var(--bad);filter:none;}
.btn-sm{padding:8px 13px;font-size:.87rem;border-radius:var(--r-sm);}
.btn-lg{padding:15px 26px;font-size:1.02rem;border-radius:14px;}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn.busy{position:relative;color:transparent !important;pointer-events:none;}
.btn.busy .ic{opacity:0;}
.btn.busy::after{content:"";position:absolute;left:50%;top:50%;width:17px;height:17px;margin:-8.5px 0 0 -8.5px;border-radius:50%;border:2.5px solid var(--on-accent);border-right-color:transparent;animation:spin .7s linear infinite;}
.btn-ghost.busy::after{border-color:var(--ink-2);border-right-color:transparent;}
.btn-danger.busy::after{border-color:var(--bad);border-right-color:transparent;}
@keyframes spin{to{transform:rotate(360deg);}}
.small-link{display:block;text-align:center;margin-top:14px;font-size:.88rem;}
.back{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:.88rem;margin-bottom:16px;color:var(--ink-2);}

/* ---------- flash + toasts ---------- */
.flash{display:flex;gap:10px;align-items:flex-start;padding:12px 15px;border-radius:var(--r-md);margin-bottom:18px;font-size:.93rem;background:var(--accent-wash);color:var(--ink);border-left:3px solid var(--accent);transition:opacity .4s;}
.flash.error{background:var(--bad-wash);color:var(--bad);border-left-color:var(--bad);}
.flash.success{background:var(--good-wash);color:var(--good);border-left-color:var(--good);}
.toasts{position:fixed;left:0;right:0;bottom:calc(20px + env(safe-area-inset-bottom,0px));display:flex;flex-direction:column;align-items:center;gap:8px;z-index:100;pointer-events:none;padding:0 16px;}
.toast{background:var(--ink);color:var(--paper);padding:11px 18px;border-radius:var(--r-md);font-weight:500;font-size:.92rem;box-shadow:var(--shadow);transition:opacity .3s, transform .3s;max-width:430px;text-align:center;}
.toast.warn{background:var(--flag);color:#221803;}
.toast.good{background:var(--good);color:#04231A;}
.toast.out{opacity:0;transform:translateY(8px);}

/* ---------- home ---------- */
.hero{display:grid;grid-template-columns:1fr;gap:36px;align-items:start;}
@media (min-width:920px){.hero{grid-template-columns:1.02fr .98fr;gap:60px;padding-top:10px;}}
.hero h1{font-size:3rem;margin:16px 0 14px;letter-spacing:-.035em;}
.hero .lead{font-size:1.06rem;}
.spec{margin:26px 0 0;border-top:1px solid var(--rule);}
.spec div{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:11px 0;border-bottom:1px solid var(--rule);}
.spec dt{color:var(--ink-2);font-size:.92rem;}
.spec dd{margin:0;font-weight:600;text-align:right;}
.steps{list-style:none;margin:28px 0 0;padding:0;counter-reset:s;}
.steps li{display:flex;gap:14px;position:relative;padding-bottom:18px;}
.steps li:last-child{padding-bottom:0;}
.steps li::after{content:"";position:absolute;left:14px;top:32px;bottom:0;width:1px;background:var(--rule);}
.steps li:last-child::after{display:none;}
.steps .n{counter-increment:s;flex:none;width:29px;height:29px;border-radius:50%;border:1px solid var(--rule);background:var(--surface);color:var(--ink-2);display:grid;place-items:center;font-size:.82rem;font-weight:600;}
.steps .n::before{content:counter(s);}
.steps b{display:block;font-weight:600;line-height:1.35;padding-top:3px;}
.steps small{color:var(--ink-2);}

/* the one bold element: an OMR answer sheet */
.sheet{position:relative;background:var(--surface);border:1px solid var(--rule);border-radius:var(--r-xl);box-shadow:var(--shadow);overflow:hidden;}
.sheet-top{padding:18px 26px 16px 48px;border-bottom:1px solid var(--rule);background:var(--surface-2);display:flex;justify-content:space-between;align-items:center;gap:12px;}
.sheet-top h2{margin:0;}
.sheet-top small{display:block;color:var(--ink-2);font-size:.83rem;}
.sheet-body{padding:24px 26px 26px 48px;position:relative;}
.sheet::before{content:"";position:absolute;top:0;bottom:0;left:32px;width:1px;background:var(--accent);opacity:.3;}
.holes{position:absolute;top:0;bottom:0;left:0;width:32px;display:flex;flex-direction:column;justify-content:space-evenly;align-items:center;padding:24px 0;}
.holes i{width:9px;height:9px;border-radius:50%;background:var(--paper);box-shadow:inset 0 1px 2px rgba(22,19,46,.28);}
.bubbles{display:flex;gap:7px;align-items:center;}
.bubbles i{width:15px;height:15px;border-radius:50%;border:1.5px solid var(--rule);}
.bubbles i.on{background:var(--accent);border-color:var(--accent);}
.empty{text-align:center;padding:12px 0 6px;}
.empty .ic{width:30px;height:30px;color:var(--ink-3);margin-bottom:8px;}
.mini-board{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:22px;}
.mini{border:1px solid var(--rule);border-radius:var(--r-md);padding:11px 12px;background:var(--surface);}
.mini b{display:block;font-family:var(--display);font-size:1.18rem;font-weight:700;line-height:1.25;}
.mini small{color:var(--ink-2);font-size:.75rem;}

/* ---------- instructions ---------- */
.who{display:flex;align-items:center;gap:14px;margin-bottom:22px;padding-bottom:20px;border-bottom:1px solid var(--rule);}
.avatar{width:48px;height:48px;border-radius:14px;background:linear-gradient(145deg,var(--accent),var(--accent-2));color:#fff;display:grid;place-items:center;font-family:var(--display);font-weight:700;font-size:1.25rem;flex:none;}
.who-name{font-weight:600;font-size:1.06rem;line-height:1.25;}
.rules{list-style:none;margin:18px 0 22px;padding:0;}
.rules li{display:flex;gap:12px;padding:9px 0;border-bottom:1px dashed var(--rule-soft);}
.rules li:last-child{border-bottom:none;}
.rules .ic{color:var(--accent);margin-top:2px;width:18px;height:18px;}
.note{background:var(--flag-wash);border-left:3px solid var(--flag);border-radius:var(--r-sm);padding:12px 15px;margin:0 0 20px;white-space:pre-line;color:var(--ink);font-size:.95rem;}

/* ---------- test ---------- */
.hud{position:sticky;top:0;z-index:30;margin:0 -20px 18px;padding:calc(11px + env(safe-area-inset-top,0px)) 20px 10px;background:color-mix(in srgb, var(--paper) 86%, transparent);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--rule);}
.hud-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.hud-name{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:44vw;}
.hud-meta{font-size:.79rem;color:var(--ink-2);}
.hud-right{display:flex;align-items:center;gap:8px;}
.timer{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-weight:700;font-size:1.04rem;white-space:nowrap;background:var(--surface);border:1px solid var(--rule);color:var(--ink);padding:7px 14px;border-radius:999px;}
.timer .ic{width:16px;height:16px;color:var(--ink-2);}
.timer.warn{background:var(--flag-wash);border-color:var(--flag);color:var(--flag);}
.timer.warn .ic{color:var(--flag);}
.timer.low{background:var(--bad-wash);border-color:var(--bad);color:var(--bad);animation:beat 1s ease-in-out infinite;}
.timer.low .ic{color:var(--bad);}
@keyframes beat{50%{transform:scale(1.04);}}
.track{height:5px;background:var(--rule);border-radius:999px;overflow:hidden;margin-top:11px;}
.bar{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent-2));border-radius:999px;transition:width .3s;}
.hud-count{display:flex;justify-content:space-between;font-size:.78rem;color:var(--ink-2);margin-top:6px;}
.hud-count .saved{display:inline-flex;align-items:center;gap:5px;}
.hud-count .saved .ic{width:13px;height:13px;color:var(--good);}
.streak{display:inline-flex;align-items:center;gap:5px;font-weight:600;color:var(--flag);}

.testgrid{display:grid;grid-template-columns:1fr;gap:18px;}
@media (min-width:1000px){.testgrid{grid-template-columns:minmax(0,1fr) 232px;align-items:start;}.map-wrap{position:sticky;top:132px;}}
.palette{border:1px solid var(--rule);border-radius:var(--r-lg);padding:15px 17px;background:var(--surface);}
.pal-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:13px;}
.pal-top b{font-size:.92rem;font-weight:600;}
.pal-grid{display:flex;flex-wrap:wrap;gap:7px;}
.pb{position:relative;width:37px;height:37px;border-radius:11px;border:1px solid var(--rule);background:var(--surface);font:inherit;font-weight:600;font-size:.87rem;color:var(--ink-3);cursor:pointer;transition:border-color .12s,transform .12s;}
.pb:hover{border-color:var(--ink-3);transform:translateY(-1px);}
.pb.answered{background:var(--accent-wash);border-color:var(--accent-line);color:var(--accent);}
.pb.current{background:var(--ink);border-color:var(--ink);color:var(--paper);}
.pb.flagged::after{content:"";position:absolute;top:-3px;right:-3px;width:11px;height:11px;border-radius:50%;background:var(--flag);border:2px solid var(--surface);}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:.77rem;color:var(--ink-2);margin-top:13px;}
.legend i{display:inline-block;width:11px;height:11px;border-radius:4px;margin-right:6px;vertical-align:-1px;border:1px solid var(--rule);}
.legend .l-ans{background:var(--accent-wash);border-color:var(--accent-line);}
.legend .l-cur{background:var(--ink);border-color:var(--ink);}
.legend .l-flag{background:var(--flag);border-color:var(--flag);}

.q{display:none;border:1px solid var(--rule);border-radius:var(--r-lg);padding:26px;background:var(--surface);box-shadow:var(--shadow-sm);}
.q.active{display:block;}
.q-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:16px;}
.q-tags{display:flex;gap:6px;flex-wrap:wrap;}
.q-head{display:flex;gap:14px;margin-bottom:20px;}
.q-num{flex:none;width:32px;height:32px;border-radius:10px;background:var(--ink);color:var(--paper);display:grid;place-items:center;font-weight:700;font-size:.87rem;}
.q-text{font-family:var(--display);font-weight:600;font-size:1.26rem;line-height:1.36;letter-spacing:-.015em;padding-top:2px;}
.opt{position:relative;display:flex;align-items:center;gap:13px;padding:13px 15px;border:1px solid var(--rule);border-radius:var(--r-md);margin-bottom:9px;cursor:pointer;transition:border-color .15s, background .15s, transform .08s;}
.opt:last-child{margin-bottom:0;}
.opt:hover{border-color:var(--ink-3);}
.opt:active{transform:scale(.995);}
.opt input{position:absolute;opacity:0;pointer-events:none;}
.letter{flex:none;width:29px;height:29px;border-radius:50%;background:var(--surface);border:1.5px solid var(--rule);display:grid;place-items:center;font-weight:700;font-size:.82rem;color:var(--ink-3);transition:background .15s, color .15s, border-color .15s;}
.opt:has(input:checked){border-color:var(--accent);background:var(--accent-wash);}
.opt input:checked + .letter{background:var(--accent);border-color:var(--accent);color:#fff;}
.opt:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:2px;}
.qnav{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:center;margin-top:16px;}
.qnav .prev{justify-self:start;}
.qnav .next{justify-self:end;}
.hint{text-align:center;color:var(--ink-3);font-size:.79rem;margin-top:14px;}
kbd{font-family:var(--mono);font-size:.74rem;border:1px solid var(--rule);border-bottom-width:2px;border-radius:5px;padding:1px 5px;background:var(--surface);color:var(--ink-2);}

dialog.dlg{border:1px solid var(--rule);border-radius:var(--r-lg);padding:26px;max-width:480px;width:calc(100% - 32px);color:var(--ink);background:var(--surface);font-family:inherit;box-shadow:var(--shadow);}
dialog.dlg::backdrop{background:rgba(10,8,22,.62);backdrop-filter:blur(3px);}
.dlg-sec{margin:16px 0 4px;}
.dlg-sec b{display:block;font-size:.85rem;margin-bottom:8px;font-weight:600;}
.jump{display:flex;flex-wrap:wrap;gap:6px;}
.jump button{min-width:34px;height:34px;padding:0 9px;border-radius:var(--r-sm);border:1px solid var(--rule);background:var(--surface);font:inherit;font-weight:600;font-size:.84rem;cursor:pointer;color:var(--ink);}
.jump button:hover{border-color:var(--accent);color:var(--accent);}
.dlg .btn-row{margin-top:22px;flex-wrap:nowrap;}
.dlg .btn-row .btn{flex:1;}
.shortcuts{display:grid;grid-template-columns:auto 1fr;gap:9px 16px;font-size:.9rem;align-items:center;}

/* ---------- report ---------- */
.hero-report{text-align:center;overflow:hidden;position:relative;}
.ring{--pct:0;width:168px;height:168px;border-radius:50%;margin:22px auto 18px;background:conic-gradient(var(--accent) calc(var(--pct) * 1%), var(--rule) 0);display:grid;place-items:center;}
.ring span{width:138px;height:138px;border-radius:50%;background:var(--surface);display:grid;place-items:center;font-family:var(--display);font-size:2.3rem;font-weight:700;color:var(--ink);letter-spacing:-.03em;}
.grade{display:inline-flex;align-items:center;gap:7px;padding:6px 18px;border-radius:999px;background:var(--ink);color:var(--paper);font-weight:700;font-size:.9rem;margin:10px 0 12px;font-family:var(--display);}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(104px,1fr));gap:10px;margin-top:24px;}
.chip{border:1px solid var(--rule);border-radius:var(--r-md);padding:13px 8px;background:var(--surface);}
.chip b{display:block;font-family:var(--display);font-size:1.32rem;font-weight:700;line-height:1.3;}
.chip small{color:var(--ink-2);font-size:.78rem;}
.chip.ok b{color:var(--good);} .chip.bad b{color:var(--bad);}
.xp{margin-top:26px;text-align:left;}
.xp-top{display:flex;justify-content:space-between;align-items:baseline;font-size:.9rem;margin-bottom:7px;}
.xp-top b{font-family:var(--display);font-size:1.05rem;}
.xp-track{height:10px;border-radius:999px;background:var(--surface-2);overflow:hidden;border:1px solid var(--rule);}
.xp-track i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent-2));transition:width 1.1s cubic-bezier(.2,.8,.2,1);}
.badges{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:22px;text-align:left;}
.badge{display:flex;gap:11px;align-items:center;border:1px solid var(--accent-line);background:var(--accent-wash);border-radius:var(--r-md);padding:11px 13px;}
.badge .ic{color:var(--accent);width:22px;height:22px;}
.badge b{display:block;font-size:.9rem;font-weight:700;line-height:1.25;}
.badge small{color:var(--ink-2);font-size:.77rem;}
.cmp{margin-top:26px;padding-top:22px;border-top:1px solid var(--rule);text-align:left;}
.cmp-top{display:flex;justify-content:space-between;align-items:baseline;}
.cmp-top b{font-weight:600;}
.cmp-track{position:relative;height:6px;background:var(--rule);border-radius:999px;margin:26px 9px 16px;}
.cmp-track i{position:absolute;top:50%;width:16px;height:16px;border-radius:50%;transform:translate(-50%,-50%);border:3px solid var(--surface);}
.cmp-track .you,.k.you{background:var(--accent);}
.cmp-track .avg,.k.avg{background:var(--flag);}
.cmp-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:.86rem;}
.cmp-legend .k{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px;}
.topics{display:flex;flex-direction:column;gap:12px;margin-top:6px;}
.topic-row{display:grid;grid-template-columns:1fr 66px;gap:10px 12px;align-items:center;}
.topic-row .nm{font-weight:600;font-size:.94rem;}
.topic-row .pc{text-align:right;font-weight:700;font-variant-numeric:tabular-nums;}
.topic-row .meter{grid-column:1 / -1;}
.rv-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px;}
.seg{display:inline-flex;border:1px solid var(--rule);border-radius:999px;padding:3px;background:var(--surface);}
.seg button{border:none;background:none;padding:6px 13px;border-radius:999px;font:inherit;font-weight:500;font-size:.84rem;color:var(--ink-2);cursor:pointer;}
.seg button.on{background:var(--ink);color:var(--paper);}
.rv{display:flex;gap:13px;padding:15px 0;border-bottom:1px solid var(--rule-soft);}
.rv-list .rv:last-child{border-bottom:none;}
.rv-mark{flex:none;width:27px;height:27px;border-radius:9px;display:grid;place-items:center;font-weight:700;font-size:.82rem;margin-top:2px;}
.rv-mark.ok{background:var(--good-wash);color:var(--good);}
.rv-mark.bad{background:var(--bad-wash);color:var(--bad);}
.rv-mark.skip{background:var(--surface-2);color:var(--ink-3);}
.rv-q{font-weight:600;}
.rv-a{font-size:.9rem;color:var(--ink-2);}
.rv-a b{color:var(--ink);font-weight:600;}
.rv-a .good{color:var(--good);}
.rv-x{margin-top:9px;padding:10px 13px;background:var(--surface-2);border-radius:var(--r-sm);font-size:.9rem;color:var(--ink-2);border-left:2px solid var(--accent-line);}
.print-only{display:none;}
.cf{position:fixed;top:-16px;width:8px;height:13px;border-radius:2px;pointer-events:none;z-index:60;animation:fall linear forwards;}
@keyframes fall{to{transform:translate(var(--dx),108vh) rotate(720deg);}}

/* ---------- certificate ---------- */
.cert{position:relative;background:var(--surface);border:1px solid var(--rule);border-radius:var(--r-lg);padding:46px 38px;text-align:center;box-shadow:var(--shadow);}
.cert::before{content:"";position:absolute;inset:14px;border:1.5px solid var(--accent-line);border-radius:12px;pointer-events:none;}
.cert-kicker{font-size:.85rem;color:var(--ink-2);}
.cert h1{font-size:2.3rem;margin:12px 0 6px;}
.cert .name{font-family:var(--display);font-size:2rem;font-weight:700;margin:20px 0 4px;letter-spacing:-.03em;}
.cert .rule{width:150px;height:2px;background:var(--accent);margin:12px auto 18px;opacity:.5;}
.cert .seal{width:86px;height:86px;margin:24px auto 0;}
.cert-meta{display:flex;justify-content:center;gap:26px;flex-wrap:wrap;margin-top:22px;color:var(--ink-2);font-size:.88rem;}

/* ---------- leaderboard ---------- */
.lb{display:flex;flex-direction:column;gap:2px;}
.lb-row{display:grid;grid-template-columns:44px 1fr auto;gap:12px;align-items:center;padding:12px 14px;border-radius:var(--r-md);}
.lb-row:nth-child(odd){background:var(--surface-2);}
.lb-row.me{background:var(--accent-wash);box-shadow:inset 0 0 0 1px var(--accent-line);}
.lb-rank{font-family:var(--display);font-weight:700;font-size:1.05rem;color:var(--ink-3);text-align:center;}
.lb-row:nth-child(1) .lb-rank,.lb-row:nth-child(2) .lb-rank,.lb-row:nth-child(3) .lb-rank{color:var(--accent);}
.lb-name{font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lb-sub{font-size:.79rem;color:var(--ink-2);font-weight:400;}
.lb-score{font-family:var(--mono);font-weight:700;}

/* ---------- admin ---------- */
.dash-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:24px;}
.dash-top h1{margin-bottom:8px;}
.admin{display:grid;grid-template-columns:206px minmax(0,1fr);gap:32px;align-items:start;}
.side{position:sticky;top:82px;}
.navlist{display:flex;flex-direction:column;gap:2px;}
.nav-item{display:flex;align-items:center;gap:11px;width:100%;padding:10px 13px;border:none;background:none;border-radius:var(--r-md);font:inherit;font-weight:500;color:var(--ink-2);cursor:pointer;text-align:left;}
.nav-item:hover{background:var(--surface-2);color:var(--ink);}
.nav-item.active{background:var(--ink);color:var(--paper);}
.panel{display:none;}
.panel.active{display:block;animation:fadein .18s ease-out;}
@keyframes fadein{from{opacity:0;}to{opacity:1;}}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:12px;margin-bottom:20px;}
.stat{display:flex;gap:13px;align-items:center;border:1px solid var(--rule);border-radius:var(--r-lg);padding:15px 17px;background:var(--surface);}
.stat .ico{flex:none;width:40px;height:40px;border-radius:12px;background:var(--accent-wash);color:var(--accent);display:grid;place-items:center;}
.stat b{display:block;font-family:var(--display);font-size:1.55rem;font-weight:700;line-height:1.2;letter-spacing:-.02em;}
.stat span{font-size:.8rem;color:var(--ink-2);}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
.two > .card{margin-bottom:20px;}
.share{display:flex;gap:18px;justify-content:space-between;align-items:center;flex-wrap:wrap;}
.share-row{display:flex;gap:10px;flex-wrap:wrap;flex:1;min-width:280px;justify-content:flex-end;}
.share-row input{flex:1;min-width:200px;font-size:.9rem;}
.hist{display:flex;align-items:flex-end;gap:6px;height:156px;margin-top:16px;}
.hcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:.72rem;color:var(--ink-3);gap:4px;}
.hbar{width:100%;background:linear-gradient(180deg,var(--accent-2),var(--accent));border-radius:7px 7px 3px 3px;min-height:3px;}
.hbar.zero{background:var(--rule);}
.gchips{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;}
.gchips span{padding:5px 12px;border-radius:999px;border:1px solid var(--rule);font-size:.83rem;color:var(--ink-2);}
.gchips b{color:var(--ink);font-weight:700;}
.irow{display:grid;grid-template-columns:1fr 140px 46px;gap:14px;align-items:center;padding:11px 0;border-bottom:1px solid var(--rule-soft);font-size:.92rem;}
.irow:last-child{border-bottom:none;}
.irow .qt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.irow .sub{grid-column:1 / -1;margin-top:-6px;font-size:.8rem;color:var(--ink-3);}
.irow .pc{font-weight:700;text-align:right;font-variant-numeric:tabular-nums;}
.irow.two-col{grid-template-columns:1fr 46px;}
.meter{height:8px;background:var(--surface-2);border-radius:999px;overflow:hidden;border:1px solid var(--rule);}
.meter i{display:block;height:100%;background:var(--accent);border-radius:999px;}
.meter.good i{background:var(--good);} .meter.mid i{background:var(--flag);} .meter.low i{background:var(--bad);}
.qrow{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--rule-soft);}
.qrow:last-child{border-bottom:none;}
.qrow b{font-weight:600;}
.qrow .opts{font-size:.88rem;color:var(--ink-2);margin-top:5px;}
.qrow .opts .right{color:var(--good);font-weight:600;}
.qrow .meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
.qrow .acts{display:flex;gap:8px;flex:none;}
.tools{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start;margin-bottom:16px;}
.tools .filters{display:flex;gap:10px;flex-wrap:wrap;}
.tools input{width:220px;} .tools select{width:150px;}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;font-size:.91rem;min-width:820px;}
th{text-align:left;color:var(--ink-2);font-weight:500;font-size:.8rem;padding:8px 10px;border-bottom:1px solid var(--rule);white-space:nowrap;user-select:none;position:sticky;top:0;background:var(--surface);}
th[data-dir=asc]::after{content:" \\2191";}
th[data-dir=desc]::after{content:" \\2193";}
td{padding:11px 10px;border-bottom:1px solid var(--rule-soft);white-space:nowrap;}
td a{font-weight:600;}
td.warn{color:var(--flag);font-weight:600;}
code.fmt{display:block;background:var(--surface-2);border:1px solid var(--rule);border-radius:var(--r-sm);padding:10px 12px;font-family:var(--mono);font-size:.78rem;margin:0 0 14px;overflow-x:auto;white-space:nowrap;color:var(--ink-2);}
.danger-card{border-color:var(--bad);}
.spark{display:flex;align-items:flex-end;gap:3px;height:56px;margin-top:12px;}
.spark i{flex:1;background:var(--accent-line);border-radius:3px 3px 0 0;min-height:2px;}
.spark i.hot{background:var(--accent);}

@media (max-width:900px){
  .admin{grid-template-columns:1fr;gap:16px;}
  .side{position:static;}
  .navlist{flex-direction:row;overflow-x:auto;border-bottom:1px solid var(--rule);padding-bottom:8px;gap:6px;}
  .nav-item{width:auto;white-space:nowrap;}
  .two{grid-template-columns:1fr;}
}
@media (max-width:560px){
  .grid2,.grid3{grid-template-columns:1fr;}
  .card{padding:20px 17px;}
  h1{font-size:1.7rem;} .hero h1{font-size:2.2rem;}
  .sheet-top{padding:16px 18px 14px 40px;}
  .sheet-body{padding:20px 18px 22px 40px;}
  .btn-row .btn{width:100%;}
  .dlg .btn-row .btn{width:auto;}
  .qrow{flex-direction:column;}
  .hud-name{max-width:34vw;}
  .brand-sub{display:none;}
  .q{padding:20px 17px;}
  .qnav{grid-template-columns:1fr 1fr;}
  .qnav .mark{grid-column:1 / -1;order:-1;}
  .irow{grid-template-columns:1fr 46px;}
  .irow .meter{grid-column:1 / -1;order:3;}
  .tools input,.tools select{width:100%;}
  .tools .filters{width:100%;}
  .share-row .btn{flex:1;}
  .mini-board{grid-template-columns:1fr 1fr 1fr;gap:7px;}
  .cert{padding:32px 20px;}
}
@media print{
  body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .no-print{display:none !important;}
  .print-only{display:block;}
  .card,.cert{border-color:#ccc;box-shadow:none;break-inside:avoid;}
  .cf,.toasts{display:none;}
}
@media (prefers-reduced-motion:reduce){*{transition:none !important;animation:none !important;}}
</style>
<script>
window.toast = function (msg, kind) {
  var box = document.getElementById('toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts'; box.className = 'toasts';
    box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  var t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 300); }, 4200);
};

/* tiny sound engine - short blips, no files, off by default if muted */
window.Sound = (function () {
  var ctx = null, on = true;
  try { on = localStorage.getItem('stp_sound') !== '0'; } catch (e) {}
  function tone(freq, dur, type, vol) {
    if (!on) return;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') ctx.resume();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type || 'sine'; o.frequency.value = freq;
      g.gain.setValueAtTime(vol || 0.05, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + dur);
      o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + dur);
    } catch (e) {}
  }
  return {
    isOn: function () { return on; },
    set: function (v) { on = v; try { localStorage.setItem('stp_sound', v ? '1' : '0'); } catch (e) {} },
    pick: function () { tone(660, 0.07, 'triangle', 0.04); },
    move: function () { tone(420, 0.05, 'sine', 0.03); },
    flag: function () { tone(520, 0.09, 'square', 0.025); },
    warn: function () { tone(300, 0.22, 'sawtooth', 0.035); },
    done: function () { tone(523, 0.12, 'sine', 0.05); setTimeout(function () { tone(784, 0.22, 'sine', 0.05); }, 130); }
  };
})();

document.addEventListener('DOMContentLoaded', function () {
  var tt = document.getElementById('theme-btn');
  if (tt) tt.addEventListener('click', function () {
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('stp_theme', next); } catch (e) {}
  });
  var sb = document.getElementById('sound-btn');
  if (sb) {
    var paint = function () { sb.classList.toggle('off', !window.Sound.isOn()); sb.setAttribute('aria-pressed', window.Sound.isOn() ? 'true' : 'false'); };
    paint();
    sb.addEventListener('click', function () { window.Sound.set(!window.Sound.isOn()); paint(); if (window.Sound.isOn()) window.Sound.pick(); });
  }
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
  <symbol id="i-bolt" viewBox="0 0 24 24"><path d="M13 3L5 14h6l-1 7 8-11h-6z"/></symbol>
  <symbol id="i-fire" viewBox="0 0 24 24"><path d="M12 3s5 4.2 5 9a5 5 0 01-10 0c0-1.4.5-2.6 1.2-3.6C9 10.6 10.5 11 11 12c.7-2.2-.5-6.3 1-9z"/><path d="M12 21a6.5 6.5 0 006.5-6.5"/></symbol>
  <symbol id="i-medal" viewBox="0 0 24 24"><circle cx="12" cy="15" r="5.5"/><path d="M8.5 10L6 3h12l-2.5 7M12 13l.8 1.7 1.9.3-1.4 1.3.3 1.9-1.6-.9-1.6.9.3-1.9-1.4-1.3 1.9-.3z"/></symbol>
  <symbol id="i-volume" viewBox="0 0 24 24"><path d="M5 9v6h3.5L13 19V5L8.5 9z"/><path d="M16.5 9.5a3.5 3.5 0 010 5M19 7a7 7 0 010 10"/></symbol>
  <symbol id="i-expand" viewBox="0 0 24 24"><path d="M9 4H4v5M15 4h5v5M15 20h5v-5M9 20H4v-5"/></symbol>
  <symbol id="i-tag" viewBox="0 0 24 24"><path d="M11 3H4v7l10 10 7-7L11 3z"/><circle cx="7.5" cy="7.5" r="1.2"/></symbol>
  <symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 16V5M7 10l5-5 5 5M5 20h14"/></symbol>
  <symbol id="i-award" viewBox="0 0 24 24"><circle cx="12" cy="9" r="5.5"/><path d="M8.5 13.5L7 21l5-2.5L17 21l-1.5-7.5"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M7 4l12 8-12 8z"/></symbol>
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
      {% if cfg.sound %}
      <button class="icon-btn" type="button" id="sound-btn" aria-label="Turn sound on or off">{{ icon('volume') }}</button>
      {% endif %}
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
    value = round(float(value or 0), 2)
    return str(int(value)) if abs(value - int(value)) < 0.001 else f"{value:g}"


def question_marks(q):
    try:
        marks = float(q["marks"])
    except (TypeError, ValueError, IndexError):
        marks = 1.0
    return marks if marks > 0 else 1.0


def evaluate(questions, answers, negative=0.0, opt_order=None):
    """Score an answer map like {'12': 'b'} against a list of question rows."""
    points = 0.0
    max_points = 0.0
    correct = wrong = skipped = 0
    streak = best_streak = 0
    review = []
    for i, q in enumerate(questions, start=1):
        marks = question_marks(q)
        max_points += marks
        chosen = answers.get(str(q["id"]))
        right = q["correct_option"]
        if chosen not in OPTION_KEYS:
            status, streak = "skip", 0
            skipped += 1
        elif chosen == right:
            status = "ok"
            correct += 1
            points += marks
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            status, streak = "bad", 0
            wrong += 1
            points -= negative
        review.append(
            {
                "number": i,
                "id": q["id"],
                "question": q["question_text"],
                "your": q[OPTION_KEYS[chosen]] if chosen in OPTION_KEYS else "Not answered",
                "correct": q[OPTION_KEYS[right]],
                "explanation": q["explanation"] or "",
                "topic": (q["topic"] or "").strip() or "General",
                "difficulty": q["difficulty"] or "medium",
                "marks": marks,
                "status": status,
            }
        )
    return {
        "points": round(max(points, 0.0), 2),
        "raw_points": round(points, 2),
        "max_points": round(max_points, 2),
        "correct": correct,
        "wrong": wrong,
        "skipped": skipped,
        "best_streak": best_streak,
        "review": review,
    }


def grade_for(pct, first_name=""):
    who = f", {first_name}" if first_name else ""
    if pct >= 90:
        return "A+", f"Outstanding{who}", "You have this topic locked down. Keep the streak alive."
    if pct >= 75:
        return "A", f"Strong paper{who}", "Clean work. Fix the few misses below and this becomes a perfect score."
    if pct >= 60:
        return "B", f"Solid effort{who}", "You know most of it. The review below shows exactly where the marks went."
    if pct >= 40:
        return "C", f"Decent start{who}", "The base is there. Work through the explanations and try again."
    return "D", f"Time to rebuild{who}", "Start with the topic that scored lowest. One topic at a time beats cramming."


def topic_breakdown(review):
    buckets = {}
    for r in review:
        b = buckets.setdefault(r["topic"], {"topic": r["topic"], "n": 0, "ok": 0})
        b["n"] += 1
        if r["status"] == "ok":
            b["ok"] += 1
    rows = []
    for b in buckets.values():
        b["pct"] = round(b["ok"] / b["n"] * 100) if b["n"] else 0
        b["level"] = "good" if b["pct"] >= 70 else "mid" if b["pct"] >= 40 else "low"
        rows.append(b)
    return sorted(rows, key=lambda x: (-x["pct"], x["topic"]))


BADGE_LIBRARY = {
    "perfect": ("medal", "Clean sweep", "Every single question correct"),
    "sharp": ("target", "Sharpshooter", "90% or more of the marks"),
    "fast": ("bolt", "Quick thinker", "Finished in under half the time"),
    "focus": ("eye", "Never looked away", "Stayed on the test page the whole time"),
    "complete": ("check", "No blanks", "Answered every question"),
    "streak": ("fire", "On a roll", "Five or more correct in a row"),
    "topic": ("award", "Topic master", "100% in at least one topic"),
    "comeback": ("trophy", "Personal best", "Your best score on this test so far"),
}


def award_badges(res, pct, time_taken, limit_seconds, focus_lost, topics, previous_best):
    keys = []
    if res["correct"] == len(res["review"]) and res["review"]:
        keys.append("perfect")
    elif pct >= 90:
        keys.append("sharp")
    if limit_seconds and time_taken and time_taken <= limit_seconds * 0.5 and pct >= 60:
        keys.append("fast")
    if focus_lost == 0:
        keys.append("focus")
    if res["skipped"] == 0 and res["review"]:
        keys.append("complete")
    if res["best_streak"] >= 5:
        keys.append("streak")
    if any(t["pct"] == 100 and t["n"] >= 2 for t in topics):
        keys.append("topic")
    if previous_best is not None and pct > previous_best:
        keys.append("comeback")
    return [{"key": k, "icon": BADGE_LIBRARY[k][0], "title": BADGE_LIBRARY[k][1], "text": BADGE_LIBRARY[k][2]} for k in keys]


def xp_for(points, pct, best_streak, badges, time_left_ratio):
    xp = int(round(points * 10 + pct * 2 + best_streak * 5 + len(badges) * 25 + max(0.0, time_left_ratio) * 30))
    return max(xp, 0)


def level_for(xp):
    level = int(math.sqrt(max(xp, 0) / 120)) + 1
    floor_xp = ((level - 1) ** 2) * 120
    next_xp = (level ** 2) * 120
    span = max(next_xp - floor_xp, 1)
    return {
        "level": level,
        "into": xp - floor_xp,
        "span": span,
        "pct": round(min(100, (xp - floor_xp) / span * 100)),
        "next": next_xp - xp,
    }


def csv_safe(value):
    """Stop Excel from running text like '=SUM(...)' typed by a student as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def back_to(tab):
    return redirect(url_for("admin_dashboard") + "#" + tab)


def comparison(conn, pct):
    """Class average and percentile, shown once at least 5 students have submitted."""
    rows = conn.execute("SELECT score, total FROM results WHERE total > 0").fetchall()
    vals = [round(r["score"] / r["total"] * 100) for r in rows]
    if len(vals) < 5:
        return None
    below = sum(1 for v in vals if v < pct)
    return {"avg": round(sum(vals) / len(vals)), "higher_than": round(below / len(vals) * 100), "n": len(vals)}


# ---------- attempts (server-side answer storage) ----------
def new_attempt(conn, student, ids, opt_order, minutes, mode):
    token = secrets.token_urlsafe(18)
    start = int(time.time())
    conn.execute(
        "INSERT INTO attempts (token, student, qorder, opt_order, answers, flags, started_at, deadline, "
        "focus_lost, mode, submitted, created_at) VALUES (?,?,?,?,'{}','[]',?,?,0,?,0,?)",
        (token, json.dumps(student), json.dumps(ids), json.dumps(opt_order), start,
         start + minutes * 60, mode, now_str()),
    )
    conn.commit()
    return token


def get_attempt(conn=None):
    token = session.get("attempt")
    if not token:
        return None
    own = conn is None
    conn = conn or get_db()
    try:
        return conn.execute("SELECT * FROM attempts WHERE token = ?", (token,)).fetchone()
    finally:
        if own:
            conn.close()


def attempt_questions(conn, attempt):
    order = json.loads(attempt["qorder"])
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM questions ORDER BY id")}
    return [rows[qid] for qid in order if qid in rows]


def clear_old_attempts(conn):
    cutoff = int(time.time()) - 60 * 60 * 24 * 3
    conn.execute("DELETE FROM attempts WHERE deadline < ? AND submitted = 0", (cutoff,))


# ----------------------------------------------------------------------
# STUDENT FLOW  1) details
# ----------------------------------------------------------------------
HOME_TEMPLATE = """
<div class="hero">
  <section>
    <span class="pill {{ 'ok' if cfg.is_open and n else 'bad' }}"><span class="dot live"></span>{{ 'Open now' if cfg.is_open and n else 'Not accepting answers' }}</span>
    <h1>{{ cfg.test_title }}</h1>
    <p class="lead">
      {% if n %}Enter your details, read the rules, and the clock starts only when you press Begin. Your answers save to the server as you go, and your report card opens the second you submit.{% else %}Your teacher is still building this paper. Check back a little later.{% endif %}
    </p>

    {% if resume %}
    <div class="card" style="border-color:var(--accent);margin-bottom:22px;">
      <h2>You have a paper in progress</h2>
      <p class="muted" style="margin:6px 0 14px;">{{ resume.left }} left on the clock. Your saved answers are waiting.</p>
      <a class="btn" href="{{ url_for('test_page') }}">{{ icon('play') }}Resume the test</a>
    </div>
    {% endif %}

    {% if n %}
    <dl class="spec">
      <div><dt>Questions</dt><dd>{{ n }} multiple choice</dd></div>
      <div><dt>Total marks</dt><dd>{{ total_marks }}{% if cfg.negative %} &middot; minus {{ neg_text }} per wrong answer{% endif %}</dd></div>
      <div><dt>Time limit</dt><dd>{{ cfg.minutes }} minutes</dd></div>
      <div><dt>Pass mark</dt><dd>{{ cfg.pass_pct }}%</dd></div>
      <div><dt>Result</dt><dd>{{ 'Score, grade and full answer review' if cfg.show_answers else 'Score and grade, shown at once' }}</dd></div>
    </dl>
    {% endif %}

    <ol class="steps">
      <li><span class="n"></span><span><b>Enter your details</b><small>Name, class, semester and phone number</small></span></li>
      <li><span class="n"></span><span><b>Read the rules</b><small>Nothing starts until you press Begin</small></span></li>
      <li><span class="n"></span><span><b>Answer the questions</b><small>One at a time, with a map to jump around and flag doubts</small></span></li>
      <li><span class="n"></span><span><b>Collect your report card</b><small>Score, grade, XP, badges and a topic-wise breakdown</small></span></li>
    </ol>
  </section>

  <section>
    <div class="sheet">
      <div class="holes" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
      {% if not cfg.is_open %}
        <div class="sheet-top"><div><h2>Test closed</h2><small>No new attempts right now</small></div></div>
        <div class="sheet-body empty">
          {{ icon('lock') }}
          <p class="muted" style="margin:0;">Your teacher has paused this test. Ask them when it opens again.</p>
        </div>
      {% elif not n %}
        <div class="sheet-top"><div><h2>Questions on the way</h2><small>Nothing to answer yet</small></div></div>
        <div class="sheet-body empty">
          {{ icon('inbox') }}
          <p class="muted" style="margin:0;">This paper has no questions yet. Ask your teacher to add them.</p>
        </div>
      {% else %}
        <div class="sheet-top">
          <div><h2>Your details</h2><small>Printed on your report card</small></div>
          <div class="bubbles" aria-hidden="true"><i></i><i class="on"></i><i></i><i></i></div>
        </div>
        <div class="sheet-body">
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
            {% if cfg.practice %}
            <label class="check"><input type="checkbox" name="practice" value="1">
              <span><b>Practice run</b><small>Same paper, nothing saved to your teacher's results.</small></span></label>
            {% endif %}
            <button class="btn btn-full btn-lg" type="submit">Continue</button>
            <p class="muted" style="text-align:center;margin:12px 0 0;font-size:.82rem;">Only your teacher can see these details.</p>
          </form>
        </div>
      {% endif %}
    </div>

    {% if n %}
    <div class="mini-board">
      <div class="mini"><b>{{ topics }}</b><small>{{ 'Topic' if topics == 1 else 'Topics' }} covered</small></div>
      <div class="mini"><b>{{ attempts }}</b><small>Papers submitted</small></div>
      <div class="mini"><b>{{ best }}%</b><small>Best score so far</small></div>
    </div>
    {% endif %}
  </section>
</div>
"""


@app.route("/")
def home():
    settings = cfg()
    with closing(get_db()) as conn:
        clear_old_attempts(conn)
        conn.commit()
        rows = conn.execute("SELECT marks, topic FROM questions").fetchall()
        agg = conn.execute("SELECT COUNT(*) c, MAX(CASE WHEN total>0 THEN score*100.0/total END) b FROM results").fetchone()
        attempt = get_attempt(conn)

    resume = None
    if attempt and not attempt["submitted"]:
        left = int(attempt["deadline"]) - int(time.time())
        if left > 5:
            resume = {"left": fmt_duration(left)}
        else:
            session.pop("attempt", None)

    total_marks = sum(question_marks(r) for r in rows)
    topics = len({(r["topic"] or "").strip().lower() for r in rows if (r["topic"] or "").strip()}) or 1
    return render_template_string(
        page(HOME_TEMPLATE),
        n=len(rows),
        total_marks=fmt_points(total_marks),
        neg_text=fmt_points(settings["negative"]),
        topics=topics,
        attempts=agg["c"] or 0,
        best=round(agg["b"] or 0),
        resume=resume,
        page_title=settings["test_title"],
    )


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
    mode = "practice" if (request.form.get("practice") and settings["practice"]) else "exam"

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
        if mode == "exam" and not settings["allow_retake"]:
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
        opt_order = {}
        if settings["shuffle_options"]:
            for qid in ids:
                letters = LETTERS[:]
                random.shuffle(letters)
                opt_order[str(qid)] = letters

        student = {"name": name, "student_class": student_class, "semester": semester, "phone": phone}
        token = new_attempt(conn, student, ids, opt_order, settings["minutes"], mode)

    session.pop("result_id", None)
    session["attempt"] = token
    session["started"] = False
    return redirect(url_for("instructions"))


# ----------------------------------------------------------------------
# STUDENT FLOW  2) instructions
# ----------------------------------------------------------------------
INSTRUCTIONS_TEMPLATE = """
<div class="narrow">
  <section class="card pad-lg">
    <div class="who">
      <div class="avatar">{{ student.name[0]|upper }}</div>
      <div>
        <div class="who-name">{{ student.name }}</div>
        <div class="muted">Class {{ student.student_class }} &middot; {{ student.semester }}</div>
      </div>
      {% if mode == 'practice' %}<span class="pill flagish" style="margin-left:auto;">Practice run</span>{% endif %}
    </div>
    <h1>Before you begin</h1>
    <p class="lead" style="margin-bottom:0;">Read these once. The clock has not started yet.</p>
    <ul class="rules">
      <li>{{ icon('list') }}<span><b>{{ n }} question{{ '' if n == 1 else 's' }}</b> worth <b>{{ total_marks }} marks</b>{% if cfg.negative %}, and <b>{{ neg_text }} mark{{ '' if neg_text == '1' else 's' }}</b> is deducted for a wrong answer{% else %}, with no negative marking{% endif %}.</span></li>
      <li>{{ icon('clock') }}<span>You get <b>{{ cfg.minutes }} minutes</b>. The timer starts when you press Begin test.</span></li>
      <li>{{ icon('flag') }}<span>Move freely between questions, change answers, and flag anything you want to revisit.</span></li>
      <li>{{ icon('check') }}<span>Answers save to the server every few seconds. If your phone dies, open the link again and carry on.</span></li>
      <li>{{ icon('target') }}<span>The paper submits itself when time runs out. Pass mark is {{ cfg.pass_pct }}%.</span></li>
      {% if cfg.track_focus %}<li>{{ icon('eye') }}<span>Stay on this page. Every switch to another tab or app is counted and your teacher sees the count.</span></li>{% endif %}
      {% if mode == 'practice' %}<li>{{ icon('play') }}<span>This is a practice run, so nothing is saved to the results list or the leaderboard.</span></li>{% endif %}
    </ul>
    {% if cfg.note %}<div class="note">{{ cfg.note }}</div>{% endif %}
    <form method="POST" action="{{ url_for('begin_test') }}">
      <button class="btn btn-full btn-lg" type="submit">Begin test</button>
    </form>
    <a class="small-link muted" href="{{ url_for('home') }}">Change my details</a>
  </section>
</div>
"""


@app.route("/instructions")
def instructions():
    with closing(get_db()) as conn:
        attempt = get_attempt(conn)
        if attempt is None or attempt["submitted"]:
            return redirect(url_for("home"))
        if session.get("started"):
            return redirect(url_for("test_page"))
        rows = conn.execute("SELECT marks FROM questions").fetchall()
    settings = cfg()
    return render_template_string(
        page(INSTRUCTIONS_TEMPLATE),
        student=json.loads(attempt["student"]),
        mode=attempt["mode"],
        n=len(json.loads(attempt["qorder"])),
        total_marks=fmt_points(sum(question_marks(r) for r in rows)),
        neg_text=fmt_points(settings["negative"]),
        page_title="Instructions",
    )


@app.route("/begin", methods=["POST"])
def begin_test():
    with closing(get_db()) as conn:
        attempt = get_attempt(conn)
        if attempt is None or attempt["submitted"]:
            return redirect(url_for("home"))
        if not session.get("started"):
            start = int(time.time())
            conn.execute(
                "UPDATE attempts SET started_at = ?, deadline = ? WHERE token = ?",
                (start, start + cfg()["minutes"] * 60, attempt["token"]),
            )
            conn.commit()
            session["started"] = True
    return redirect(url_for("test_page"))


# ----------------------------------------------------------------------
# STUDENT FLOW  3) the test
# ----------------------------------------------------------------------
TEST_TEMPLATE = """
<div class="hud">
  <div class="hud-row">
    <div style="min-width:0;">
      <div class="hud-name">{{ student.name }}{% if mode == 'practice' %} &middot; practice{% endif %}</div>
      <div class="hud-meta">Class {{ student.student_class }} &middot; {{ student.semester }}</div>
    </div>
    <div class="hud-right">
      {% if cfg.focus_mode %}<button class="icon-btn no-print" type="button" id="fs-btn" aria-label="Full screen">{{ icon('expand') }}</button>{% endif %}
      <div id="timer" class="timer" role="timer" aria-live="off">{{ icon('clock') }}<span id="time-left">--:--</span></div>
    </div>
  </div>
  <div class="track"><div class="bar" id="bar"></div></div>
  <div class="hud-count">
    <span><span id="answered">0</span> of {{ questions|length }} answered</span>
    <span class="saved" id="save-state">{{ icon('check') }}<span>Saved</span></span>
  </div>
</div>

<noscript><div class="flash error">Turn on JavaScript in your browser to take this test.</div></noscript>

<div class="testgrid">
  <div>
    <form method="POST" action="{{ url_for('submit_test') }}" id="quiz-form" data-nobusy>
      <input type="hidden" name="focus_lost" id="focus-lost" value="0">
      {% for q in questions %}
      <section class="q" data-i="{{ loop.index0 }}" aria-label="Question {{ loop.index }}">
        <div class="q-top">
          <div class="q-tags">
            {% if q.topic %}<span class="tag">{{ q.topic }}</span>{% endif %}
            <span class="tag {{ q.difficulty }}">{{ q.difficulty }}</span>
            <span class="tag">{{ q.marks }} mark{{ '' if q.marks == '1' else 's' }}</span>
          </div>
          <span class="muted num">{{ loop.index }} / {{ questions|length }}</span>
        </div>
        <div class="q-head">
          <div class="q-num">{{ loop.index }}</div>
          <div class="q-text">{{ q.text }}</div>
        </div>
        {% for o in q.opts %}
        <label class="opt">
          <input type="radio" name="q_{{ q.id }}" value="{{ o.key }}">
          <span class="letter">{{ 'ABCD'[loop.index0] }}</span>
          <span>{{ o.text }}</span>
        </label>
        {% endfor %}
      </section>
      {% endfor %}

      <div class="qnav">
        <button class="btn btn-ghost prev" type="button" id="prev-btn">Previous</button>
        <button class="btn btn-ghost mark" type="button" id="mark-btn">{{ icon('flag') }}<span>Flag for review</span></button>
        <button class="btn next" type="button" id="next-btn">Next</button>
      </div>
      <p class="hint no-print"><kbd>1</kbd>-<kbd>4</kbd> pick an option &nbsp; <kbd>&larr;</kbd><kbd>&rarr;</kbd> move &nbsp; <kbd>F</kbd> flag &nbsp; <kbd>?</kbd> shortcuts</p>
    </form>
  </div>

  <div class="map-wrap">
    <div class="palette">
      <div class="pal-top">
        <b>Question map</b>
        <button class="btn btn-ghost btn-sm" type="button" id="finish-btn">Submit</button>
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
  </div>
</div>

<dialog class="dlg" id="summary">
  <h2>Ready to submit?</h2>
  <p class="muted" id="sum-text" style="margin:6px 0 0;"></p>
  <div class="dlg-sec" id="sum-un" hidden><b>Not answered yet, tap a number to go there</b><div class="jump" id="sum-un-list"></div></div>
  <div class="dlg-sec" id="sum-fl" hidden><b>Flagged for review</b><div class="jump" id="sum-fl-list"></div></div>
  <div class="btn-row">
    <button class="btn btn-ghost" type="button" id="keep-btn">Keep working</button>
    <button class="btn" type="button" id="confirm-btn">Submit test</button>
  </div>
</dialog>

<dialog class="dlg" id="keys">
  <h2>Keyboard shortcuts</h2>
  <div class="shortcuts" style="margin-top:14px;">
    <kbd>1</kbd><span>Choose option A (2, 3, 4 for B, C, D)</span>
    <kbd>&rarr;</kbd><span>Next question</span>
    <kbd>&larr;</kbd><span>Previous question</span>
    <kbd>F</kbd><span>Flag or unflag this question</span>
    <kbd>Enter</kbd><span>Open the submit summary</span>
    <kbd>Esc</kbd><span>Close a dialog</span>
  </div>
  <div class="btn-row"><button class="btn" type="button" id="keys-close">Got it</button></div>
</dialog>

<script>
(function () {
  var TOTAL = {{ questions|length }};
  var INITIAL = {{ remaining }};
  var TRACK = {{ 'true' if cfg.track_focus else 'false' }};
  var SAVE_URL = '{{ url_for("autosave") }}';
  var endAt = Date.now() + INITIAL * 1000;
  var storeKey = 'stp_answers_{{ token }}';

  var form = document.getElementById('quiz-form');
  var qs = [].slice.call(document.querySelectorAll('.q'));
  var pbs = [].slice.call(document.querySelectorAll('.pb'));
  var timeEl = document.getElementById('time-left');
  var timerEl = document.getElementById('timer');
  var bar = document.getElementById('bar');
  var countEl = document.getElementById('answered');
  var saveState = document.getElementById('save-state').querySelector('span');
  var prevBtn = document.getElementById('prev-btn');
  var nextBtn = document.getElementById('next-btn');
  var markBtn = document.getElementById('mark-btn');
  var markLabel = markBtn.querySelector('span');
  var dlg = document.getElementById('summary');
  var keysDlg = document.getElementById('keys');
  var state = { cur: 0, flags: {}, lost: {{ attempt_lost }} };
  var sent = false, allow = false, dirty = false;
  var warned5 = false, warned1 = false;

  var SAVED = {{ saved_answers|tojson }};
  var SAVED_FLAGS = {{ saved_flags|tojson }};

  function answered(i) { return !!qs[i].querySelector('input:checked'); }
  function answeredCount() { var n = 0; for (var i = 0; i < TOTAL; i++) if (answered(i)) n++; return n; }
  function collect() {
    var ans = {};
    [].forEach.call(form.querySelectorAll('input[type=radio]:checked'), function (r) { ans[r.name.slice(2)] = r.value; });
    return ans;
  }

  function localSave() {
    try {
      localStorage.setItem(storeKey, JSON.stringify({ cur: state.cur, flags: state.flags, lost: state.lost, ans: collect() }));
    } catch (e) {}
  }

  var saveTimer = null;
  function queueSave(now) {
    dirty = true;
    saveState.textContent = 'Saving';
    localSave();
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(push, now ? 0 : 1200);
  }
  function push() {
    if (sent) return;
    var body = JSON.stringify({ answers: collect(), flags: Object.keys(state.flags).map(Number), lost: state.lost, cur: state.cur });
    fetch(SAVE_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        dirty = false;
        saveState.textContent = 'Saved';
        if (d && typeof d.left === 'number') endAt = Date.now() + d.left * 1000;
        if (d && d.closed) { allow = true; form.submit(); }
      })
      .catch(function () { saveState.textContent = 'Saved on this device'; });
  }

  function restore() {
    var ans = SAVED || {};
    var flags = SAVED_FLAGS || [];
    try {
      var s = JSON.parse(localStorage.getItem(storeKey) || 'null');
      if (s) {
        Object.keys(s.ans || {}).forEach(function (k) { if (!(k in ans)) ans[k] = s.ans[k]; });
        flags = flags.concat(Object.keys(s.flags || {}).map(Number));
        state.cur = Math.min(Math.max(s.cur || 0, 0), TOTAL - 1);
        state.lost = Math.max(state.lost, s.lost || 0);
      }
    } catch (e) {}
    Object.keys(ans).forEach(function (qid) {
      var el = form.querySelector('input[name="q_' + qid + '"][value="' + ans[qid] + '"]');
      if (el) el.checked = true;
    });
    flags.forEach(function (i) { if (i >= 0 && i < TOTAL) state.flags[i] = true; });
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
    markLabel.textContent = on ? 'Flagged' : 'Flag for review';
    markBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }

  function go(i, quiet) {
    var next = Math.min(Math.max(i, 0), TOTAL - 1);
    if (next !== state.cur && !quiet) window.Sound.move();
    state.cur = next;
    render();
    queueSave();
    window.scrollTo({ top: 0, behavior: 'instant' in document.createElement('div').style ? 'auto' : 'auto' });
  }

  function makeJump(listId, wrapId, indexes) {
    var list = document.getElementById(listId), wrap = document.getElementById(wrapId);
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
    sent = true; allow = true;
    document.getElementById('focus-lost').value = state.lost;
    try { localStorage.removeItem(storeKey); } catch (e) {}
    window.Sound.done();
    var cb = document.getElementById('confirm-btn');
    cb.disabled = true; cb.textContent = 'Submitting';
    form.submit();
  }

  function tick() {
    var left = Math.max(0, Math.round((endAt - Date.now()) / 1000));
    timeEl.textContent = String(Math.floor(left / 60)).padStart(2, '0') + ':' + String(left % 60).padStart(2, '0');
    timerEl.classList.toggle('warn', left <= 300 && left > 60);
    timerEl.classList.toggle('low', left <= 60);
    if (!warned5 && left <= 300 && left > 60 && INITIAL > 300) { warned5 = true; window.Sound.warn(); window.toast('5 minutes left', 'warn'); }
    if (!warned1 && left <= 60 && left > 0 && INITIAL > 60) { warned1 = true; window.Sound.warn(); window.toast('1 minute left', 'warn'); }
    if (left === 0) { clearInterval(iv); if (dlg.open) dlg.close(); send(); }
  }

  prevBtn.addEventListener('click', function () { go(state.cur - 1); });
  nextBtn.addEventListener('click', function () { if (state.cur === TOTAL - 1) openSummary(); else go(state.cur + 1); });
  markBtn.addEventListener('click', function () {
    if (state.flags[state.cur]) delete state.flags[state.cur]; else state.flags[state.cur] = true;
    window.Sound.flag(); render(); queueSave();
  });
  pbs.forEach(function (b) { b.addEventListener('click', function () { go(parseInt(b.dataset.i, 10)); }); });
  document.getElementById('finish-btn').addEventListener('click', openSummary);
  document.getElementById('keep-btn').addEventListener('click', function () { dlg.close(); });
  document.getElementById('confirm-btn').addEventListener('click', send);
  document.getElementById('keys-close').addEventListener('click', function () { keysDlg.close(); });
  form.addEventListener('change', function () { window.Sound.pick(); render(); queueSave(true); });
  form.addEventListener('submit', function (e) { if (!allow) { e.preventDefault(); openSummary(); } });

  var fs = document.getElementById('fs-btn');
  if (fs) {
    fs.addEventListener('click', function () {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen();
    });
    document.addEventListener('fullscreenchange', function () {
      if (!document.fullscreenElement && !sent) window.toast('You left full screen. Tap the expand button to go back.', 'warn');
    });
  }

  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === '?' ) { e.preventDefault(); if (keysDlg.showModal && !keysDlg.open) keysDlg.showModal(); return; }
    if (dlg.open || keysDlg.open) return;
    var k = e.key.toLowerCase();
    var idx = { '1': 0, '2': 1, '3': 2, '4': 3 }[k];
    if (idx !== undefined) {
      var radios = qs[state.cur].querySelectorAll('input[type=radio]');
      if (radios[idx]) { radios[idx].checked = true; radios[idx].dispatchEvent(new Event('change', { bubbles: true })); }
    } else if (e.key === 'ArrowRight') { go(state.cur + 1); }
    else if (e.key === 'ArrowLeft') { go(state.cur - 1); }
    else if (k === 'f') { markBtn.click(); }
    else if (e.key === 'Enter' && e.target === document.body) { openSummary(); }
  });

  var wasHidden = false;
  document.addEventListener('visibilitychange', function () {
    if (!TRACK || sent) return;
    if (document.hidden) { wasHidden = true; state.lost++; queueSave(true); }
    else if (wasHidden) { wasHidden = false; window.toast('You left the test page. That is recorded for your teacher.', 'warn'); }
  });

  window.addEventListener('beforeunload', function (e) {
    if (!sent) { if (dirty) push(); e.preventDefault(); e.returnValue = ''; }
  });

  var iv = setInterval(tick, 500);
  setInterval(function () { if (!sent) push(); }, 15000);
  restore();
  render();
  tick();
})();
</script>
"""


@app.route("/test")
def test_page():
    settings = cfg()
    with closing(get_db()) as conn:
        attempt = get_attempt(conn)
        if attempt is None:
            return redirect(url_for("home"))
        if attempt["submitted"]:
            return redirect(url_for("report"))
        if not session.get("started"):
            return redirect(url_for("instructions"))
        rows = attempt_questions(conn, attempt)

    if not rows:
        return redirect(url_for("home"))

    opt_order = json.loads(attempt["opt_order"] or "{}")
    questions = []
    for q in rows:
        keys = opt_order.get(str(q["id"])) or LETTERS
        questions.append(
            {
                "id": q["id"],
                "text": q["question_text"],
                "topic": (q["topic"] or "").strip(),
                "difficulty": (q["difficulty"] or "medium"),
                "marks": fmt_points(question_marks(q)),
                "opts": [{"key": k, "text": q[OPTION_KEYS[k]]} for k in keys if k in OPTION_KEYS],
            }
        )

    remaining = max(0, int(attempt["deadline"]) - int(time.time()))
    return render_template_string(
        page(TEST_TEMPLATE),
        student=json.loads(attempt["student"]),
        mode=attempt["mode"],
        questions=questions,
        remaining=remaining,
        token=attempt["token"],
        saved_answers=json.loads(attempt["answers"] or "{}"),
        saved_flags=json.loads(attempt["flags"] or "[]"),
        attempt_lost=attempt["focus_lost"] or 0,
        page_title="Test in progress",
    )


@app.route("/api/autosave", methods=["POST"])
def autosave():
    data = request.get_json(silent=True) or {}
    with closing(get_db()) as conn:
        attempt = get_attempt(conn)
        if attempt is None or attempt["submitted"]:
            return jsonify({"ok": False}), 403

        answers = {}
        valid = {str(qid) for qid in json.loads(attempt["qorder"])}
        for qid, letter in (data.get("answers") or {}).items():
            if str(qid) in valid and letter in OPTION_KEYS:
                answers[str(qid)] = letter
        flags = [int(i) for i in (data.get("flags") or []) if isinstance(i, (int, float)) and 0 <= int(i) < len(valid)]
        try:
            lost = max(0, min(999, int(data.get("lost", 0))))
        except (TypeError, ValueError):
            lost = 0

        conn.execute(
            "UPDATE attempts SET answers = ?, flags = ?, focus_lost = ? WHERE token = ?",
            (json.dumps(answers), json.dumps(flags[:400]), lost, attempt["token"]),
        )
        conn.commit()
        left = max(0, int(attempt["deadline"]) - int(time.time()))
    return jsonify({"ok": True, "left": left, "closed": left == 0})


# ----------------------------------------------------------------------
# STUDENT FLOW  4) submit + report card
# ----------------------------------------------------------------------
@app.route("/submit", methods=["POST"])
def submit_test():
    settings = cfg()
    with closing(get_db()) as conn:
        attempt = get_attempt(conn)
        if attempt is None:
            return redirect(url_for("home"))
        if attempt["submitted"]:
            return redirect(url_for("report"))

        questions = attempt_questions(conn, attempt)
        stored = json.loads(attempt["answers"] or "{}")
        answers = dict(stored)
        posted = False
        for q in questions:
            value = request.form.get(f"q_{q['id']}")
            if value in OPTION_KEYS:
                answers[str(q["id"])] = value
                posted = True
        if not posted and not stored:
            answers = {}

        now = int(time.time())
        started = int(attempt["started_at"] or now)
        limit = max(0, int(attempt["deadline"]) - started)
        time_taken = max(0, min(now - started, limit))

        try:
            focus_lost = max(0, min(999, int(request.form.get("focus_lost", attempt["focus_lost"] or 0))))
        except ValueError:
            focus_lost = attempt["focus_lost"] or 0
        focus_lost = max(focus_lost, attempt["focus_lost"] or 0)
        if not settings["track_focus"]:
            focus_lost = 0

        res = evaluate(questions, answers, settings["negative"])
        pct = round(res["points"] / res["max_points"] * 100) if res["max_points"] else 0
        topics = topic_breakdown(res["review"])
        student = json.loads(attempt["student"])

        previous_best = None
        if attempt["mode"] == "exam":
            row = conn.execute(
                "SELECT MAX(CASE WHEN total > 0 THEN score * 100.0 / total END) b FROM results "
                "WHERE lower(student_name) = lower(?) AND phone = ?",
                (student["name"], student["phone"]),
            ).fetchone()
            previous_best = round(row["b"]) if row and row["b"] is not None else None

        badges = award_badges(res, pct, time_taken, limit, focus_lost, topics, previous_best)
        time_left_ratio = (limit - time_taken) / limit if limit else 0
        xp = xp_for(res["points"], pct, res["best_streak"], badges, time_left_ratio)
        submitted_at = now_str()

        result_id = None
        if attempt["mode"] == "exam":
            cur = conn.execute(
                "INSERT INTO results (student_name, student_class, semester, phone, score, total, submitted_at, "
                "time_taken, answers, focus_lost, points, max_points, wrong_count, skipped_count, best_streak, xp, badges) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    student["name"], student["student_class"], student["semester"], student["phone"],
                    res["correct"], len(questions), submitted_at, time_taken, json.dumps(answers), focus_lost,
                    res["points"], res["max_points"], res["wrong"], res["skipped"], res["best_streak"], xp,
                    json.dumps([b["key"] for b in badges]),
                ),
            )
            result_id = cur.lastrowid

        conn.execute(
            "UPDATE attempts SET submitted = 1, answers = ?, focus_lost = ? WHERE token = ?",
            (json.dumps(answers), focus_lost, attempt["token"]),
        )
        conn.commit()

    session["result_id"] = result_id
    session["outcome"] = {
        "time_taken": time_taken,
        "focus_lost": focus_lost,
        "xp": xp,
        "badges": [b["key"] for b in badges],
        "submitted_at": submitted_at,
        "mode": attempt["mode"],
        "previous_best": previous_best,
    }
    return redirect(url_for("report"))


REPORT_TEMPLATE = """
<div class="narrow">
  <div class="print-only" style="text-align:center;margin-bottom:16px;">
    <h2>{{ cfg.school_name }}</h2>
    <div class="muted">{{ cfg.test_title }} &middot; Report card &middot; {{ submitted_at }}</div>
  </div>

  <section class="card pad-lg hero-report">
    <span class="pill {{ 'flagish' if mode == 'practice' else ('ok' if passed else 'bad') }}">
      {{ 'Practice run, not saved' if mode == 'practice' else ('Passed' if passed else 'Below the pass mark') }}
    </span>
    <h1 style="margin-top:14px;">{{ headline }}</h1>
    <p class="lead" style="margin:0 auto;"><b style="color:var(--ink);">{{ student.name }}</b><br>Class {{ student.student_class }} &middot; {{ student.semester }}</p>
    <div class="ring" id="ring" data-pct="{{ pct }}" style="--pct:{{ pct }};"><span id="pct-val">{{ pct }}%</span></div>
    <h2>{{ points }} out of {{ max_points }} marks</h2>
    <div class="grade">Grade {{ grade }}</div>
    <p class="muted" style="margin:0;">{{ message }}</p>

    <div class="chips">
      <div class="chip ok"><b>{{ correct }}</b><small>Correct</small></div>
      <div class="chip bad"><b>{{ wrong }}</b><small>Wrong</small></div>
      <div class="chip"><b>{{ skipped }}</b><small>Skipped</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
      <div class="chip"><b>{{ best_streak }}</b><small>Best streak</small></div>
    </div>

    <div class="xp">
      <div class="xp-top"><span>Level {{ level.level }} &middot; {{ xp }} XP earned</span><b>+{{ xp }}</b></div>
      <div class="xp-track"><i id="xp-bar" data-pct="{{ level.pct }}"></i></div>
      <p class="muted" style="margin:7px 0 0;font-size:.82rem;">{{ level.next }} XP to level {{ level.level + 1 }}.</p>
    </div>

    {% if badges %}
    <div class="badges">
      {% for b in badges %}
      <div class="badge">{{ icon(b.icon) }}<div><b>{{ b.title }}</b><small>{{ b.text }}</small></div></div>
      {% endfor %}
    </div>
    {% endif %}

    {% if cmp %}
    <div class="cmp">
      <div class="cmp-top"><b>How you compare</b><span class="muted">{{ cmp.n }} students so far</span></div>
      <div class="cmp-track" aria-hidden="true"><i class="avg" style="left:{{ cmp.avg }}%;"></i><i class="you" style="left:{{ pct }}%;"></i></div>
      <div class="cmp-legend"><span><i class="k you"></i>You {{ pct }}%</span><span><i class="k avg"></i>Class average {{ cmp.avg }}%</span></div>
      {% if cmp.higher_than > 0 %}<p class="muted" style="margin:10px 0 0;">You scored higher than {{ cmp.higher_than }}% of them.</p>{% endif %}
    </div>
    {% endif %}
  </section>

  {% if topics|length > 1 %}
  <section class="card">
    <h2>Topic by topic</h2>
    <p class="muted" style="margin:0 0 16px;">Start your revision at the bottom of this list.</p>
    <div class="topics">
      {% for t in topics %}
      <div class="topic-row">
        <div class="nm">{{ t.topic }} <span class="muted" style="font-weight:400;">&middot; {{ t.ok }}/{{ t.n }}</span></div>
        <div class="pc">{{ t.pct }}%</div>
        <div class="meter {{ t.level }}"><i style="width:{{ t.pct }}%;"></i></div>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  {% if cfg.show_answers %}
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
  </section>
  {% endif %}

  <div class="btn-row no-print" style="margin-bottom:28px;">
    <button class="btn" type="button" onclick="window.print()">{{ icon('print') }}Print or save as PDF</button>
    {% if show_cert %}<a class="btn btn-ghost" href="{{ url_for('certificate') }}">{{ icon('award') }}Get certificate</a>{% endif %}
    {% if cfg.leaderboard and mode == 'exam' %}<a class="btn btn-ghost" href="{{ url_for('leaderboard') }}">{{ icon('trophy') }}Leaderboard</a>{% endif %}
    <a class="btn btn-ghost" href="{{ url_for('home') }}">Back to start</a>
  </div>
</div>

<script>
(function () {
  var ring = document.getElementById('ring');
  var val = document.getElementById('pct-val');
  var xpBar = document.getElementById('xp-bar');
  var target = parseInt(ring.dataset.pct, 10) || 0;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function confetti() {
    var css = getComputedStyle(document.documentElement);
    var colors = ['accent', 'accent-2', 'good', 'flag'].map(function (n) { return css.getPropertyValue('--' + n).trim(); });
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

  setTimeout(function () { if (xpBar) xpBar.style.width = (parseInt(xpBar.dataset.pct, 10) || 0) + '%'; }, 260);

  if (!reduce && target > 0) {
    var t0 = null, dur = 950;
    ring.style.setProperty('--pct', 0);
    val.textContent = '0%';
    var step = function (ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      var cur = Math.round(target * (1 - Math.pow(1 - p, 3)));
      ring.style.setProperty('--pct', cur);
      val.textContent = cur + '%';
      if (p < 1) requestAnimationFrame(step);
      else if (target >= 75) { confetti(); window.Sound.done(); }
    };
    requestAnimationFrame(step);
  }

  var btns = [].slice.call(document.querySelectorAll('.seg button'));
  btns.forEach(function (b) {
    b.addEventListener('click', function () {
      btns.forEach(function (x) { x.classList.toggle('on', x === b); });
      var only = b.dataset.filter === 'review';
      [].forEach.call(document.querySelectorAll('.rv'), function (r) { r.hidden = only && r.dataset.status === 'ok'; });
    });
  });
})();
</script>
"""


def report_data():
    """Rebuild everything the report card and certificate need from the finished attempt."""
    settings = cfg()
    outcome = session.get("outcome")
    with closing(get_db()) as conn:
        attempt = get_attempt(conn)
        if attempt is None or not attempt["submitted"] or not outcome:
            return None
        questions = attempt_questions(conn, attempt)
        res = evaluate(questions, json.loads(attempt["answers"] or "{}"), settings["negative"])
        pct = round(res["points"] / res["max_points"] * 100) if res["max_points"] else 0
        cmp_data = comparison(conn, pct) if settings["show_stats"] else None

    student = json.loads(attempt["student"])
    grade, headline, message = grade_for(pct, student["name"].split()[0])
    badges = [
        {"key": k, "icon": BADGE_LIBRARY[k][0], "title": BADGE_LIBRARY[k][1], "text": BADGE_LIBRARY[k][2]}
        for k in outcome.get("badges", []) if k in BADGE_LIBRARY
    ]
    return {
        "student": student,
        "res": res,
        "pct": pct,
        "grade": grade,
        "headline": headline,
        "message": message,
        "badges": badges,
        "cmp": cmp_data,
        "outcome": outcome,
        "topics": topic_breakdown(res["review"]),
        "passed": pct >= settings["pass_pct"],
    }


@app.route("/report")
def report():
    data = report_data()
    if data is None:
        return redirect(url_for("home"))
    settings = cfg()
    outcome = data["outcome"]
    res = data["res"]
    return render_template_string(
        page(REPORT_TEMPLATE),
        student=data["student"],
        pct=data["pct"],
        points=fmt_points(res["points"]),
        max_points=fmt_points(res["max_points"]),
        correct=res["correct"],
        wrong=res["wrong"],
        skipped=res["skipped"],
        best_streak=res["best_streak"],
        review=res["review"],
        topics=data["topics"],
        grade=data["grade"],
        headline=data["headline"],
        message=data["message"],
        badges=data["badges"],
        cmp=data["cmp"],
        passed=data["passed"],
        mode=outcome.get("mode", "exam"),
        xp=outcome.get("xp", 0),
        level=level_for(outcome.get("xp", 0)),
        time_text=fmt_duration(outcome.get("time_taken")),
        submitted_at=outcome.get("submitted_at", ""),
        show_cert=settings["certificate"] and data["passed"] and outcome.get("mode") == "exam",
        page_title="Report card",
    )


CERTIFICATE_TEMPLATE = """
<div class="narrow">
  <a class="back no-print" href="{{ url_for('report') }}">{{ icon('arrow-left') }}Back to the report card</a>
  <section class="cert">
    <div class="cert-kicker">{{ cfg.school_name }}</div>
    <h1>Certificate of achievement</h1>
    <p class="muted" style="margin:0;">This is presented to</p>
    <div class="name">{{ student.name }}</div>
    <div class="rule"></div>
    <p style="max-width:44ch;margin:0 auto;">
      for completing <b>{{ cfg.test_title }}</b> with a score of <b>{{ points }} out of {{ max_points }} marks</b>
      ({{ pct }}%, grade {{ grade }}).
    </p>
    <svg class="seal" viewBox="0 0 100 100" aria-hidden="true">
      <circle cx="50" cy="50" r="42" fill="none" stroke="var(--accent)" stroke-width="2" opacity=".5"/>
      <circle cx="50" cy="50" r="34" fill="none" stroke="var(--accent)" stroke-width="1" opacity=".35"/>
      <path d="M34 51l11 11 22-24" fill="none" stroke="var(--accent)" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <div class="cert-meta">
      <span>Class {{ student.student_class }} &middot; {{ student.semester }}</span>
      <span>{{ submitted_at }}</span>
      <span>Certificate no. {{ serial }}</span>
    </div>
  </section>
  <div class="btn-row no-print" style="margin-top:22px;">
    <button class="btn" type="button" onclick="window.print()">{{ icon('print') }}Print or save as PDF</button>
    <a class="btn btn-ghost" href="{{ url_for('home') }}">Back to start</a>
  </div>
</div>
"""


@app.route("/certificate")
def certificate():
    settings = cfg()
    data = report_data()
    if data is None or not settings["certificate"]:
        return redirect(url_for("home"))
    if not data["passed"] or data["outcome"].get("mode") != "exam":
        flash("Certificates are given for scores at or above the pass mark.", "error")
        return redirect(url_for("report"))
    res = data["res"]
    serial = "{}-{:04d}".format(datetime.now(IST).strftime("%y%m"), (session.get("result_id") or 0) % 10000)
    return render_template_string(
        page(CERTIFICATE_TEMPLATE),
        student=data["student"],
        pct=data["pct"],
        grade=data["grade"],
        points=fmt_points(res["points"]),
        max_points=fmt_points(res["max_points"]),
        submitted_at=data["outcome"].get("submitted_at", ""),
        serial=serial,
        page_title="Certificate",
    )


LEADERBOARD_TEMPLATE = """
<div class="narrow">
  <h1>Leaderboard</h1>
  <p class="lead">Top papers for {{ cfg.test_title }}. Ranked by marks, then by the time taken.</p>
  <section class="card">
    {% if rows %}
    <div class="lb">
      {% for r in rows %}
      <div class="lb-row {{ 'me' if r.me }}">
        <div class="lb-rank">{{ loop.index }}</div>
        <div>
          <div class="lb-name">{{ r.name }}{% if r.me %} &middot; you{% endif %}</div>
          <div class="lb-sub">Class {{ r.student_class }} &middot; {{ r.time }} &middot; {{ r.xp }} XP</div>
        </div>
        <div class="lb-score">{{ r.points }}/{{ r.max_points }}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">{{ icon('trophy') }}<p class="muted" style="margin:0;">No papers submitted yet. Be the first name on this board.</p></div>
    {% endif %}
  </section>
  <a class="small-link muted" href="{{ url_for('home') }}">Back to start</a>
</div>
"""


def short_name(full):
    parts = [p for p in full.split() if p]
    if len(parts) == 1:
        return parts[0]
    return parts[0] + " " + parts[-1][0].upper() + "."


@app.route("/leaderboard")
def leaderboard():
    if not cfg()["leaderboard"]:
        return redirect(url_for("home"))
    mine = session.get("result_id")
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM results ORDER BY points DESC, score DESC, "
            "CASE WHEN time_taken IS NULL THEN 999999 ELSE time_taken END ASC LIMIT 25"
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "name": short_name(r["student_name"]),
                "student_class": r["student_class"],
                "points": fmt_points(r["points"] or r["score"]),
                "max_points": fmt_points(r["max_points"] or r["total"]),
                "xp": r["xp"] or 0,
                "time": fmt_clock(r["time_taken"]) or "-",
                "me": mine is not None and r["id"] == mine,
            }
        )
    return render_template_string(page(LEADERBOARD_TEMPLATE), rows=out, page_title="Leaderboard")


# ----------------------------------------------------------------------
# ADMIN
# ----------------------------------------------------------------------
def admin_required():
    return session.get("is_admin", False)


LOGIN_TEMPLATE = """
<div class="narrow" style="max-width:430px;">
  <section class="card pad-lg">
    <h1>Teacher login</h1>
    <p class="lead">Questions, results and settings live behind this password.</p>
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
        time.sleep(0.4)
        flash("That password did not match. Try again.", "error")
    return render_template_string(page(LOGIN_TEMPLATE), page_title="Teacher login")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


def build_dashboard_data(conn):
    settings = load_settings(conn)
    questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    rows = conn.execute("SELECT * FROM results ORDER BY id DESC").fetchall()

    band_counts = {"A+": 0, "A": 0, "B": 0, "C": 0, "D": 0}
    bins = [0] * 10
    pcts, times, focus = [], [], []
    per_q = {q["id"]: {"attempted": 0, "correct": 0, "opts": {"a": 0, "b": 0, "c": 0, "d": 0}} for q in questions}
    per_topic = {}
    per_diff = {d: {"n": 0, "ok": 0} for d in DIFFICULTIES}
    per_day = {}
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
        if r["focus_lost"] is not None:
            focus.append(r["focus_lost"])
        per_day[r["submitted_at"][:10]] = per_day.get(r["submitted_at"][:10], 0) + 1

        if r["answers"]:
            try:
                ans = json.loads(r["answers"])
            except ValueError:
                ans = {}
            for q in questions:
                slot = per_q[q["id"]]
                slot["attempted"] += 1
                chosen = ans.get(str(q["id"]))
                topic = (q["topic"] or "").strip() or "General"
                diff = q["difficulty"] if q["difficulty"] in per_diff else "medium"
                tslot = per_topic.setdefault(topic, {"topic": topic, "n": 0, "ok": 0})
                tslot["n"] += 1
                per_diff[diff]["n"] += 1
                if chosen == q["correct_option"]:
                    slot["correct"] += 1
                    tslot["ok"] += 1
                    per_diff[diff]["ok"] += 1
                elif chosen in slot["opts"]:
                    slot["opts"][chosen] += 1

        if len(results) < 800:
            d = dict(r)
            d["pct"] = pct
            d["grade"] = grade
            d["time"] = fmt_clock(r["time_taken"]) or "-"
            d["points_text"] = fmt_points(r["points"] or r["score"])
            d["max_text"] = fmt_points(r["max_points"] or r["total"])
            results.append(d)

    top = max(bins) if bins else 0
    hist = [{"label": str(i * 10), "count": n, "height": round(n / top * 100) if top else 0} for i, n in enumerate(bins)]
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
                wrong_note = (
                    f"Most chosen wrong answer: {letter.upper()} \u2014 {q[OPTION_KEYS[letter]]} "
                    f"({round(count / attempted * 100)}% of students)"
                )
        items.append(
            {
                "id": q["id"], "number": i, "text": q["question_text"], "pct": pct,
                "level": level, "wrong_note": wrong_note,
                "topic": (q["topic"] or "").strip() or "General",
                "difficulty": q["difficulty"] or "medium",
            }
        )
    hardest = sorted([x for x in items if x["pct"] is not None], key=lambda x: x["pct"])[:5]

    topic_rows = []
    for t in per_topic.values():
        t["pct"] = round(t["ok"] / t["n"] * 100) if t["n"] else 0
        t["level"] = "good" if t["pct"] >= 70 else "mid" if t["pct"] >= 40 else "low"
        topic_rows.append(t)
    topic_rows.sort(key=lambda x: x["pct"])

    diff_rows = []
    for name in DIFFICULTIES:
        slot = per_diff[name]
        if slot["n"]:
            pct = round(slot["ok"] / slot["n"] * 100)
            diff_rows.append({"name": name, "pct": pct, "n": slot["n"],
                              "level": "good" if pct >= 70 else "mid" if pct >= 40 else "low"})

    days = sorted(per_day.items())[-14:]
    day_top = max([c for _, c in days], default=0)
    spark = [{"day": d, "count": c, "height": round(c / day_top * 100) if day_top else 0} for d, c in days]

    stats = {
        "count": len(rows),
        "avg": round(sum(pcts) / len(pcts)) if pcts else 0,
        "best": max(pcts) if pcts else 0,
        "avg_time": fmt_clock(sum(times) / len(times)) if times else "-",
        "pass_rate": round(sum(1 for p in pcts if p >= settings["pass_pct"]) / len(pcts) * 100) if pcts else 0,
        "flagged": sum(1 for f in focus if f >= 3),
        "total_marks": fmt_points(sum(question_marks(q) for q in questions)),
    }
    return {
        "questions": questions,
        "results": results,
        "stats": stats,
        "hist": hist,
        "grades": grades,
        "items": items,
        "hardest": hardest,
        "topic_rows": topic_rows,
        "diff_rows": diff_rows,
        "spark": spark,
        "classes": sorted(classes, key=str.lower),
        "topics": sorted({(q["topic"] or "").strip() for q in questions if (q["topic"] or "").strip()}, key=str.lower),
    }


DASHBOARD_TEMPLATE = """
<div class="dash-top">
  <div>
    <h1>Dashboard</h1>
    <span class="pill {{ 'ok' if cfg.is_open else 'bad' }}"><span class="dot"></span>{{ 'Open to students' if cfg.is_open else 'Closed to students' }}</span>
    <span class="pill" style="margin-left:6px;">{{ questions|length }} questions &middot; {{ stats.total_marks }} marks</span>
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
      <button class="nav-item" type="button" data-tab="insights">{{ icon('target') }}Insights</button>
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
    <div class="stat"><div class="ico">{{ icon('medal') }}</div><div><b>{{ stats.pass_rate }}%</b><span>Passed ({{ cfg.pass_pct }}%+)</span></div></div>
    <div class="stat"><div class="ico">{{ icon('clock') }}</div><div><b>{{ stats.avg_time }}</b><span>Average time</span></div></div>
    <div class="stat"><div class="ico">{{ icon('eye') }}</div><div><b>{{ stats.flagged }}</b><span>3+ tab switches</span></div></div>
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
      {% if spark %}
      <h3 style="margin-top:22px;">Submissions, last {{ spark|length }} day{{ '' if spark|length == 1 else 's' }}</h3>
      <div class="spark" role="img" aria-label="Submissions per day">
        {% for s in spark %}<i class="{{ 'hot' if s.height > 60 }}" style="height:{{ s.height }}%;" title="{{ s.day }}: {{ s.count }}"></i>{% endfor %}
      </div>
      {% endif %}
    </section>
  </div>
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
        <div class="grid3">
          <label class="field"><span>Correct option</span>
            <select name="correct_option" required>
              <option value="a">A</option><option value="b">B</option><option value="c">C</option><option value="d">D</option>
            </select>
          </label>
          <label class="field"><span>Difficulty</span>
            <select name="difficulty">
              <option value="easy">Easy</option><option value="medium" selected>Medium</option><option value="hard">Hard</option>
            </select>
          </label>
          <label class="field"><span>Marks</span>
            <input type="number" name="marks" min="0.5" max="20" step="0.5" value="1">
          </label>
        </div>
        <label class="field"><span>Topic <small>optional, groups the analysis</small></span>
          <input type="text" name="topic" maxlength="40" list="topic-list" placeholder="Algebra">
          <datalist id="topic-list">{% for t in topics %}<option value="{{ t }}"></option>{% endfor %}</datalist>
        </label>
        <label class="field"><span>Explanation <small>optional, shown to students who miss it</small></span>
          <textarea name="explanation" rows="2" maxlength="400" placeholder="Why is this the right answer?"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add question</button>
      </form>
    </section>

    <section class="card">
      <h2>Add many at once</h2>
      <p class="lead" style="margin-bottom:14px;">One question per line, parts separated by the | symbol. Everything after the correct letter is optional.</p>
      <code class="fmt">Question | A | B | C | D | b | Explanation | Topic | hard | 2</code>
      <form method="POST" action="{{ url_for('bulk_add') }}" enctype="multipart/form-data">
        <label class="field"><span>Paste questions</span>
          <textarea name="bulk" rows="9" placeholder="What is 2 + 2? | 3 | 4 | 5 | 6 | b | Adding two and two gives four. | Arithmetic | easy | 1&#10;Capital of India? | Mumbai | Delhi | Chennai | Kolkata | b"></textarea>
        </label>
        <label class="field"><span>Or upload a .txt or .csv file <small>same pipe format, one per line</small></span>
          <input type="file" name="file" accept=".txt,.csv,text/plain">
        </label>
        <button class="btn btn-full" type="submit">Add all questions</button>
      </form>
    </section>
  </div>

  <section class="card">
    <div class="tools">
      <div><h2>All questions ({{ questions|length }})</h2><span class="muted">Edits apply to students who start after you save.</span></div>
      <div class="filters">
        <input type="text" id="q-search" placeholder="Search questions" aria-label="Search questions">
        {% if questions %}<a class="btn btn-ghost btn-sm" href="{{ url_for('export_questions') }}">{{ icon('download') }}Backup</a>{% endif %}
      </div>
    </div>
    <div id="q-list">
    {% for q in questions %}
    <div class="qrow" data-text="{{ (q.question_text ~ ' ' ~ (q.topic or ''))|lower }}">
      <div>
        <b>{{ loop.index }}. {{ q.question_text }}</b>
        <div class="opts">
          {% for letter, key in [('A','option_a'),('B','option_b'),('C','option_c'),('D','option_d')] %}
            <span class="{{ 'right' if letter|lower == q.correct_option else '' }}">{{ letter }}. {{ q[key] }}</span>{% if not loop.last %} &nbsp; {% endif %}
          {% endfor %}
        </div>
        <div class="meta">
          {% if q.topic %}<span class="tag">{{ q.topic }}</span>{% endif %}
          <span class="tag {{ q.difficulty or 'medium' }}">{{ q.difficulty or 'medium' }}</span>
          <span class="tag">{{ '%g'|format(q.marks or 1) }} mark{{ '' if (q.marks or 1) == 1 else 's' }}</span>
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
    {% if questions %}<p class="muted" style="margin:16px 0 0;">On Render's free plan the database can reset when the app redeploys. Keep the backup file, then paste it back into "Add many at once" to restore everything.</p>{% endif %}
  </section>
</div>

<!-- RESULTS -->
<div class="panel" data-panel="results">
  <section class="card">
    <div class="tools">
      <div>
        <h2>Results</h2>
        <span class="muted">Showing {{ results|length }} of {{ stats.count }}. Click a column to sort, or a name to see their paper.</span>
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
          <th data-sort="num">Time</th><th data-sort="num">Tab switches</th><th data-sort="num">XP</th><th data-sort="text">Submitted</th><th></th>
        </tr></thead>
        <tbody>
        {% for r in results %}
        <tr>
          <td data-v="{{ r.student_name|lower }}"><a href="{{ url_for('result_detail', rid=r.id) }}">{{ r.student_name }}</a></td>
          <td data-v="{{ r.student_class|lower }}">{{ r.student_class }}</td>
          <td data-v="{{ r.semester|lower }}">{{ r.semester }}</td>
          <td data-v="{{ r.phone }}">{{ r.phone }}</td>
          <td data-v="{{ r.points or r.score }}" class="tabular">{{ r.points_text }}/{{ r.max_text }}</td>
          <td data-v="{{ r.pct }}" class="tabular">{{ r.pct }}%</td>
          <td data-v="{{ r.grade }}">{{ r.grade }}</td>
          <td data-v="{{ r.time_taken if r.time_taken is not none else -1 }}" class="tabular">{{ r.time }}</td>
          <td data-v="{{ r.focus_lost or 0 }}" class="tabular {{ 'warn' if (r.focus_lost or 0) >= 3 }}">{{ r.focus_lost if r.focus_lost is not none else '-' }}</td>
          <td data-v="{{ r.xp or 0 }}" class="tabular">{{ r.xp or 0 }}</td>
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

<!-- INSIGHTS -->
<div class="panel" data-panel="insights">
  <div class="two">
    <section class="card">
      <h2>Topics that need another lesson</h2>
      <p class="muted" style="margin:0 0 14px;">Weakest first, across every submitted paper.</p>
      <div class="topics">
        {% for t in topic_rows %}
        <div class="topic-row">
          <div class="nm">{{ t.topic }} <span class="muted" style="font-weight:400;">&middot; {{ t.ok }}/{{ t.n }} answers</span></div>
          <div class="pc">{{ t.pct }}%</div>
          <div class="meter {{ t.level }}"><i style="width:{{ t.pct }}%;"></i></div>
        </div>
        {% else %}
        <p class="muted" style="margin:0;">Tag your questions with a topic to unlock this breakdown.</p>
        {% endfor %}
      </div>
    </section>
    <section class="card">
      <h2>Difficulty check</h2>
      <p class="muted" style="margin:0 0 14px;">Is your "hard" really hard? Compare the labels with the scores.</p>
      <div class="topics">
        {% for d in diff_rows %}
        <div class="topic-row">
          <div class="nm" style="text-transform:capitalize;">{{ d.name }} <span class="muted" style="font-weight:400;">&middot; {{ d.n }} answers</span></div>
          <div class="pc">{{ d.pct }}%</div>
          <div class="meter {{ d.level }}"><i style="width:{{ d.pct }}%;"></i></div>
        </div>
        {% else %}
        <p class="muted" style="margin:0;">No answers yet.</p>
        {% endfor %}
      </div>
    </section>
  </div>

  <section class="card">
    <h2>Question by question</h2>
    <p class="muted" style="margin:0 0 10px;">Share of students who got each one right, with the wrong answer they fell for.</p>
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
        <div class="grid3">
          <label class="field"><span>Time limit <small>min</small></span>
            <input type="number" name="minutes" min="1" max="300" value="{{ cfg.minutes }}" required>
          </label>
          <label class="field"><span>Pass mark <small>%</small></span>
            <input type="number" name="pass_pct" min="0" max="100" value="{{ cfg.pass_pct }}" required>
          </label>
          <label class="field"><span>Negative marks</span>
            <input type="number" name="negative" min="0" max="5" step="0.25" value="{{ '%g'|format(cfg.negative) }}">
          </label>
        </div>
        <label class="field"><span>Message for students <small>optional, shown before the test</small></span>
          <textarea name="note" rows="3" maxlength="300" placeholder="Best of luck. No calculators.">{{ cfg.note }}</textarea>
        </label>
        <span class="field" style="margin-bottom:8px;"><span>Accent colour</span></span>
        <div class="swatches">
          {% for key, pair in accents.items() %}
          <label title="{{ key }}">
            <input type="radio" name="accent" value="{{ key }}" {{ 'checked' if cfg.accent == key }}>
            <span class="sw" style="background:linear-gradient(145deg,{{ pair[0] }},{{ pair[1] }});"></span>
          </label>
          {% endfor %}
        </div>
        <label class="check"><input type="checkbox" name="is_open" {{ 'checked' if cfg.is_open }}>
          <span><b>Test is open</b><small>Turn off to stop new students from starting.</small></span></label>
        <label class="check"><input type="checkbox" name="shuffle" {{ 'checked' if cfg.shuffle }}>
          <span><b>Shuffle question order</b><small>Each student sees a different order.</small></span></label>
        <label class="check"><input type="checkbox" name="shuffle_options" {{ 'checked' if cfg.shuffle_options }}>
          <span><b>Shuffle the options too</b><small>A, B, C, D move around as well, so answers cannot be copied.</small></span></label>
        <label class="check"><input type="checkbox" name="allow_retake" {{ 'checked' if cfg.allow_retake }}>
          <span><b>Allow retakes</b><small>If off, the same name, class, semester and phone can submit only once.</small></span></label>
        <label class="check"><input type="checkbox" name="practice" {{ 'checked' if cfg.practice }}>
          <span><b>Offer a practice run</b><small>Students can take the paper without it being recorded.</small></span></label>
        <label class="check"><input type="checkbox" name="show_answers" {{ 'checked' if cfg.show_answers }}>
          <span><b>Show the answer review</b><small>Turn off if the same paper is being written in another batch.</small></span></label>
        <label class="check"><input type="checkbox" name="show_stats" {{ 'checked' if cfg.show_stats }}>
          <span><b>Show class comparison</b><small>Report card shows the class average once 5 students have submitted.</small></span></label>
        <label class="check"><input type="checkbox" name="leaderboard" {{ 'checked' if cfg.leaderboard }}>
          <span><b>Public leaderboard</b><small>Top 25 papers, shown with shortened names.</small></span></label>
        <label class="check"><input type="checkbox" name="certificate" {{ 'checked' if cfg.certificate }}>
          <span><b>Printable certificate</b><small>Offered to students who reach the pass mark.</small></span></label>
        <label class="check"><input type="checkbox" name="track_focus" {{ 'checked' if cfg.track_focus }}>
          <span><b>Record tab switches</b><small>Counts how often a student leaves the test page. Students are told about this.</small></span></label>
        <label class="check"><input type="checkbox" name="focus_mode" {{ 'checked' if cfg.focus_mode }}>
          <span><b>Offer full screen</b><small>Adds a full-screen button and warns when a student leaves it.</small></span></label>
        <label class="check"><input type="checkbox" name="sound" {{ 'checked' if cfg.sound }}>
          <span><b>Sound cues</b><small>Soft blips when an option is picked and when time is short.</small></span></label>
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
    t.addEventListener('click', function () { history.replaceState(null, '', '#' + t.dataset.tab); show(t.dataset.tab); });
  });
  show(location.hash.slice(1) || 'overview');

  var copyBtn = document.getElementById('copy-btn');
  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var input = document.getElementById('share-url');
      function done() { window.toast('Link copied', 'good'); }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).then(done, function () { input.select(); document.execCommand('copy'); done(); });
      } else { input.select(); document.execCommand('copy'); done(); }
    });
  }

  var qSearch = document.getElementById('q-search');
  if (qSearch) {
    qSearch.addEventListener('input', function () {
      var v = qSearch.value.toLowerCase();
      [].forEach.call(document.querySelectorAll('#q-list .qrow'), function (row) {
        row.hidden = v && row.dataset.text.indexOf(v) === -1;
      });
    });
  }

  var search = document.getElementById('search');
  var classSel = document.getElementById('class-filter');
  var table = document.getElementById('results-table');
  function applyFilters() {
    if (!table) return;
    var q = search.value.toLowerCase(), c = classSel.value;
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
          var r = th.dataset.sort === 'num' ? (parseFloat(x) - parseFloat(y)) : String(x).localeCompare(String(y));
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
        page(DASHBOARD_TEMPLATE), wide=True, share_url=request.url_root,
        accents=ACCENTS, page_title="Dashboard", **data
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
      <div class="chip"><b>{{ points }}/{{ max_points }}</b><small>Marks</small></div>
      <div class="chip"><b>{{ pct }}%</b><small>Percentage</small></div>
      <div class="chip"><b>{{ grade }}</b><small>Grade</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
      <div class="chip"><b>{{ r.focus_lost if r.focus_lost is not none else '-' }}</b><small>Tab switches</small></div>
      <div class="chip"><b>{{ r.xp or 0 }}</b><small>XP</small></div>
    </div>
    <p class="muted" style="margin:16px 0 0;">Submitted {{ r.submitted_at }}</p>
  </section>

  {% if topics|length > 1 %}
  <section class="card">
    <h2>Topic by topic</h2>
    <div class="topics" style="margin-top:14px;">
      {% for t in topics %}
      <div class="topic-row">
        <div class="nm">{{ t.topic }} <span class="muted" style="font-weight:400;">&middot; {{ t.ok }}/{{ t.n }}</span></div>
        <div class="pc">{{ t.pct }}%</div>
        <div class="meter {{ t.level }}"><i style="width:{{ t.pct }}%;"></i></div>
      </div>
      {% endfor %}
    </div>
  </section>
  {% endif %}

  <section class="card">
    <div class="rv-head"><h2>Their paper</h2>
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
    settings = cfg()
    with closing(get_db()) as conn:
        r = conn.execute("SELECT * FROM results WHERE id = ?", (rid,)).fetchone()
        if r is None:
            abort(404)
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

    review, topics = None, []
    if r["answers"]:
        try:
            res = evaluate(questions, json.loads(r["answers"]), settings["negative"])
            review = res["review"]
            topics = topic_breakdown(review)
        except ValueError:
            review = None
    pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
    return render_template_string(
        page(RESULT_TEMPLATE),
        r=r,
        pct=pct,
        grade=grade_for(pct)[0],
        points=fmt_points(r["points"] or r["score"]),
        max_points=fmt_points(r["max_points"] or r["total"]),
        time_text=fmt_duration(r["time_taken"]),
        review=review,
        topics=topics,
        page_title=r["student_name"],
    )


EDIT_TEMPLATE = """
<div class="narrow">
  <a class="back" href="{{ url_for('admin_dashboard') }}#questions">{{ icon('arrow-left') }}Back to questions</a>
  <section class="card pad-lg">
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
          <input type="number" name="marks" min="0.5" max="20" step="0.5" value="{{ '%g'|format(q.marks or 1) }}">
        </label>
      </div>
      <label class="field"><span>Topic <small>optional</small></span>
        <input type="text" name="topic" maxlength="40" value="{{ q.topic or '' }}">
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
    topic = request.form.get("topic", "").strip()[:40]
    difficulty = request.form.get("difficulty", "medium")
    if difficulty not in DIFFICULTIES:
        difficulty = "medium"
    try:
        marks = max(0.5, min(20.0, float(request.form.get("marks", "1"))))
    except ValueError:
        marks = 1.0
    ok = bool(text) and all(options) and correct in OPTION_KEYS
    return ok, (text, *options, correct, explanation, topic, difficulty, marks)


INSERT_Q = (
    "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option, "
    "explanation, topic, difficulty, marks) VALUES (?,?,?,?,?,?,?,?,?,?)"
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


def parse_bulk_line(line):
    """Question | A | B | C | D | correct | [explanation] | [topic] | [difficulty] | [marks]"""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 6 or not all(parts[:6]) or parts[5].lower() not in OPTION_KEYS:
        return None
    explanation = parts[6][:400] if len(parts) > 6 else ""
    topic = parts[7][:40] if len(parts) > 7 else ""
    difficulty = parts[8].lower() if len(parts) > 8 and parts[8].lower() in DIFFICULTIES else "medium"
    try:
        marks = max(0.5, min(20.0, float(parts[9]))) if len(parts) > 9 and parts[9] else 1.0
    except ValueError:
        marks = 1.0
    return (parts[0], parts[1], parts[2], parts[3], parts[4], parts[5].lower(), explanation, topic, difficulty, marks)


@app.route("/admin/bulk_add", methods=["POST"])
def bulk_add():
    if not admin_required():
        return redirect(url_for("admin_login"))

    text = request.form.get("bulk", "")
    upload = request.files.get("file")
    if upload and upload.filename:
        try:
            text += "\n" + upload.read().decode("utf-8-sig", errors="replace")
        except Exception:
            flash("That file could not be read. Save it as plain text and try again.", "error")

    rows, bad_lines = [], []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parsed = parse_bulk_line(line)
        if parsed is None:
            bad_lines.append(str(number))
        else:
            rows.append(parsed)

    if rows:
        with closing(get_db()) as conn:
            conn.executemany(INSERT_Q, rows)
            conn.commit()
        flash(f"{len(rows)} question(s) added.", "success")
    if bad_lines:
        flash(
            "Skipped line(s) " + ", ".join(bad_lines[:20])
            + ". Each line needs a question, 4 options and the correct letter, separated by |.",
            "error",
        )
    if not rows and not bad_lines:
        flash("Nothing to add. Paste at least one question or choose a file.", "error")
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
        lines.append(
            " | ".join(
                [
                    clean(q["question_text"]), clean(q["option_a"]), clean(q["option_b"]),
                    clean(q["option_c"]), clean(q["option_d"]), q["correct_option"],
                    clean(q["explanation"]), clean(q["topic"]), q["difficulty"] or "medium",
                    fmt_points(question_marks(q)),
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
        conn.execute("DELETE FROM attempts")
        conn.commit()
    flash("All results deleted.", "success")
    return back_to("settings")


@app.route("/admin/settings", methods=["POST"])
def save_settings():
    if not admin_required():
        return redirect(url_for("admin_login"))

    def whole(name, lo, hi, fallback):
        try:
            return str(max(lo, min(hi, int(float(request.form.get(name, fallback))))))
        except ValueError:
            return str(fallback)

    values = {
        "school_name": request.form.get("school_name", "").strip()[:60] or DEFAULT_SETTINGS["school_name"],
        "test_title": request.form.get("test_title", "").strip()[:80] or DEFAULT_SETTINGS["test_title"],
        "minutes": whole("minutes", 1, 300, 15),
        "pass_pct": whole("pass_pct", 0, 100, 40),
        "note": request.form.get("note", "").strip()[:300],
        "accent": request.form.get("accent") if request.form.get("accent") in ACCENTS else "violet",
    }
    try:
        values["negative"] = str(max(0.0, min(5.0, round(float(request.form.get("negative", "0")), 2))))
    except ValueError:
        values["negative"] = "0"
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
            "Percentage", "Grade", "Wrong", "Skipped", "Best streak", "XP", "Badges",
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
        try:
            badges = ", ".join(BADGE_LIBRARY[k][1] for k in json.loads(r["badges"] or "[]") if k in BADGE_LIBRARY)
        except ValueError:
            badges = ""
        marks = []
        for q in questions:  # 1 = correct, 0 = wrong or skipped, blank = no data
            marks.append("" if ans is None else (1 if ans.get(str(q["id"])) == q["correct_option"] else 0))
        writer.writerow(
            [
                csv_safe(r["student_name"]), csv_safe(r["student_class"]), csv_safe(r["semester"]), r["phone"],
                r["score"], r["total"], fmt_points(r["points"] or r["score"]), fmt_points(r["max_points"] or r["total"]),
                pct, grade_for(pct)[0], r["wrong_count"] or 0, r["skipped_count"] or 0, r["best_streak"] or 0,
                r["xp"] or 0, badges, fmt_clock(r["time_taken"]),
                "" if r["focus_lost"] is None else r["focus_lost"], r["submitted_at"],
            ]
            + marks
        )

    filename = "test_results_" + datetime.now(IST).strftime("%Y%m%d_%H%M") + ".csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),  # BOM so Excel opens it correctly
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.errorhandler(404)
def not_found(_e):
    body = """
    <div class="narrow"><section class="card pad-lg" style="text-align:center;">
      <h1>That page is not here</h1>
      <p class="lead" style="margin:0 auto 20px;">The link may be old, or the test may have moved on.</p>
      <a class="btn" href="{{ url_for('home') }}">Go to the test</a>
    </section></div>"""
    return render_template_string(page(body), page_title="Not found"), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
