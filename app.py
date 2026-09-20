import os
import sqlite3
import io
import csv
from datetime import datetime

from flask import Flask, request, redirect, url_for, render_template_string, session, Response

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kids-test-app-secret-change-me")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "test_app.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


# ----------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
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
    conn.close()


init_db()


# ----------------------------------------------------------------------
# SHARED STYLE (kid-friendly, responsive) - plain CSS, no Jinja braces
# ----------------------------------------------------------------------
STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Baloo+2:wght@500;600;700;800&display=swap');

:root{
  --primary:#6C5CE7;
  --primary-dark:#5546c9;
  --secondary:#00CEC9;
  --accent:#FDCB6E;
  --good:#00B894;
  --bad:#FF6B6B;
  --bg1:#a29bfe;
  --bg2:#74ebd5;
  --card:#ffffff;
  --text:#2d3436;
}
*{box-sizing:border-box;}
body{
  margin:0;
  min-height:100vh;
  font-family:'Baloo 2', system-ui, sans-serif;
  background:linear-gradient(135deg,var(--bg1),var(--bg2));
  background-attachment:fixed;
  color:var(--text);
  display:flex;
  justify-content:center;
  padding:24px 12px;
}
.wrap{width:100%;max-width:720px;}
.card{
  background:var(--card);
  border-radius:24px;
  padding:28px 26px;
  box-shadow:0 14px 34px rgba(0,0,0,0.18);
  margin-bottom:20px;
  animation:pop .35s ease;
}
@keyframes pop{from{transform:scale(.96);opacity:0}to{transform:scale(1);opacity:1}}
h1{font-size:1.7rem;margin-top:0;color:var(--primary-dark);}
h2{color:var(--primary-dark);}
p.sub{color:#636e72;margin-top:-8px;}
label{display:block;font-weight:600;margin:14px 0 6px;}
input[type=text], input[type=tel], select{
  width:100%;
  padding:12px 14px;
  border-radius:14px;
  border:2px solid #dfe6e9;
  font-family:inherit;
  font-size:1rem;
  outline:none;
  transition:.2s;
}
input:focus, select:focus{border-color:var(--primary);}
.btn{
  display:inline-block;
  border:none;
  cursor:pointer;
  padding:13px 26px;
  border-radius:16px;
  font-family:inherit;
  font-weight:700;
  font-size:1rem;
  color:#fff;
  background:var(--primary);
  transition:transform .15s, box-shadow .15s;
  box-shadow:0 6px 0 var(--primary-dark);
}
.btn:active{transform:translateY(4px);box-shadow:0 2px 0 var(--primary-dark);}
.btn-block{width:100%;margin-top:18px;text-align:center;}
.btn-secondary{background:var(--secondary);box-shadow:0 6px 0 #00a39f;}
.btn-danger{background:var(--bad);box-shadow:0 6px 0 #c0392b;}
.btn-accent{background:var(--accent);color:#7a5b00;box-shadow:0 6px 0 #caa136;}
.emoji{font-size:2.2rem;}
.q-block{
  border:2px solid #f1f2f6;
  border-radius:16px;
  padding:16px 16px 6px;
  margin-bottom:16px;
}
.q-title{font-weight:700;margin-bottom:10px;}
.opt{
  display:block;
  background:#f8f9fe;
  border:2px solid #eef0fb;
  border-radius:12px;
  padding:10px 14px;
  margin-bottom:10px;
  cursor:pointer;
  transition:.15s;
}
.opt:hover{border-color:var(--primary);}
.opt input{margin-right:10px;}
#timer{
  position:sticky;top:10px;
  background:var(--accent);
  color:#7a5b00;
  font-weight:800;
  padding:10px 16px;
  border-radius:14px;
  text-align:center;
  margin-bottom:16px;
  box-shadow:0 6px 0 #caa136;
}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:.95rem;}
th,td{padding:8px 6px;border-bottom:1px solid #eee;text-align:left;}
.tag{padding:3px 10px;border-radius:20px;color:#fff;font-weight:700;font-size:.8rem;}
.tag-good{background:var(--good);}
.tag-bad{background:var(--bad);}
.score-circle{
  width:150px;height:150px;border-radius:50%;
  background:conic-gradient(var(--good) calc(var(--pct)*1%), #eee 0);
  display:flex;align-items:center;justify-content:center;
  margin:14px auto;font-size:1.6rem;font-weight:800;color:var(--primary-dark);
}
.score-circle span{background:#fff;width:118px;height:118px;border-radius:50%;display:flex;align-items:center;justify-content:center;}
.flash{background:#fff3cd;border:2px solid #ffe08a;padding:10px 14px;border-radius:12px;margin-bottom:14px;font-weight:600;}
.admin-row{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #eee;padding:10px 0;gap:10px;flex-wrap:wrap;}
.small-link{font-size:.85rem;color:#636e72;text-align:center;display:block;margin-top:10px;}
.small-link a{color:var(--primary-dark);}
@media print{ .no-print{display:none;} body{background:#fff;padding:0;} .card{box-shadow:none;} }
@media (max-width:480px){ h1{font-size:1.4rem;} .card{padding:20px 16px;border-radius:18px;} }
</style>
"""


def page(body_html):
    return STYLE + body_html


# ----------------------------------------------------------------------
# STUDENT FLOW
# ----------------------------------------------------------------------
HOME_TEMPLATE = """
<div class="wrap">
  <div class="card">
    <div style="text-align:center;">
      <div class="emoji">📝✨</div>
      <h1>Chalo Test Shuru Karein!</h1>
      <p class="sub">Apni details bharo aur test start karo</p>
    </div>

    {% if flash_msg %}<div class="flash">{{ flash_msg }}</div>{% endif %}

    {% if no_questions %}
      <div class="flash">⚠️ Abhi test taiyar nahi hai. Teacher se contact karein.</div>
    {% else %}
    <form method="POST" action="{{ url_for('start_test') }}">
      <label>👦 Naam (Name)</label>
      <input type="text" name="name" required placeholder="Apna naam likho">

      <label>🏫 Class</label>
      <input type="text" name="student_class" required placeholder="Jaise: 6th, 10-A">

      <label>📚 Semester / Term</label>
      <input type="text" name="semester" required placeholder="Jaise: Semester 1">

      <label>📱 Phone Number</label>
      <input type="tel" name="phone" required pattern="[0-9]{10}" maxlength="10" placeholder="10 digit number">

      <button class="btn btn-block" type="submit">Test Shuru Karo 🚀</button>
    </form>
    {% endif %}
    <span class="small-link"><a href="{{ url_for('admin_login') }}">Admin Login</a></span>
  </div>
</div>
"""


@app.route("/")
def home():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
    conn.close()
    return render_template_string(
        page(HOME_TEMPLATE), no_questions=(count == 0), flash_msg=session.pop("flash_msg", None)
    )


@app.route("/start", methods=["POST"])
def start_test():
    name = request.form.get("name", "").strip()
    student_class = request.form.get("student_class", "").strip()
    semester = request.form.get("semester", "").strip()
    phone = request.form.get("phone", "").strip()

    if not (name and student_class and semester and phone):
        session["flash_msg"] = "Please fill all the fields!"
        return redirect(url_for("home"))

    session["student"] = {
        "name": name,
        "student_class": student_class,
        "semester": semester,
        "phone": phone,
    }
    session["submitted"] = False
    return redirect(url_for("test_page"))


TEST_TEMPLATE = """
<div class="wrap">
  <div id="timer">⏳ Time Left: <span id="time-left">15:00</span></div>
  <div class="card">
    <h1>Hello {{ student.name }} 👋</h1>
    <p class="sub">Class: {{ student.student_class }} | {{ student.semester }}</p>

    <form method="POST" action="{{ url_for('submit_test') }}" id="quiz-form">
      {% for q in questions %}
      <div class="q-block">
        <div class="q-title">Q{{ loop.index }}. {{ q.question_text }}</div>
        <label class="opt"><input type="radio" name="q_{{ q.id }}" value="a" required> {{ q.option_a }}</label>
        <label class="opt"><input type="radio" name="q_{{ q.id }}" value="b"> {{ q.option_b }}</label>
        <label class="opt"><input type="radio" name="q_{{ q.id }}" value="c"> {{ q.option_c }}</label>
        <label class="opt"><input type="radio" name="q_{{ q.id }}" value="d"> {{ q.option_d }}</label>
      </div>
      {% endfor %}
      <button class="btn btn-block" type="submit">Submit Test ✅</button>
    </form>
  </div>
</div>

<script>
let seconds = 15 * 60;
const timeEl = document.getElementById('time-left');
const timer = setInterval(() => {
  seconds--;
  if (seconds <= 0) {
    clearInterval(timer);
    document.getElementById('quiz-form').submit();
    return;
  }
  const m = Math.floor(seconds / 60).toString().padStart(2, '0');
  const s = (seconds % 60).toString().padStart(2, '0');
  timeEl.textContent = m + ':' + s;
}, 1000);
</script>
"""


@app.route("/test")
def test_page():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))
    conn = get_db()
    questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    conn.close()
    if not questions:
        return redirect(url_for("home"))
    return render_template_string(page(TEST_TEMPLATE), student=session["student"], questions=questions)


REPORT_TEMPLATE = """
<div class="wrap">
  <div class="card" style="text-align:center;">
    <div class="emoji">🎉</div>
    <h1>Report Card</h1>
    <p class="sub">{{ student.name }} | Class {{ student.student_class }} | {{ student.semester }}</p>
    <div class="score-circle" style="--pct:{{ pct }};"><span>{{ pct }}%</span></div>
    <h2>{{ score }} / {{ total }} Correct</h2>
    <h2>Grade: {{ grade }}</h2>
    <p>{{ message }}</p>
  </div>

  <div class="card">
    <h2>📋 Answer Review</h2>
    <table>
      <tr><th>Q#</th><th>Your Answer</th><th>Correct Answer</th><th>Result</th></tr>
      {% for r in review %}
      <tr>
        <td>{{ loop.index }}</td>
        <td>{{ r.your }}</td>
        <td>{{ r.correct }}</td>
        <td>{% if r.is_correct %}<span class="tag tag-good">Correct</span>{% else %}<span class="tag tag-bad">Wrong</span>{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
    <button class="btn btn-secondary btn-block no-print" onclick="window.print()">🖨️ Print / Save as PDF</button>
    <a href="{{ url_for('home') }}"><button class="btn btn-accent btn-block no-print">🏠 Back to Home</button></a>
  </div>
</div>
"""


@app.route("/submit", methods=["POST"])
def submit_test():
    if "student" not in session or session.get("submitted"):
        return redirect(url_for("home"))

    conn = get_db()
    questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()

    score = 0
    review = []
    option_text_map = {"a": "option_a", "b": "option_b", "c": "option_c", "d": "option_d"}

    for q in questions:
        chosen = request.form.get(f"q_{q['id']}")
        is_correct = chosen == q["correct_option"]
        if is_correct:
            score += 1
        review.append(
            {
                "your": q[option_text_map[chosen]] if chosen in option_text_map else "Not answered",
                "correct": q[option_text_map[q["correct_option"]]],
                "is_correct": is_correct,
            }
        )

    total = len(questions)
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
            total,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()

    session["submitted"] = True

    pct = round((score / total) * 100) if total else 0
    if pct >= 90:
        grade, message = "A+", "Wah! Bahut shानdaar performance! 🌟"
    elif pct >= 75:
        grade, message = "A", "Great job! Bahut accha kiya! 👏"
    elif pct >= 60:
        grade, message = "B", "Accha kiya! Aur mehnat karo. 💪"
    elif pct >= 40:
        grade, message = "C", "Thik hai, thoda aur practice karo. 📚"
    else:
        grade, message = "D", "Chinta mat karo, agli baar aur accha karoge! 🙂"

    return render_template_string(
        page(REPORT_TEMPLATE),
        student=student,
        score=score,
        total=total,
        pct=pct,
        grade=grade,
        message=message,
        review=review,
    )


# ----------------------------------------------------------------------
# ADMIN
# ----------------------------------------------------------------------
def admin_required():
    return session.get("is_admin", False)


LOGIN_TEMPLATE = """
<div class="wrap">
  <div class="card">
    <h1>🔐 Admin Login</h1>
    {% if error %}<div class="flash">{{ error }}</div>{% endif %}
    <form method="POST">
      <label>Password</label>
      <input type="text" name="password" required>
      <button class="btn btn-block" type="submit">Login</button>
    </form>
    <span class="small-link"><a href="{{ url_for('home') }}">⬅ Back to Test</a></span>
  </div>
</div>
"""


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if admin_required():
        return redirect(url_for("admin_dashboard"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Galat password!"
    return render_template_string(page(LOGIN_TEMPLATE), error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


DASHBOARD_TEMPLATE = """
<div class="wrap">
  <div class="card">
    <h1>👩‍🏫 Admin Dashboard</h1>
    <p class="sub">Total Questions: {{ questions|length }} | Total Submissions: {{ results_count }}</p>
    <a href="{{ url_for('download_csv') }}"><button class="btn btn-secondary">⬇️ Download Results CSV</button></a>
    <a href="{{ url_for('reset_results') }}" onclick="return confirm('Sab results delete ho jayenge. Sure?');"><button class="btn btn-danger">🗑️ Reset All Results</button></a>
    <a href="{{ url_for('admin_logout') }}"><button class="btn btn-accent">🚪 Logout</button></a>
  </div>

  <div class="card">
    <h2>➕ Add Question</h2>
    <form method="POST" action="{{ url_for('add_question') }}">
      <label>Question</label>
      <input type="text" name="question_text" required>
      <label>Option A</label>
      <input type="text" name="option_a" required>
      <label>Option B</label>
      <input type="text" name="option_b" required>
      <label>Option C</label>
      <input type="text" name="option_c" required>
      <label>Option D</label>
      <input type="text" name="option_d" required>
      <label>Correct Option</label>
      <select name="correct_option" required>
        <option value="a">A</option>
        <option value="b">B</option>
        <option value="c">C</option>
        <option value="d">D</option>
      </select>
      <button class="btn btn-block" type="submit">Add Question</button>
    </form>
  </div>

  <div class="card">
    <h2>📚 Existing Questions</h2>
    {% for q in questions %}
    <div class="admin-row">
      <div>
        <b>Q{{ loop.index }}.</b> {{ q.question_text }}
        <br><span class="sub">Correct: {{ q.correct_option|upper }}</span>
      </div>
      <a href="{{ url_for('delete_question', qid=q.id) }}" onclick="return confirm('Delete this question?');"><button class="btn btn-danger">Delete</button></a>
    </div>
    {% else %}
    <p>Koi question nahi hai abhi. Upar se add karo.</p>
    {% endfor %}
  </div>
</div>
"""


@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))
    conn = get_db()
    questions = conn.execute("SELECT * FROM questions ORDER BY id").fetchall()
    results_count = conn.execute("SELECT COUNT(*) c FROM results").fetchone()["c"]
    conn.close()
    return render_template_string(page(DASHBOARD_TEMPLATE), questions=questions, results_count=results_count)


@app.route("/admin/add_question", methods=["POST"])
def add_question():
    if not admin_required():
        return redirect(url_for("admin_login"))
    conn = get_db()
    conn.execute(
        "INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            request.form["question_text"].strip(),
            request.form["option_a"].strip(),
            request.form["option_b"].strip(),
            request.form["option_c"].strip(),
            request.form["option_d"].strip(),
            request.form["correct_option"],
        ),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_question/<int:qid>")
def delete_question(qid):
    if not admin_required():
        return redirect(url_for("admin_login"))
    conn = get_db()
    conn.execute("DELETE FROM questions WHERE id = ?", (qid,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset_results")
def reset_results():
    if not admin_required():
        return redirect(url_for("admin_login"))
    conn = get_db()
    conn.execute("DELETE FROM results")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/download_csv")
def download_csv():
    if not admin_required():
        return redirect(url_for("admin_login"))
    conn = get_db()
    rows = conn.execute("SELECT * FROM results ORDER BY id").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Class", "Semester", "Phone", "Score", "Total", "Percentage", "Submitted At"])
    for r in rows:
        pct = round((r["score"] / r["total"]) * 100) if r["total"] else 0
        writer.writerow(
            [r["student_name"], r["student_class"], r["semester"], r["phone"], r["score"], r["total"], pct, r["submitted_at"]]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=test_results.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
