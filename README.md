# 🎯 Student Test App (Flask + SQLite)

Ek lightweight, single-file quiz app — bacchon ke liye test lene ke liye.

## Features
- Student pehle apna Naam, Class, Semester, Phone bharta hai
- Fir 10 (ya jitne bhi tum add karo) MCQ questions ka test deta hai
- Submit karte hi turant Report Card (score, %, grade, answer review) dikhta hai
- Har submission SQLite database mein save hoti hai
- Admin panel se tum questions add/delete kar sakte ho
- Admin panel se ek click mein sab results CSV file mein download kar sakte ho (Excel mein khol kar analysis)
- Mobile/tablet/laptop sab pe responsive, colorful aur kids-friendly design

## Files
- `app.py` — poora app (backend + frontend + DB), ek hi file mein
- `requirements.txt` — Python dependencies
- `Procfile` — Render/Heroku deployment ke liye

## Local pe chalane ke liye
```bash
pip install -r requirements.txt
python app.py
```
Browser mein kholo: http://localhost:5000

Admin panel: http://localhost:5000/admin
Default admin password: `admin123` (neeche change karna mat bhoolna)

## Render pe Deploy karne ke steps
1. Ye poora folder GitHub repo mein push karo.
2. Render.com pe jao → "New +" → "Web Service" → apna GitHub repo select karo.
3. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
4. Environment Variables (Render dashboard mein "Environment" tab, zaroor add karo):
   - `SECRET_KEY` = koi bhi random lambi string
   - `ADMIN_PASSWORD` = apna khud ka secure password
5. Deploy dabao — kuch minute mein live ho jayega!

⚠️ **Important note:** Render ka free tier filesystem *ephemeral* hota hai — matlab agar app restart/redeploy hoti hai to SQLite database (`test_app.db`) reset ho sakta hai. Isliye:
- Test lene ke turant baad admin panel se CSV download kar ke apne paas save kar lo.
- Agar data permanently chahiye, Render ka paid "Persistent Disk" use karo ya database ko koi free Postgres (Render/Supabase) mein migrate kar lo — poochna mujhe, main help kar dunga.

## Test khatam karne ke baad
- Har naye test/exam ke liye purane questions delete kar ke naye add kar do (admin dashboard se), ya purane hi rehne do.
- Naya batch shuru karne se pehle "Reset All Results" dabao (purana CSV pehle download kar lena!).

Enjoy! 🎉
