"""
Student Test Portal - single-file Flask app (SQLite + HTML/CSS/JS).

Student flow : details form -> timed MCQ test -> instant report card
Admin flow   : /admin -> add / delete questions, view results, download CSV

Environment variables (set these on Render):
    SECRET_KEY       long random string (keeps login sessions secure)
    ADMIN_PASSWORD   your admin password (default: admin123 - CHANGE IT)
    TEST_MINUTES     time limit in minutes (default: 15)
    DB_PATH          path of the SQLite file (default: ./test_app.db)

Run locally :  python app.py
Run on Render: gunicorn app:app
"""
import csv
import hmac
import io
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    Response,
    flash,
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
TEST_MINUTES = int(os.environ.get("TEST_MINUTES", "15"))
IST = timezone(timedelta(hours=5, minutes=30))  # timestamps are saved in Indian time

OPTION_KEYS = {"a": "option_a", "b": "option_b", "c": "option_c", "d": "option_d"}


# ----------------------------------------------------------------------
# DATABASE
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
                submitted_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


init_db()


# ----------------------------------------------------------------------
# SHARED LAYOUT + STYLE (white background, responsive, one font family)
# ----------------------------------------------------------------------
LAYOUT_TOP = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Student Test Portal</title>
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
.brandbar{max-width:720px;margin:0 auto;padding:22px 20px 0;}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:1.05rem;}
.mark{
  width:36px;height:36px;border-radius:11px;background:var(--brand);
  display:grid;place-items:center;box-shadow:0 0 0 4px var(--brand-tint);
}
.wrap{max-width:720px;margin:0 auto;padding:22px 20px 56px;}

/* typography */
h1{font-size:1.75rem;line-height:1.2;margin:0 0 8px;font-weight:800;letter-spacing:-0.01em;}
h2{font-size:1.15rem;margin:0 0 4px;font-weight:800;}
.lead{color:var(--muted);margin:0 0 22px;}
.muted{color:var(--muted);font-size:.9rem;}

