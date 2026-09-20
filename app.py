"""
Student Test Portal (v3) - single-file Flask app: SQLite + HTML + CSS + JavaScript.

STUDENT FLOW
    Details -> Instructions -> Timed test (one question at a time) -> Report card

ADMIN FLOW  (/admin)
    Overview (analysis + share link) | Questions (add / bulk add / edit / backup)
    Results (search, sort, per-student answers) | Settings

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
# SHARED LAYOUT + DESIGN SYSTEM  (white background, responsive, Nunito)
# ----------------------------------------------------------------------
LAYOUT_TOP = """{% macro icon(name) %}<svg class="ic" aria-hidden="true"><use href="#i-{{ name }}"/></svg>{% endmacro %}<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#FFFFFF">
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
.ic{width:20px;height:20px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;}

/* page frame */
.brandbar{max-width:1000px;margin:0 auto;padding:20px 20px 0;display:flex;align-items:center;justify-content:space-between;gap:16px;}
.brandbar.wide,.wrap.wide{max-width:1180px;}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:1.05rem;min-width:0;}
.brand span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.mark{
  flex:none;width:36px;height:36px;border-radius:11px;background:var(--brand);color:#fff;
  display:grid;place-items:center;box-shadow:0 0 0 4px var(--brand-tint);
}
.mark .ic{width:20px;height:20px;stroke-width:3;}
.brand-test{font-size:.85rem;font-weight:700;color:var(--muted);white-space:nowrap;}
.wrap{max-width:1000px;margin:0 auto;padding:22px 20px 56px;}
.narrow{max-width:720px;margin:0 auto;}
.foot{max-width:1000px;margin:0 auto;padding:0 20px 32px;text-align:center;color:#9AA1BA;font-size:.8rem;}

/* typography */
h1{font-size:1.75rem;line-height:1.2;margin:0 0 8px;font-weight:800;letter-spacing:-0.015em;}
h2{font-size:1.15rem;margin:0 0 4px;font-weight:800;letter-spacing:-0.005em;}
.lead{color:var(--muted);margin:0 0 22px;}
.muted{color:var(--muted);font-size:.9rem;}

/* cards, pills */
.card{border:1px solid var(--line);border-radius:var(--radius);padding:28px;margin-bottom:20px;background:#fff;}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;background:var(--brand-tint);color:var(--brand-dark);font-weight:700;font-size:.85rem;}
.pill.ok{background:var(--ok-tint);color:var(--ok);}
.pill.bad{background:var(--err-tint);color:var(--err);}
.pill .ic{width:15px;height:15px;}

/* forms */
.field{display:block;margin-bottom:16px;}
.field > span{display:block;font-weight:700;font-size:.9rem;margin-bottom:6px;}
.field > span small{font-weight:600;color:var(--muted);}
input[type=text],input[type=tel],input[type=password],input[type=number],select,textarea{
  width:100%;padding:13px 14px;border:1.5px solid var(--line);border-radius:12px;
  font:inherit;font-size:16px;color:var(--ink);background:#fff;outline:none;
  transition:border-color .15s, box-shadow .15s;
}
textarea{resize:vertical;line-height:1.5;}
input::placeholder,textarea::placeholder{color:#9AA1BA;}
input:focus,select:focus,textarea:focus{border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-tint);}
input[readonly]{background:var(--soft);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;}
.check{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px;cursor:pointer;}
.check input{width:20px;height:20px;margin-top:2px;accent-color:var(--brand);flex:none;}
.check b{display:block;font-size:.95rem;}
.check small{color:var(--muted);}
.pw{position:relative;}
.pw input{padding-right:48px;}
.pw button{position:absolute;right:6px;top:6px;width:40px;height:40px;border:none;background:none;color:var(--muted);cursor:pointer;border-radius:10px;display:grid;place-items:center;}
.pw button:hover{background:var(--soft);}

/* buttons */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:13px 22px;border-radius:12px;border:1.5px solid var(--brand);
  background:var(--brand);color:#fff;font:inherit;font-weight:800;cursor:pointer;
  text-decoration:none;transition:background .15s, transform .1s;
}
.btn .ic{width:18px;height:18px;}
.btn:hover{background:var(--brand-dark);border-color:var(--brand-dark);}
.btn:active{transform:translateY(1px);}
.btn:focus-visible,.pb:focus-visible,.nav-item:focus-visible,.pw button:focus-visible{outline:3px solid var(--brand);outline-offset:2px;}
.btn-full{width:100%;margin-top:6px;}
.btn-ghost{background:#fff;color:var(--ink);border-color:var(--line);}
.btn-ghost:hover{background:var(--soft);border-color:var(--line);}
.btn-ghost.on{background:var(--amber-tint);border-color:var(--amber);color:#7A5200;}
.btn-danger{background:#fff;color:var(--err);border-color:#F1B5B5;}
.btn-danger:hover{background:var(--err-tint);border-color:#F1B5B5;}
.btn-sm{padding:8px 14px;font-size:.875rem;border-radius:10px;}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn.busy{position:relative;color:transparent !important;pointer-events:none;}
.btn.busy .ic{opacity:0;}
.btn.busy::after{
  content:"";position:absolute;left:50%;top:50%;width:18px;height:18px;margin:-9px 0 0 -9px;
  border-radius:50%;border:2.5px solid #fff;border-right-color:transparent;animation:spin .7s linear infinite;
}
.btn-ghost.busy::after{border-color:var(--brand);border-right-color:transparent;}
.btn-danger.busy::after{border-color:var(--err);border-right-color:transparent;}
@keyframes spin{to{transform:rotate(360deg);}}
.small-link{display:block;text-align:center;margin-top:10px;font-size:.9rem;}
.back{display:inline-flex;align-items:center;gap:6px;font-weight:700;font-size:.9rem;margin-bottom:14px;}

/* flash + toasts */
.flash{padding:12px 16px;border-radius:12px;margin-bottom:18px;font-weight:600;font-size:.95rem;background:var(--brand-tint);color:var(--brand-dark);transition:opacity .4s;}
.flash.error{background:var(--err-tint);color:var(--err);}
.flash.success{background:var(--ok-tint);color:var(--ok);}
.toasts{position:fixed;left:0;right:0;bottom:20px;display:flex;flex-direction:column;align-items:center;gap:8px;z-index:100;pointer-events:none;padding:0 16px;}
.toast{
  background:var(--ink);color:#fff;padding:11px 18px;border-radius:12px;font-weight:700;font-size:.92rem;
  box-shadow:0 10px 30px rgba(27,33,64,.25);transition:opacity .3s, transform .3s;max-width:420px;text-align:center;
}
.toast.warn{background:#7A5200;}
.toast.out{opacity:0;transform:translateY(8px);}

/* home */
.split{display:grid;grid-template-columns:1fr;gap:28px;align-items:start;}
@media (min-width:860px){.split{grid-template-columns:1fr 1fr;gap:56px;padding-top:20px;}}
.intro h1{font-size:2.3rem;margin:14px 0 10px;}
.facts{display:flex;flex-wrap:wrap;gap:10px 18px;margin:20px 0 0;color:var(--ink);font-weight:700;font-size:.92rem;}
.facts span{display:inline-flex;align-items:center;gap:8px;}
.facts .ic{color:var(--brand);width:18px;height:18px;}
.omr{display:block;width:100%;max-width:340px;height:auto;margin:26px 0 0;}
.steps{list-style:none;margin:26px 0 0;padding:0;}
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
@media (max-width:859px){.omr{display:none;}}

/* instructions */
.who{display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid var(--line);}
.avatar{width:48px;height:48px;border-radius:50%;background:var(--brand);color:#fff;display:grid;place-items:center;font-weight:800;font-size:1.2rem;flex:none;}
.who-name{font-weight:800;font-size:1.1rem;line-height:1.2;}
.rules{list-style:none;margin:16px 0 20px;padding:0;}
.rules li{display:flex;gap:12px;padding:9px 0;}
.rules li::before{
  content:"";flex:none;width:20px;height:20px;margin-top:2px;border-radius:50%;background:var(--brand-tint);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%232E3DB0' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6 12.5l4 4 8-9'/%3E%3C/svg%3E");
  background-size:70%;background-repeat:no-repeat;background-position:center;
}
.note{background:var(--amber-tint);border-left:4px solid var(--amber);border-radius:10px;padding:12px 16px;margin:0 0 20px;white-space:pre-line;font-weight:600;color:#5C4200;}

/* test */
.testbar{
  position:sticky;top:0;z-index:10;background:#fff;
  margin:0 -20px 18px;padding:12px 20px 10px;border-bottom:1px solid var(--line);
}
.tb-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.tb-name{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw;}
.tb-meta{font-size:.82rem;color:var(--muted);}
.timer{
  display:inline-flex;align-items:center;gap:8px;
  font-weight:800;font-variant-numeric:tabular-nums;white-space:nowrap;
  background:var(--brand-tint);color:var(--brand-dark);padding:7px 14px;border-radius:999px;
}
.timer .ic{width:17px;height:17px;}
.timer.warn{background:var(--amber-tint);color:#7A5200;}
.timer.low{background:var(--err-tint);color:var(--err);}
.track{height:6px;background:var(--line);border-radius:999px;overflow:hidden;margin-top:10px;}
.bar{height:100%;width:0;background:var(--brand);border-radius:999px;transition:width .25s;}
.tb-count{display:flex;justify-content:space-between;font-size:.8rem;color:var(--muted);margin-top:6px;font-weight:600;}
.tb-count .saved{display:inline-flex;align-items:center;gap:5px;}
.tb-count .saved .ic{width:14px;height:14px;color:var(--ok);}

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
.dlg .btn-row{margin-top:22px;flex-wrap:nowrap;}
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
.cmp{margin-top:24px;padding-top:20px;border-top:1px solid var(--line);text-align:left;}
.cmp-top{display:flex;justify-content:space-between;align-items:baseline;}
.cmp-track{position:relative;height:8px;background:var(--line);border-radius:999px;margin:22px 8px 16px;}
.cmp-track i{position:absolute;top:50%;width:18px;height:18px;border-radius:50%;transform:translate(-50%,-50%);border:3px solid #fff;box-shadow:0 0 0 1px var(--line);}
.cmp-track .you,.k.you{background:var(--brand);}
.cmp-track .avg,.k.avg{background:var(--amber);}
.cmp-legend{display:flex;gap:18px;flex-wrap:wrap;font-size:.85rem;font-weight:700;}
.cmp-legend .k{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:7px;}
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
.rv-x{margin-top:8px;padding:9px 12px;background:var(--soft);border-left:3px solid var(--brand);border-radius:8px;font-size:.9rem;}
.print-only{display:none;}
.cf{position:fixed;top:-16px;width:9px;height:14px;border-radius:2px;pointer-events:none;z-index:60;animation:fall linear forwards;}
@keyframes fall{to{transform:translate(var(--dx),108vh) rotate(720deg);}}

/* admin */
.dash-top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;margin-bottom:22px;}
.dash-top h1{margin-bottom:6px;}
.admin{display:grid;grid-template-columns:210px minmax(0,1fr);gap:36px;align-items:start;}
.side{position:sticky;top:20px;}
.navlist{display:flex;flex-direction:column;gap:4px;}
.nav-item{
  display:flex;align-items:center;gap:12px;width:100%;padding:11px 14px;border:none;background:none;border-radius:12px;
  font:inherit;font-weight:700;color:var(--muted);cursor:pointer;text-align:left;
}
.nav-item:hover{background:var(--soft);}
.nav-item.active{background:var(--brand-tint);color:var(--brand-dark);}
.panel{display:none;}
.panel.active{display:block;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:20px;}
.stat{display:flex;gap:12px;align-items:center;border:1px solid var(--line);border-radius:14px;padding:14px 16px;}
.stat .ico{flex:none;width:40px;height:40px;border-radius:12px;background:var(--brand-tint);color:var(--brand-dark);display:grid;place-items:center;}
.stat b{display:block;font-size:1.45rem;font-weight:800;line-height:1.2;}
.stat span{font-size:.82rem;color:var(--muted);font-weight:600;}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
.two > .card{margin-bottom:20px;}
.share{display:flex;gap:18px;justify-content:space-between;align-items:center;flex-wrap:wrap;}
.share-row{display:flex;gap:10px;flex-wrap:wrap;flex:1;min-width:280px;justify-content:flex-end;}
.share-row input{flex:1;min-width:200px;}
.hist{display:flex;align-items:flex-end;gap:7px;height:160px;margin-top:14px;}
.hcol{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;font-size:.75rem;color:var(--muted);font-weight:700;gap:4px;}
.hbar{width:100%;background:var(--brand);border-radius:8px 8px 3px 3px;min-height:3px;}
.hbar.zero{background:var(--line);}
.hlab{margin-top:2px;}
.gchips{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;}
.gchips span{padding:5px 12px;border-radius:999px;background:var(--soft);font-weight:700;font-size:.85rem;}
.gchips b{color:var(--brand-dark);}
.irow{display:grid;grid-template-columns:1fr 150px 44px;gap:14px;align-items:center;padding:11px 0;border-bottom:1px solid var(--line);font-size:.92rem;}
.irow:last-child{border-bottom:none;}
.irow .qt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;}
.irow .sub{grid-column:1 / -1;margin-top:-8px;font-size:.82rem;color:var(--muted);}
.irow .pc{font-weight:800;text-align:right;}
.irow.two-col{grid-template-columns:1fr 44px;}
.meter{height:10px;background:var(--soft);border-radius:999px;overflow:hidden;}
.meter i{display:block;height:100%;background:var(--brand);border-radius:999px;}
.meter.good i{background:var(--ok);}
.meter.mid i{background:var(--amber);}
.meter.low i{background:var(--err);}
.qrow{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--line);}
.qrow:last-child{border-bottom:none;}
.qrow .opts{font-size:.9rem;color:var(--muted);margin-top:4px;}
.qrow .opts .right{color:var(--ok);font-weight:800;}
.qrow .acts{display:flex;gap:8px;flex:none;}
.tools{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:14px;}
.tools .filters{display:flex;gap:10px;flex-wrap:wrap;}
.tools input{width:230px;}
.tools select{width:150px;}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;font-size:.92rem;min-width:760px;}
th{text-align:left;color:var(--muted);font-weight:700;font-size:.82rem;padding:8px 10px;border-bottom:1.5px solid var(--line);white-space:nowrap;user-select:none;}
th[data-dir=asc]::after{content:" ▲";font-size:.7em;}
th[data-dir=desc]::after{content:" ▼";font-size:.7em;}
td{padding:10px;border-bottom:1px solid var(--line);white-space:nowrap;}
td a{font-weight:700;}
td.warn{color:#9A6500;font-weight:800;}
code.fmt{display:block;background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:.82rem;margin:0 0 14px;overflow-x:auto;white-space:nowrap;}

@media (max-width:900px){
  .admin{grid-template-columns:1fr;gap:14px;}
  .side{position:static;}
  .navlist{flex-direction:row;overflow-x:auto;border-bottom:1px solid var(--line);padding-bottom:8px;}
  .nav-item{width:auto;white-space:nowrap;}
  .two{grid-template-columns:1fr;}
}
@media (max-width:560px){
  .grid2{grid-template-columns:1fr;}
  .card{padding:22px 18px;}
  h1{font-size:1.5rem;}
  .intro h1{font-size:1.9rem;}
  .btn-row .btn{width:100%;}
  .dlg .btn-row .btn{width:auto;}
  .qrow{flex-direction:column;}
  .tb-name{max-width:34vw;}
  .brand-test{display:none;}
  .q{padding:18px;}
  .qnav{grid-template-columns:1fr 1fr;}
  .qnav .mark{grid-column:1 / -1;order:-1;}
  .irow{grid-template-columns:1fr 44px;}
  .irow .meter{grid-column:1 / -1;order:3;}
  .tools input,.tools select{width:100%;}
  .tools .filters{width:100%;}
  .share-row .btn{flex:1;}
}
@media print{
  body{-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .no-print{display:none !important;}
  .print-only{display:block;}
  .card{border-color:#ccc;break-inside:avoid;}
  .cf,.toasts{display:none;}
}
@media (prefers-reduced-motion:reduce){*{transition:none !important;}}
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
</svg>
<div class="brandbar no-print{{ ' wide' if wide }}">
  <div class="brand">
    <div class="mark">{{ icon('check') }}</div>
    <span>{{ cfg.school_name }}</span>
  </div>
  <div class="brand-test">{{ cfg.test_title }}</div>
</div>
<main class="wrap{{ ' wide' if wide }}">
{% for cat, msg in get_flashed_messages(with_categories=true) %}
  <div class="flash {{ cat }}" role="status">{{ msg }}</div>
{% endfor %}
"""

LAYOUT_BOTTOM = """
</main>
<div class="foot no-print">{{ cfg.school_name }}</div>
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
<div class="split">
  <section class="intro">
    <span class="pill">{{ cfg.test_title }}</span>
    <h1>Welcome to your test</h1>
    <p class="lead" style="margin-bottom:0;">
      {% if n %}Answer {{ n }} multiple-choice question{{ '' if n == 1 else 's' }} in {{ cfg.minutes }} minutes and see your report card as soon as you finish.{% else %}Your teacher is still setting up the questions.{% endif %}
    </p>
    {% if n %}
    <div class="facts">
      <span>{{ icon('list') }}{{ n }} question{{ '' if n == 1 else 's' }}</span>
      <span>{{ icon('clock') }}{{ cfg.minutes }} minutes</span>
      <span>{{ icon('chart') }}Instant report card</span>
    </div>
    {% endif %}

    <svg class="omr" viewBox="0 0 340 168" role="img" aria-label="Illustration of an answer sheet">
      <rect x="1.5" y="1.5" width="337" height="165" rx="16" fill="#FFFFFF" stroke="#E4E8F3" stroke-width="2"/>
      <g font-family="Nunito, sans-serif" font-weight="800" font-size="13" text-anchor="middle">
      {% for row in [(44, 1, 1), (84, 2, 3), (124, 3, 0)] %}
        <text x="30" y="{{ row[0] + 5 }}" fill="#9AA1BA">{{ row[1] }}</text>
        {% for k in range(4) %}
        <circle cx="{{ 92 + 54 * k }}" cy="{{ row[0] }}" r="15" fill="{{ '#3B4EDB' if k == row[2] else '#FFFFFF' }}" stroke="{{ '#3B4EDB' if k == row[2] else '#E4E8F3' }}" stroke-width="2"/>
        <text x="{{ 92 + 54 * k }}" y="{{ row[0] + 5 }}" fill="{{ '#FFFFFF' if k == row[2] else '#5F6785' }}">{{ 'ABCD'[k] }}</text>
        {% endfor %}
      {% endfor %}
      </g>
    </svg>

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
      {% if cfg.track_focus %}<li><span>Stay on this page. Switching to another tab or app is recorded and your teacher can see it.</span></li>{% endif %}
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
      <div class="tb-meta">Class {{ student.student_class }}, {{ student.semester }}</div>
    </div>
    <div id="timer" class="timer" role="timer">{{ icon('clock') }}<span id="time-left">--:--</span></div>
  </div>
  <div class="track"><div class="bar" id="bar"></div></div>
  <div class="tb-count"><span><span id="answered">0</span> of {{ questions|length }} answered</span><span class="saved">{{ icon('check') }}Auto-saved</span></div>
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
    <button class="btn btn-ghost mark" type="button" id="mark-btn">{{ icon('flag') }}<span>Mark for review</span></button>
    <button class="btn next" type="button" id="next-btn">Next</button>
  </div>
  <p class="hint">Tip: press 1 to 4 to choose an option, and the arrow keys to move between questions.</p>
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
    markLabel.textContent = on ? 'Marked for review' : 'Mark for review';
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
    if (!warned1 && left <= 60 && left > 0 && INITIAL > 60) { warned1 = true; window.toast('Only 1 minute left', 'warn'); }
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
    else if (wasHidden) { wasHidden = false; window.toast('You left the test page. This is recorded for your teacher.', 'warn'); }
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
    {% if cmp %}
    <div class="cmp">
      <div class="cmp-top"><b>How you compare</b><span class="muted">{{ cmp.n }} students</span></div>
      <div class="cmp-track" aria-hidden="true"><i class="avg" style="left:{{ cmp.avg }}%;"></i><i class="you" style="left:{{ pct }}%;"></i></div>
      <div class="cmp-legend"><span><i class="k you"></i>You {{ pct }}%</span><span><i class="k avg"></i>Class average {{ cmp.avg }}%</span></div>
      {% if cmp.higher_than > 0 %}<p class="muted" style="margin:10px 0 0;">You scored higher than {{ cmp.higher_than }}% of students.</p>{% endif %}
    </div>
    {% endif %}
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
          {% if r.status != 'ok' and r.explanation %}<div class="rv-x">{{ r.explanation }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    <div class="btn-row no-print" style="margin-top:20px;">
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
    var colors = ['#3B4EDB', '#E8A200', '#1F9D55', '#D64545', '#7C8CFF'];
    for (var i = 0; i < 70; i++) {
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
<div class="narrow" style="max-width:440px;">
  <section class="card">
    <h1>Teacher login</h1>
    <p class="lead">Sign in to manage questions and see results.</p>
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
    <span class="pill {{ 'ok' if cfg.is_open else 'bad' }}">{{ 'Test is open' if cfg.is_open else 'Test is closed' }}</span>
  </div>
  <div class="btn-row">
    <a class="btn" href="{{ url_for('download_csv') }}">{{ icon('download') }}Download results (CSV)</a>
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
    <div><h2>Share the test link</h2><span class="muted">Send this to your students on WhatsApp or write it on the board.</span></div>
    <div class="share-row">
      <input type="text" id="share-url" readonly value="{{ share_url }}" aria-label="Test link">
      <button class="btn btn-ghost" type="button" id="copy-btn">{{ icon('link') }}Copy link</button>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/?text={{ (cfg.test_title ~ ': ' ~ share_url)|urlencode }}">WhatsApp</a>
    </div>
  </section>

  <div class="two">
    <section class="card">
      <h2>Score distribution</h2>
      <p class="muted" style="margin:0;">Number of students in each 10-point score range (%).</p>
      <div class="hist" role="img" aria-label="Score distribution chart">
        {% for b in hist %}
        <div class="hcol"><span>{{ b.count if b.count else '' }}</span><div class="hbar {{ 'zero' if not b.count }}" style="height:{{ b.height }}%;"></div><span class="hlab">{{ b.label }}</span></div>
        {% endfor %}
      </div>
      <div class="gchips">
        {% for g in grades %}<span>Grade {{ g.label }}: <b>{{ g.count }}</b></span>{% endfor %}
      </div>
    </section>
    <section class="card">
      <h2>Hardest questions</h2>
      <p class="muted" style="margin:0 0 8px;">Lowest share of students who got them right.</p>
      {% for it in hardest %}
      <div class="irow two-col">
        <div class="qt" title="{{ it.text }}">Q{{ it.number }}. {{ it.text }}</div><div class="pc">{{ it.pct }}%</div>
        {% if it.wrong_note %}<div class="sub">{{ it.wrong_note }}</div>{% endif %}
      </div>
      {% else %}
      <p class="muted" style="margin:14px 0 0;">Appears after the first submission.</p>
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
      {% if it.wrong_note %}<div class="sub">{{ it.wrong_note }}</div>{% endif %}
      {% else %}
      <div class="muted">No data yet</div><div></div>
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
        <label class="field"><span>Explanation <small>(optional, shown to students who miss it)</small></span>
          <textarea name="explanation" rows="2" maxlength="400" placeholder="Why is this the right answer?"></textarea>
        </label>
        <button class="btn btn-full" type="submit">Add question</button>
      </form>
    </section>

    <section class="card">
      <h2>Add many at once</h2>
      <p class="lead" style="margin-bottom:12px;">One question per line, parts separated by the | symbol. The explanation at the end is optional.</p>
      <code class="fmt">Question | Option A | Option B | Option C | Option D | b | Explanation</code>
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
      <div><h2>All questions ({{ questions|length }})</h2></div>
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
    <p class="muted" style="margin:8px 0 0;">No questions yet. Add your first one above.</p>
    {% endfor %}
    {% if questions %}<p class="muted" style="margin:14px 0 0;">Tip: on Render's free plan the database can reset when the app redeploys. Keep the backup file, then paste it into "Add many at once" to restore everything.</p>{% endif %}
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
          <td data-v="{{ r.score }}">{{ r.score }}/{{ r.total }}</td>
          <td data-v="{{ r.pct }}">{{ r.pct }}%</td>
          <td data-v="{{ r.grade }}">{{ r.grade }}</td>
          <td data-v="{{ r.time_taken if r.time_taken is not none else -1 }}">{{ r.time }}</td>
          <td data-v="{{ r.focus_lost or 0 }}" class="{{ 'warn' if (r.focus_lost or 0) >= 3 }}">{{ r.focus_lost if r.focus_lost is not none else '-' }}</td>
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
        <label class="field"><span>Message for students <small>(optional, shown before the test)</small></span>
          <textarea name="note" rows="3" maxlength="300" placeholder="e.g. Best of luck! No calculators.">{{ cfg.note }}</textarea>
        </label>
        <label class="check"><input type="checkbox" name="is_open" {{ 'checked' if cfg.is_open }}>
          <span><b>Test is open</b><small>Turn off to stop new students from starting.</small></span></label>
        <label class="check"><input type="checkbox" name="shuffle" {{ 'checked' if cfg.shuffle }}>
          <span><b>Shuffle question order</b><small>Each student sees a different order, so neighbours can't copy.</small></span></label>
        <label class="check"><input type="checkbox" name="allow_retake" {{ 'checked' if cfg.allow_retake }}>
          <span><b>Allow retakes</b><small>If off, the same name, class, semester and phone can submit only once.</small></span></label>
        <label class="check"><input type="checkbox" name="show_stats" {{ 'checked' if cfg.show_stats }}>
          <span><b>Show class comparison</b><small>Report card shows class average once 5 students have submitted.</small></span></label>
        <label class="check"><input type="checkbox" name="track_focus" {{ 'checked' if cfg.track_focus }}>
          <span><b>Record tab switches</b><small>Counts how often a student leaves the test page. Students are told about this.</small></span></label>
        <button class="btn btn-full" type="submit">Save settings</button>
      </form>
    </section>

    <section class="card">
      <h2>Danger zone</h2>
      <p class="lead" style="margin-bottom:16px;">These cannot be undone. Download the CSV and question backup first.</p>
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
        <div class="muted">Class {{ r.student_class }}, {{ r.semester }}, phone {{ r.phone }}</div>
      </div>
    </div>
    <div class="chips" style="margin-top:0;">
      <div class="chip"><b>{{ r.score }}/{{ r.total }}</b><small>Score</small></div>
      <div class="chip"><b>{{ pct }}%</b><small>Percentage</small></div>
      <div class="chip"><b>{{ grade }}</b><small>Grade</small></div>
      <div class="chip"><b>{{ time_text }}</b><small>Time taken</small></div>
      <div class="chip"><b>{{ r.focus_lost if r.focus_lost is not none else '-' }}</b><small>Tab switches</small></div>
    </div>
    <p class="muted" style="margin:14px 0 0;">Submitted {{ r.submitted_at }}</p>
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
      <label class="field"><span>Explanation <small>(optional)</small></span>
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
        flash("Please fill in the question, all four options and the correct answer.", "error")
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
                flash("Please fill in the question, all four options and the correct answer.", "error")
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
        flash("Skipped line(s) " + ", ".join(bad_lines) + ". Each line needs: question, 4 options, correct letter.", "error")
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