/* cards */
.card{border:1px solid var(--line);border-radius:var(--radius);padding:28px;margin-bottom:20px;background:#fff;}
.pill{display:inline-block;padding:4px 12px;border-radius:999px;background:var(--brand-tint);color:var(--brand-dark);font-weight:700;font-size:.85rem;}
.facts{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px;}

/* forms */
.field{display:block;margin-bottom:16px;}
.field > span{display:block;font-weight:700;font-size:.9rem;margin-bottom:6px;}
input[type=text],input[type=tel],input[type=password],select{
  width:100%;padding:13px 14px;border:1.5px solid var(--line);border-radius:12px;
  font:inherit;font-size:16px;color:var(--ink);background:#fff;outline:none;
  transition:border-color .15s, box-shadow .15s;
}
input::placeholder{color:#9AA1BA;}
input:focus,select:focus{border-color:var(--brand);box-shadow:0 0 0 4px var(--brand-tint);}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px;}

/* buttons */
.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:13px 22px;border-radius:12px;border:1.5px solid var(--brand);
  background:var(--brand);color:#fff;font:inherit;font-weight:800;cursor:pointer;
  text-decoration:none;transition:background .15s, transform .1s;
}
.btn:hover{background:var(--brand-dark);border-color:var(--brand-dark);}
.btn:active{transform:translateY(1px);}
.btn:focus-visible{outline:3px solid var(--brand);outline-offset:2px;}
.btn-full{width:100%;margin-top:6px;}
.btn-ghost{background:#fff;color:var(--ink);border-color:var(--line);}
.btn-ghost:hover{background:var(--soft);border-color:var(--line);}
.btn-danger{background:#fff;color:var(--err);border-color:#F1B5B5;}
.btn-danger:hover{background:var(--err-tint);border-color:#F1B5B5;}
.btn-sm{padding:8px 14px;font-size:.875rem;border-radius:10px;}
.btn-row{display:flex;gap:10px;flex-wrap:wrap;}
.btn:disabled{opacity:.6;cursor:not-allowed;}

/* flash messages */
.flash{padding:12px 16px;border-radius:12px;margin-bottom:18px;font-weight:600;font-size:.95rem;background:var(--brand-tint);color:var(--brand-dark);}
.flash.error{background:var(--err-tint);color:var(--err);}
.flash.success{background:var(--ok-tint);color:var(--ok);}

/* test page */
.testbar{
  position:sticky;top:0;z-index:10;background:#fff;
  margin:0 -20px 20px;padding:12px 20px 10px;border-bottom:1px solid var(--line);
}
.tb-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.tb-name{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46vw;}
.tb-meta{font-size:.82rem;color:var(--muted);}
.timer{
  font-weight:800;font-variant-numeric:tabular-nums;white-space:nowrap;
  background:var(--brand-tint);color:var(--brand-dark);padding:7px 14px;border-radius:999px;
}
.timer.low{background:var(--err-tint);color:var(--err);}
.track{height:6px;background:var(--line);border-radius:999px;overflow:hidden;margin-top:10px;}
.bar{height:100%;width:0;background:var(--brand);border-radius:999px;transition:width .25s;}
.tb-count{font-size:.8rem;color:var(--muted);margin-top:6px;font-weight:600;}

.q{border:1px solid var(--line);border-radius:var(--radius);padding:20px;margin-bottom:16px;}
.q-head{display:flex;gap:12px;margin-bottom:14px;}
.q-num{
  flex:none;width:32px;height:32px;border-radius:50%;background:var(--ink);color:#fff;
  display:grid;place-items:center;font-weight:800;font-size:.9rem;
}
.q-text{font-weight:700;font-size:1.05rem;line-height:1.4;padding-top:3px;}
.opt{
  position:relative;display:flex;align-items:center;gap:12px;padding:12px 14px;
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

/* report card */
.hero{text-align:center;}
.ring{
  --pct:0;width:156px;height:156px;border-radius:50%;margin:22px auto 16px;
  background:conic-gradient(var(--brand) calc(var(--pct) * 1%), var(--line) 0);
  display:grid;place-items:center;
}
.ring span{
  width:124px;height:124px;border-radius:50%;background:#fff;
  display:grid;place-items:center;font-size:2.1rem;font-weight:800;color:var(--brand-dark);
}
.grade{display:inline-block;padding:6px 18px;border-radius:999px;background:var(--brand);color:#fff;font-weight:800;margin:6px 0 10px;}
.chips{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:22px;}
.chip{border-radius:12px;padding:12px 8px;background:var(--soft);}
.chip b{display:block;font-size:1.4rem;font-weight:800;}
.chip small{color:var(--muted);font-weight:600;}
.chip.ok b{color:var(--ok);} .chip.bad b{color:var(--err);}
.rv{display:flex;gap:12px;padding:14px 0;border-bottom:1px solid var(--line);}
.rv:last-of-type{border-bottom:none;}
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

/* admin */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin-bottom:22px;}
.stat{background:var(--soft);border-radius:12px;padding:14px 16px;}
.stat b{display:block;font-size:1.6rem;font-weight:800;color:var(--brand);line-height:1.2;}
.stat span{font-size:.85rem;color:var(--muted);font-weight:600;}
.qrow{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;padding:16px 0;border-bottom:1px solid var(--line);}
.qrow:last-child{border-bottom:none;}
.qrow .opts{font-size:.9rem;color:var(--muted);margin-top:4px;}
.qrow .opts .right{color:var(--ok);font-weight:800;}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table{width:100%;border-collapse:collapse;font-size:.92rem;min-width:520px;}
th{text-align:left;color:var(--muted);font-weight:700;font-size:.82rem;padding:8px 10px;border-bottom:1.5px solid var(--line);white-space:nowrap;}
td{padding:11px 10px;border-bottom:1px solid var(--line);white-space:nowrap;}
.small-link{display:block;text-align:center;margin-top:8px;font-size:.9rem;}

@media (max-width:560px){
  .grid2{grid-template-columns:1fr;}
  .card{padding:22px 18px;}
  h1{font-size:1.5rem;}
  .btn-row .btn{width:100%;}
  .qrow{flex-direction:column;}
  .tb-name{max-width:40vw;}
}
@media print{
  .no-print{display:none !important;}
  .brandbar{padding-top:0;}
  .card{border-color:#ccc;}
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
    Student Test Portal
  </div>
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


def evaluate(questions, answers):
    """Return (score, review_list) for the given answers {question_id: 'a'..'d'}."""
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


def grade_for(pct):
    if pct >= 90:
        return "A+", "Outstanding work! Keep it up."
    if pct >= 75:
        return "A", "Great job! You did really well."
    if pct >= 60:
        return "B", "Good effort. A little more practice will help."
    if pct >= 40:
        return "C", "You're on the right track. Keep practising."
    return "D", "Don't worry. Review the topics and you'll do better next time."


def csv_safe(value):
    """Stop Excel from running text like '=SUM(...)' typed by a student as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


# ----------------------------------------------------------------------
# STUDENT FLOW
# ----------------------------------------------------------------------
HOME_TEMPLATE = """
<section class="card">
  <h1>Ready for your test?</h1>
  <p class="lead">Enter your details below. The timer starts when you press Start test.</p>

  {% if no_questions %}
    <div class="flash error">The test isn't ready yet. Please ask your teacher.</div>
  {% else %}
    <div class="facts">
      <span class="pill">{{ n }} questions</span>
      <span class="pill">{{ minutes }} minutes</span>
    </div>
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
      <button class="btn btn-full" type="submit">Start test</button>
    </form>
  {% endif %}
</section>
<a class="small-link muted" href="{{ url_for('admin_login') }}">Teacher login</a>
"""


@app.route("/")
def home():
    with closing(get_db()) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    return render_template_string(
        page(HOME_TEMPLATE), no_questions=(count == 0), n=count, minutes=TEST_MINUTES
    )


@app.route("/start", methods=["POST"])
def start_test():
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
        count = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    if count == 0:
        return redirect(url_for("home"))

    session["student"] = {
        "name": name,
        "student_class": student_class,
        "semester": semester,
        "phone": phone,
    }
    session["submitted"] = False
    session["deadline"] = int(time.time()) + TEST_MINUTES * 60
    session.pop("answers", None)
    return redirect(url_for("test_page"))


TEST_TEMPLATE = """
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

<form method="POST" action="{{ url_for('submit_test') }}" id="quiz-form">
  {% for q in questions %}
  <section class="q">
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
  <button class="btn btn-full" type="submit" id="submit-btn">Submit test</button>
</form>

<script>
(function () {
  var form = document.getElementById('quiz-form');
  var timeEl = document.getElementById('time-left');
  var timerEl = document.getElementById('timer');
  var bar = document.getElementById('bar');
  var countEl = document.getElementById('answered');
  var btn = document.getElementById('submit-btn');
  var total = {{ questions|length }};
  var endAt = Date.now() + {{ remaining }} * 1000;
  var sent = false;

  function answeredCount() {
    return form.querySelectorAll('input[type=radio]:checked').length;
  }
  function updateProgress() {
    var n = answeredCount();
    countEl.textContent = n;
    bar.style.width = (total ? (n / total) * 100 : 0) + '%';
  }
  function send() {
    if (sent) return;
    sent = true;
    btn.disabled = true;
    btn.textContent = 'Submitting...';
    form.submit();
  }
  function tick() {
    var left = Math.max(0, Math.round((endAt - Date.now()) / 1000));
    var m = String(Math.floor(left / 60)).padStart(2, '0');
    var s = String(left % 60).padStart(2, '0');
    timeEl.textContent = m + ':' + s;
    if (left <= 60) timerEl.classList.add('low');
    if (left === 0) { clearInterval(iv); send(); }
  }

  form.addEventListener('change', updateProgress);
  form.addEventListener('submit', function (e) {
    if (sent) { e.preventDefault(); return; }
    var missing = total - answeredCount();
    if (missing > 0 && !confirm('You have ' + missing + ' unanswered question(s). Submit anyway?')) {
      e.preventDefault();
      return;
    }
    sent = true;
    btn.disabled = true;
    btn.textContent = 'Submitting...';
  });

  var iv = setInterval(tick, 500);
  tick();
  updateProgress();
})();
</script>
"""


@app.route("/test")
def test_page():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    if not questions:
        return redirect(url_for("home"))
    remaining = max(0, int(session.get("deadline", 0)) - int(time.time()))
    return render_template_string(
        page(TEST_TEMPLATE), student=session["student"], questions=questions, remaining=remaining
    )


@app.route("/submit", methods=["POST"])
def submit_test():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))

    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

        answers = {}
        for q in questions:
            value = request.form.get(f"q_{q['id']}")
            if value in OPTION_KEYS:
                answers[str(q["id"])] = value

        score, _ = evaluate(questions, answers)
        student = session["student"]
        conn.execute(
            "INSERT INTO results (student_name, student_class, semester, phone, score, total, submitted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                student["name"],
                student["student_class"],
                student["semester"],
                student["phone"],
                score,
                len(questions),
                now_str(),
            ),
        )
        conn.commit()

    session["answers"] = answers
    session["submitted"] = True
    return redirect(url_for("report"))


REPORT_TEMPLATE = """
<section class="card hero">
  <h1>Report card</h1>
  <p class="lead" style="margin-bottom:0;">{{ student.name }}<br>Class {{ student.student_class }}, {{ student.semester }}</p>
  <div class="ring" style="--pct:{{ pct }};"><span>{{ pct }}%</span></div>
  <h2>{{ score }} out of {{ total }} correct</h2>
  <div class="grade">Grade {{ grade }}</div>
  <p class="muted" style="margin:0;">{{ message }}</p>
  <div class="chips">
    <div class="chip ok"><b>{{ score }}</b><small>Correct</small></div>
    <div class="chip bad"><b>{{ wrong }}</b><small>Wrong</small></div>
    <div class="chip"><b>{{ skipped }}</b><small>Skipped</small></div>
  </div>
</section>

<section class="card">
  <h2 style="margin-bottom:8px;">Answer review</h2>
  {% for r in review %}
  <div class="rv">
    <div class="rv-mark {{ r.status }}">{% if r.status == 'ok' %}&#10003;{% elif r.status == 'bad' %}&#10005;{% else %}&ndash;{% endif %}</div>
    <div>
      <div class="rv-q">{{ r.number }}. {{ r.question }}</div>
      <div class="rv-a">Your answer: <b>{{ r.your }}</b></div>
      {% if r.status != 'ok' %}<div class="rv-a">Correct answer: <b class="good">{{ r.correct }}</b></div>{% endif %}
    </div>
  </div>
  {% endfor %}
  <div class="btn-row no-print" style="margin-top:20px;">
    <button class="btn" type="button" onclick="window.print()">Print or save as PDF</button>
    <a class="btn btn-ghost" href="{{ url_for('home') }}">Back to start</a>
  </div>
</section>
"""


@app.route("/report")
def report():
    if "student" not in session or not session.get("submitted"):
        return redirect(url_for("home"))

    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

    score, review = evaluate(questions, session.get("answers", {}))
    total = len(questions)
    pct = round((score / total) * 100) if total else 0
    grade, message = grade_for(pct)
    skipped = sum(1 for r in review if r["status"] == "skip")
    wrong = sum(1 for r in review if r["status"] == "bad")

    return render_template_string(
        page(REPORT_TEMPLATE),
        student=session["student"],
        score=score,
        total=total,
        pct=pct,
        grade=grade,
        message=message,
        review=review,
        wrong=wrong,
        skipped=skipped,
    )


# ----------------------------------------------------------------------
# ADMIN
# ----------------------------------------------------------------------
def admin_required():
    return session.get("is_admin", False)


LOGIN_TEMPLATE = """
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
<section class="card">
  <h1>Dashboard</h1>
  <p class="lead">Manage questions and review submitted results.</p>

  <div class="stats">
    <div class="stat"><b>{{ questions|length }}</b><span>Questions</span></div>
    <div class="stat"><b>{{ stats.count }}</b><span>Submissions</span></div>
    <div class="stat"><b>{{ stats.avg }}%</b><span>Average score</span></div>
    <div class="stat"><b>{{ stats.best }}%</b><span>Highest score</span></div>
  </div>

  <div class="btn-row">
    <a class="btn" href="{{ url_for('download_csv') }}">Download results (CSV)</a>
    <a class="btn btn-ghost" href="{{ url_for('home') }}" target="_blank" rel="noopener">Open student page</a>
    <a class="btn btn-ghost" href="{{ url_for('admin_logout') }}">Log out</a>
  </div>
</section>

<section class="card">
  <h2>Add a question</h2>
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
        <option value="a">A</option>
        <option value="b">B</option>
        <option value="c">C</option>
        <option value="d">D</option>
      </select>
    </label>
    <button class="btn btn-full" type="submit">Add question</button>
  </form>
</section>

<section class="card">
  <h2>Questions ({{ questions|length }})</h2>
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

<section class="card">
  <h2>Recent results</h2>
  <p class="muted" style="margin:0 0 10px;">Showing the latest {{ results|length }}. Download the CSV for everything.</p>
  {% if results %}
  <div class="tscroll">
    <table>
      <tr><th>Name</th><th>Class</th><th>Semester</th><th>Phone</th><th>Score</th><th>%</th><th>Submitted</th></tr>
      {% for r in results %}
      <tr>
        <td>{{ r.student_name }}</td><td>{{ r.student_class }}</td><td>{{ r.semester }}</td>
        <td>{{ r.phone }}</td><td>{{ r.score }}/{{ r.total }}</td><td>{{ r.pct }}%</td><td>{{ r.submitted_at }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% else %}
  <p class="muted" style="margin:0;">No submissions yet.</p>
  {% endif %}
</section>

<section class="card">
  <h2>Reset results</h2>
  <p class="lead" style="margin-bottom:16px;">Deletes every submitted result. Download the CSV first if you need it.</p>
  <form method="POST" action="{{ url_for('reset_results') }}" onsubmit="return confirm('All results will be deleted permanently. Continue?');">
    <button class="btn btn-danger" type="submit">Delete all results</button>
  </form>
</section>
"""


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
        rows = conn.execute("SELECT * FROM results ORDER BY id DESC LIMIT 100").fetchall()
        agg = conn.execute(
            "SELECT COUNT(*) c, AVG(score * 100.0 / total) a, MAX(score * 100.0 / total) m "
            "FROM results WHERE total > 0"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) c FROM results").fetchone()["c"]

    results = []
    for r in rows:
        d = dict(r)
        d["pct"] = round((r["score"] / r["total"]) * 100) if r["total"] else 0
        results.append(d)

    stats = {
        "count": count,
        "avg": round(agg["a"]) if agg["a"] is not None else 0,
        "best": round(agg["m"]) if agg["m"] is not None else 0,
    }
    return render_template_string(
        page(DASHBOARD_TEMPLATE), questions=questions, results=results, stats=stats
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
        return redirect(url_for("admin_dashboard"))

    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (text, *options, correct),
        )
        conn.commit()
    flash("Question added.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_question/<int:qid>", methods=["POST"])
def delete_question(qid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
        conn.commit()
    flash("Question deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset_results", methods=["POST"])
def reset_results():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM results")
        conn.commit()
    flash("All results deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/download_csv")
def download_csv():
    if not admin_required():
        return redirect(url_for("admin_login"))
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM results ORDER BY id").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Class", "Semester", "Phone", "Score", "Total", "Percentage", "Submitted At"])
    for r in rows:
        pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
        writer.writerow(
            [
                csv_safe(r["student_name"]),
                csv_safe(r["student_class"]),
                csv_safe(r["semester"]),
                r["phone"],
                r["score"],
                r["total"],
                pct,
                r["submitted_at"],
            ]
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
