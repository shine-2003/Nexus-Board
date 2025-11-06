import os
import requests
import random
import string
import google.generativeai as genai
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask import request, jsonify
from datetime import datetime, date
from flask import jsonify, request, session
from flask import jsonify, request
from flask_login import login_required, current_user
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from io import BytesIO
from flask import send_file
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import threading
import os
from dotenv import load_dotenv
load_dotenv()

print("✅ EMAIL_USER:", os.getenv("EMAIL_USER"))
print("✅ EMAIL_PASS:", os.getenv("EMAIL_PASS"))

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
active_members = {}  # { board_id: { user_id: {"username": ..., "last_active": datetime} } }
app.secret_key = os.environ.get('FLASK_SECRET', 'change_this_secret')
# ---------- EMAIL CONFIG ----------
EMAIL_ADDRESS = os.getenv("EMAIL_USER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASS")


GEMINI_API_KEY = "Api_key"

genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel("gemini-2.0-flash")
# DB config — change if needed
DB_HOST = os.environ.get('DB_HOST', 'localhost')
DB_NAME = os.environ.get('DB_NAME', 'nexusboard')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASS = os.environ.get('DB_PASS', 'your password')
DB_PORT = os.environ.get('DB_PORT', '5432')

def get_db_conn():
    return psycopg2.connect(
        host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=DB_PORT
    )

def gen_code():
    return 'NXB' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

def log_action(board_id, user_id, action):
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO history (board_id, user_id, action) VALUES (%s, %s, %s)",
                    (board_id, user_id, action))
        conn.commit()
        socketio.emit('history_update', {'board_id': board_id}, room=f'board_{board_id}')

    except Exception:
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# ---------- AUTH ----------
@app.route('/')
def index():
    if session.get('user'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username','').strip()
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        if not username or not email or not password:
            flash('All fields required', 'error')
            return render_template('auth.html', mode='register')
        hashed = generate_password_hash(password)
        conn = get_db_conn()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
                        (username, email, hashed))
            conn.commit()
            flash('Registration successful. Please login.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            conn.rollback()
            flash('Registration error: ' + str(e), 'error')
        finally:
            cur.close(); conn.close()
    return render_template('auth.html', mode='register')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        conn = get_db_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("SELECT id, username, email, password_hash FROM users WHERE email=%s", (email,))
            user = cur.fetchone()
            if user and check_password_hash(user['password_hash'], password):
                session['user'] = {'id': user['id'], 'username': user['username']}
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid credentials', 'error')
        except Exception as e:
            flash('Login error: ' + str(e), 'error')
        finally:
            cur.close(); conn.close()
    return render_template('auth.html', mode='login')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out', 'success')
    return redirect(url_for('login'))

# ---------- DASHBOARD ----------
@app.route('/dashboard')
def dashboard():
    if not session.get('user'):
        return redirect(url_for('login'))
    user_id = session['user']['id']
    conn = get_db_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Boards user owns
        cur.execute("SELECT * FROM boards WHERE owner_id=%s ORDER BY created_at DESC", (user_id,))
        owned = cur.fetchall()
        # Boards user joined (exclude ones they own to avoid duplication)
        cur.execute("""
            SELECT b.*, u.username AS owner_name
            FROM boards b
            JOIN users u ON b.owner_id = u.id
            JOIN user_boards ub ON ub.board_id = b.id
            WHERE ub.user_id = %s AND b.owner_id != %s
            ORDER BY b.created_at DESC
        """, (user_id, user_id))
        joined = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return render_template('dashboard.html', user=session['user'], owned_boards=owned, joined_boards=joined)



@app.route('/get_daily_quote')
def get_daily_quote():
    today = date.today()
    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("SELECT quote, author FROM daily_quotes WHERE date = %s", (today,))
    row = cur.fetchone()

    if row:
        quote, author = row
    else:
        API_KEY = "apikey"
        headers = {'X-Api-Key': API_KEY}

        try:
            # ✅ Try with inspirational first
            resp = requests.get('https://api.api-ninjas.com/v1/quotes?category=inspirational', headers=headers, timeout=5)
            data = resp.json() if resp.status_code == 200 else []
            if data:
                q = data[0]
                quote = q.get('quote', '').strip() or "Keep pushing your limits."
                author = q.get('author', '').strip() or "NexusBoard"
            else:
                # ✅ Try without category as fallback
                resp2 = requests.get('https://api.api-ninjas.com/v1/quotes', headers=headers, timeout=5)
                data2 = resp2.json() if resp2.status_code == 200 else []
                if data2:
                    q = data2[0]
                    quote = q.get('quote', '').strip() or "Keep pushing forward!"
                    author = q.get('author', '').strip() or "NexusBoard"
                else:
                    quote = "Collaboration turns goals into achievements!"
                    author = "NexusBoard"
        except Exception as e:
            print("Quote API error:", e)
            quote = "Collaboration turns goals into achievements!"
            author = "NexusBoard"

        # ✅ Save today's quote safely
        cur.execute("""
            INSERT INTO daily_quotes (date, quote, author)
            VALUES (%s, %s, %s)
            ON CONFLICT (date) DO UPDATE SET quote = EXCLUDED.quote, author = EXCLUDED.author
        """, (today, quote, author))
        conn.commit()

    cur.close()
    conn.close()
    return jsonify({'quote': quote, 'author': author})


@app.route("/get_avatar", methods=["GET"])
def get_avatar():
    if "user" not in session:
        return jsonify({"error": "not_logged_in"}), 403

    user_id = session["user"]["id"]
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT avatar_url FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        avatar_url = row[0] if row else None
    finally:
        cur.close()
        conn.close()
    return jsonify({"avatar_url": avatar_url})


@app.route("/save_avatar", methods=["POST"])
def save_avatar():
    if "user" not in session:
        return jsonify({"error": "not_logged_in"}), 403

    data = request.get_json()
    avatar_url = data.get("avatar_url")
    if not avatar_url:
        return jsonify({"status": "error", "message": "No avatar URL provided"}), 400

    user_id = session["user"]["id"]
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET avatar_url = %s WHERE id = %s", (avatar_url, user_id))
        conn.commit()
        return jsonify({"status": "success"})
    finally:
        cur.close()
        conn.close()

@app.route("/export_tasks_pdf/<int:board_id>")
def export_tasks_pdf(board_id):
    if "user" not in session:
        return redirect(url_for("login"))

    conn = get_db_conn()
    cur = conn.cursor()
    try:
        # Get board name
        cur.execute("SELECT name FROM boards WHERE id = %s", (board_id,))
        board = cur.fetchone()
        if not board:
            flash("Board not found", "error")
            return redirect(url_for("dashboard"))
        board_name = board[0]

        # ✅ Join with users table to get assigned username
        cur.execute("""
            SELECT 
                t.name, t.description, u.username AS assigned_name, 
                t.comments, t.progress_percent, t.created_at, t.due_date
            FROM tasks t
            LEFT JOIN users u ON t.assigned_to = u.id
            WHERE t.board_id = %s
            ORDER BY t.created_at ASC
        """, (board_id,))
        tasks = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    # Create PDF
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    pdf.setTitle(f"{board_name} - Task Report")

    # Header
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(1 * inch, height - 1 * inch, f"📋 Task Report – {board_name}")

    pdf.setFont("Helvetica", 12)
    pdf.drawString(1 * inch, height - 1.3 * inch, f"Exported by: {session['user']['username']}")
    pdf.drawString(1 * inch, height - 1.5 * inch, f"Total Tasks: {len(tasks)}")

    y = height - 2 * inch

    for t in tasks:
        name = t[0] or "Untitled Task"
        desc = t[1] or "No description"
        assigned_name = t[2] or "Unassigned"
        comments = t[3] or "None"
        progress = str(t[4] or 0)
        created_at = t[5].strftime("%Y-%m-%d %H:%M") if t[5] else "N/A"
        due_date = t[6].strftime("%Y-%m-%d %H:%M") if t[6] else "N/A"

        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(1 * inch, y, f"Task: {name}")
        y -= 15
        pdf.setFont("Helvetica", 11)
        pdf.drawString(1.2 * inch, y, f"Assigned: {assigned_name}")
        y -= 13
        pdf.drawString(1.2 * inch, y, f"Progress: {progress}%")
        y -= 13
        pdf.drawString(1.2 * inch, y, f"Comments: {comments}")
        y -= 13
        pdf.drawString(1.2 * inch, y, f"Created: {created_at} | Due: {due_date}")
        y -= 20

        # New page if needed
        if y < 100:
            pdf.showPage()
            y = height - 1 * inch

    pdf.showPage()
    pdf.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{board_name}_tasks.pdf",
        mimetype="application/pdf"
    )



# Update (increment) user time
@app.route("/update_time_spent", methods=["POST"])
def update_time_spent():
    if "user" not in session:
        return jsonify({"error": "not_logged_in"}), 403

    data = request.get_json()
    seconds = data.get("seconds", 0)
    user_id = session["user"]["id"]
    today = date.today()

    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO time_spent (user_id, date, seconds)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, date)
            DO UPDATE SET seconds = time_spent.seconds + EXCLUDED.seconds
        """, (user_id, today, seconds))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return jsonify({"status": "ok"})

# Get today’s total
@app.route("/get_today_time")
def get_today_time():
    if "user" not in session:
        return jsonify({"seconds": 0})

    user_id = session["user"]["id"]
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT seconds FROM time_spent WHERE user_id=%s AND date=%s",
                    (user_id, date.today()))
        row = cur.fetchone()
        total_seconds = row[0] if row else 0
    finally:
        cur.close()
        conn.close()

    return jsonify({"seconds": total_seconds})

# Get last 7 days
@app.route("/get_weekly_time")
def get_weekly_time():
    if "user" not in session:
        return jsonify([])

    user_id = session["user"]["id"]
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT date, seconds FROM time_spent
            WHERE user_id=%s
            ORDER BY date DESC
            LIMIT 7
        """, (user_id,))
        data = [{"date": str(r[0]), "seconds": r[1]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    return jsonify(data)



# ---------- CREATE BOARD ----------
@app.route('/add_board', methods=['POST'])
def add_board():
    if not session.get('user'):
        return redirect(url_for('login'))
    name = request.form.get('name','').strip()
    description = request.form.get('description','').strip()
    if not name:
        flash('Board name required', 'error'); return redirect(url_for('dashboard'))
    owner_id = session['user']['id']
    code = gen_code()
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO boards (name, description, board_code, owner_id) VALUES (%s,%s,%s,%s)",
                    (name, description, code, owner_id))
        conn.commit()
        # auto add owner as member too (optional, but helpful)
        cur.execute("INSERT INTO user_boards (user_id, board_id) VALUES (%s, currval('boards_id_seq'))", (owner_id,))
        conn.commit()
        flash(f'Board created. Code: {code}', 'success')
        socketio.emit('board_update')
    except Exception as e:
        conn.rollback(); flash('Create board error: ' + str(e), 'error')
    finally:
        cur.close(); conn.close()
    return redirect(url_for('dashboard'))

# ---------- JOIN BOARD ----------
@app.route('/join_board', methods=['POST'])
def join_board():
    if not session.get('user'):
        return redirect(url_for('login'))
    code = request.form.get('board_code','').strip()
    if not code:
        flash('Board code required', 'error'); return redirect(url_for('dashboard'))
    user_id = session['user']['id']
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM boards WHERE board_code=%s", (code,))
        row = cur.fetchone()
        if not row:
            flash('Invalid board code', 'error')
        else:
            board_id = row[0]
            # check existing
            cur.execute("SELECT id FROM user_boards WHERE user_id=%s AND board_id=%s", (user_id, board_id))
            if cur.fetchone():
                flash('Already joined', 'info')
            else:
                cur.execute("INSERT INTO user_boards (user_id, board_id) VALUES (%s,%s)", (user_id, board_id))
                conn.commit()
                flash('Joined board', 'success')
    except Exception as e:
        conn.rollback(); flash('Join error: ' + str(e), 'error')
    finally:
        cur.close(); conn.close()
    return redirect(url_for('dashboard'))

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow}

# ---------- OPEN BOARD VIEW ----------
@app.route('/board/<int:board_id>')
def board_view(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    user_id = session['user']['id']
    search = request.args.get('search', '').strip().lower()
    filter_user = request.args.get('filter', '')

    conn = get_db_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT b.*, u.username AS owner_name FROM boards b JOIN users u ON b.owner_id=u.id WHERE b.id=%s", (board_id,))
        board = cur.fetchone()
        if not board:
            flash('Board not found', 'error'); return redirect(url_for('dashboard'))

        cur.execute("SELECT 1 FROM user_boards WHERE board_id=%s AND user_id=%s", (board_id, user_id))
        if not cur.fetchone():
            flash('You are not a member of this board', 'error'); return redirect(url_for('dashboard'))

        # members
        cur.execute("""
            SELECT u.id, u.username
            FROM users u
            JOIN user_boards ub ON ub.user_id = u.id
            WHERE ub.board_id = %s
            ORDER BY u.username
        """, (board_id,))
        members = cur.fetchall()

        # tasks with optional search/filter
        query = """
            SELECT t.*, u.username AS assigned_name
            FROM tasks t
            LEFT JOIN users u ON t.assigned_to = u.id
            WHERE t.board_id=%s
        """
        params = [board_id]

        if search:
            query += " AND LOWER(t.name) LIKE %s"
            params.append(f"%{search}%")
        if filter_user:
            query += " AND t.assigned_to=%s"
            params.append(filter_user)

        query += " ORDER BY t.position ASC"
        cur.execute(query, tuple(params))
        tasks = cur.fetchall()
    finally:
        cur.close()
        conn.close()

# Fetch completed tasks (run outside finally)
    conn = get_db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT c.*, u.username AS completed_by
        FROM completed_tasks c
       LEFT JOIN users u ON c.assigned_to = u.id
       WHERE c.board_id = %s
       ORDER BY c.completed_date DESC
    """, (board_id,))
    completed = cur.fetchall()
    cur.close()
    conn.close()
    return render_template('board.html', board=board, members=members, tasks=tasks, completed=completed,
                           user=session['user'], search=search, filter_user=filter_user)

def get_gemini_response(user_message):
    chat = model.start_chat(history=[])
    response = chat.send_message(user_message, stream=False)
    if response.candidates:
        generated_text = response.candidates[0].content
        parts = generated_text.split('text: "')
        if len(parts) >= 2:
            refined_text = parts[1].split('"\n')[0]
            cleaned_string = refined_text.replace('\n', '')
            return cleaned_string
    return "Sorry, I don’t have a response for that right now."

@app.route("/api/chatbot", methods=["POST"])
def chatbot_api():
    try:
        data = request.get_json(force=True)
        message = data.get("message") or data.get("prompt")
        if not message:
            return jsonify({"error": "Empty message"}), 400

        # Use the same model initialized globally
        response = model.generate_content(message)

        # Ensure we return real text if available
        reply_text = getattr(response, "text", None)
        if not reply_text:
            return jsonify({"reply": "Sorry, I couldn’t get a response from Gemini."}), 200

        return jsonify({"reply": reply_text}), 200

    except Exception as e:
        print("Chatbot error:", str(e))
        return jsonify({"error": str(e)}), 500


@app.route('/add_task/<int:board_id>', methods=['POST'])
def add_task(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    name = request.form.get('name','').strip()
    description = request.form.get('description','').strip()
    assigned_to = request.form.get('assigned_to') or None
    comments = request.form.get('comments','').strip()
    due_date = request.form.get('due_date') or None
    if not name:
        flash('Task name required', 'error')
        return redirect(url_for('board_view', board_id=board_id))

    dt_due = None
    if due_date:
        try:
            dt_due = datetime.fromisoformat(due_date)
        except Exception:
            flash('Invalid due date format.', 'error')
            return redirect(url_for('board_view', board_id=board_id))

    conn = get_db_conn()
    cur = conn.cursor()
    try:
        progress_percent = int(request.form.get('progress_percent', 0))
        cur.execute("""
            INSERT INTO tasks (name, description, board_id, assigned_to, comments, due_date, progress_percent)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (name, description, board_id, assigned_to, comments, dt_due, progress_percent))
        task_id = cur.fetchone()[0]
        conn.commit()

        # Log history
        log_action(board_id, session['user']['id'], f"Created task '{name}'")

        # ✅ Trigger Email (3-minute delay)
        if assigned_to:
            cur.execute("SELECT email, username FROM users WHERE id=%s", (assigned_to,))
            user_row = cur.fetchone()

            cur.execute("SELECT name FROM boards WHERE id=%s", (board_id,))
            board_row = cur.fetchone()

            if user_row and board_row:
                to_email = user_row[0]
                board_name = board_row[0]
                assigned_by = session['user']['username']
                timer = threading.Timer(
                    180,  # 3 minutes delay
                    send_task_email,
                    args=[to_email, board_name, name, description, due_date, assigned_by]
                )
                timer.start()

        flash('Task added successfully and email scheduled.', 'success')
        socketio.emit('task_update', {'board_id': board_id}, room=f'board_{board_id}')

    except Exception as e:
        conn.rollback()
        print("Add task error:", e)
        flash('Error adding task: ' + str(e), 'error')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('board_view', board_id=board_id))

def send_task_email(to_email, board_name, task_name, description, due_date, assigned_by):
    """Send task details email via Gmail SMTP"""
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = to_email
        msg["Subject"] = f"📋 New Task Assigned – {board_name}"

        body = f"""
        <html>
        <body>
            <h2>New Task Assigned in <b>{board_name}</b></h2>
            <p>Hello,</p>
            <p>You’ve been assigned a new task.</p>
            <ul>
                <li><b>Task:</b> {task_name}</li>
                <li><b>Description:</b> {description or 'No description provided'}</li>
                <li><b>Due Date:</b> {due_date or 'Not set'}</li>
                <li><b>Assigned by:</b> {assigned_by}</li>
            </ul>
            <p>Please log in to NexusBoard to view and update progress.</p>
            <p>– Team NexusBoard</p>
        </body>
        </html>
        """

        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.send_message(msg)

        print(f"✅ Email sent to {to_email} for task '{task_name}'")

    except Exception as e:
        print("❌ Email send error:", e)

# ---------- EDIT TASK ----------
@app.route('/edit_task/<int:task_id>', methods=['GET', 'POST'])
def edit_task(task_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if request.method == 'POST':
            name = request.form.get('name').strip()
            description = request.form.get('description','').strip()
            assigned_to = request.form.get('assigned_to') or None
            comments = request.form.get('comments','').strip()
            due_date = request.form.get('due_date') or None
            progress_percent = int(request.form.get('progress_percent', 0))
            dt_due = None
            if due_date:
                try:
                    dt_due = datetime.fromisoformat(due_date)
                except:
                    flash('Invalid due date', 'error')
                    return redirect(request.referrer)
            cur.execute("SELECT board_id FROM tasks WHERE id=%s", (task_id,))
            task_row = cur.fetchone()
            if not task_row:
                flash('Task not found', 'error'); return redirect(url_for('dashboard'))
            board_id = task_row['board_id']
            # check membership
            user_id = session['user']['id']
            cur.execute("SELECT 1 FROM user_boards WHERE user_id=%s AND board_id=%s", (user_id, board_id))
            if not cur.fetchone():
                flash('Not authorized', 'error'); return redirect(url_for('dashboard'))
            cur.execute("""
                UPDATE tasks
                SET name=%s, description=%s, assigned_to=%s, comments=%s, due_date=%s, progress_percent=%s
                WHERE id=%s
            """, (name, description, assigned_to, comments, dt_due, progress_percent, task_id))
            conn.commit()
            log_action(board_id, session['user']['id'], f"Edited task '{name}'")
            # --- Auto move to completed_tasks if progress == 100 ---
            if progress_percent == 100:
                cur.execute("SELECT name, description, assigned_to FROM tasks WHERE id=%s", (task_id,))
                t = cur.fetchone()
                if t:
                    cur.execute("""
                        INSERT INTO completed_tasks (name, description, board_id, assigned_to, completed_date)
                        VALUES (%s, %s, %s, %s, NOW())
                    """, (t['name'], t['description'], board_id, t['assigned_to']))
                    cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
                    conn.commit()
                    log_action(board_id, session['user']['id'], f"Completed task '{t['name']}'")
                    socketio.emit('task_update', {'board_id': board_id}, room=f'board_{board_id}')
            flash('Task updated', 'success')
            socketio.emit('task_update', {'board_id': board_id}, room=f'board_{board_id}')
            return redirect(url_for('board_view', board_id=board_id))
        # GET: show form
        cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
        task = cur.fetchone()
        if not task:
            flash('Task not found', 'error'); return redirect(url_for('dashboard'))
        board_id = task['board_id']
        # load members for select
        cur.execute("""
            SELECT u.id, u.username
            FROM users u
            JOIN user_boards ub ON ub.user_id = u.id
            WHERE ub.board_id = %s
            ORDER BY u.username
        """, (board_id,))
        members = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return render_template('edit_task.html', task=task, members=members)

# ---------- DELETE TASK ----------
@app.route('/delete_task/<int:task_id>')
def delete_task(task_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT board_id FROM tasks WHERE id=%s", (task_id,))
        row = cur.fetchone()
        if not row:
            flash('Task not found', 'error'); return redirect(url_for('dashboard'))
        board_id = row[0]
        # verify membership
        user_id = session['user']['id']
        cur.execute("SELECT 1 FROM user_boards WHERE user_id=%s AND board_id=%s", (user_id, board_id))
        if not cur.fetchone():
            flash('Not authorized', 'error'); return redirect(url_for('dashboard'))
        cur.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        log_action(board_id, session['user']['id'], f"Deleted a task (ID {task_id})")
        conn.commit(); flash('Task deleted', 'success')
        socketio.emit('task_update', {'board_id': board_id}, room=f'board_{board_id}')
    except Exception as e:
        conn.rollback(); flash('Delete task error: '+str(e), 'error')
    finally:
        cur.close(); conn.close()
    return redirect(url_for('board_view', board_id=board_id))

# ---------- GROUP CHAT ROUTES ----------
@app.route('/groupchat/<int:board_id>')
def group_chat(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    return render_template('groupchat.html', board_id=board_id, user=session['user'])

@app.route('/messages/<int:board_id>')
def get_messages(board_id):
    if not session.get('user'):
        return jsonify([])
    user_id = session['user']['id']
    conn = get_db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # hide messages deleted by this user or deleted for all
        cur.execute("""
            SELECT m.id, m.message, m.sent_at, u.username AS sender_name, m.sender_id
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE m.board_id = %s
              AND m.deleted_for_all = FALSE
              AND m.id NOT IN (
                SELECT message_id FROM message_deletions WHERE user_id = %s
              )
            ORDER BY m.sent_at ASC
        """, (board_id, user_id))
        messages = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return jsonify(messages)

# ---------- PERFORMANCE ----------
@app.route('/performance/<int:board_id>')
def performance(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT u.username, COALESCE(AVG(t.progress_percent),0) AS avg_progress
            FROM users u
            JOIN user_boards ub ON ub.user_id = u.id
            LEFT JOIN tasks t ON t.assigned_to = u.id AND t.board_id = %s
            WHERE ub.board_id = %s
            GROUP BY u.username
            ORDER BY avg_progress DESC
        """, (board_id, board_id))
        data = cur.fetchall()
    finally:
        cur.close(); conn.close()
    return render_template('performance.html', data=data)

# ---------- PROJECT STATUS ----------
@app.route('/status/<int:board_id>')
def project_status(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(AVG(progress_percent),0) FROM tasks WHERE board_id=%s", (board_id,))
        percent = cur.fetchone()[0]
    finally:
        cur.close(); conn.close()
    return render_template('status.html', percent=percent)

@app.route('/update_task_order/<int:board_id>', methods=['POST'])
def update_task_order(board_id):
    if not session.get('user'):
        return "Unauthorized", 403
    data = request.get_json()
    ordered_ids = data.get('ordered_ids', [])
    if not ordered_ids:
        return "No order provided", 400
    conn = get_db_conn(); cur = conn.cursor()
    try:
        for position, task_id in enumerate(ordered_ids):
            cur.execute("UPDATE tasks SET position=%s WHERE id=%s AND board_id=%s", (position, task_id, board_id))
        conn.commit()
        socketio.emit('task_update', {'board_id': board_id}, room=f'board_{board_id}')
        return "Order updated", 200
    except Exception as e:
        conn.rollback()
        return str(e), 500
    finally:
        cur.close(); conn.close()

# ---------- EDIT BOARD ----------
@app.route('/edit_board/<int:board_id>', methods=['GET','POST'])
def edit_board(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM boards WHERE id=%s", (board_id,))
        board = cur.fetchone()
        if not board:
            flash('Not found', 'error'); return redirect(url_for('dashboard'))
        if session['user']['id'] != board['owner_id']:
            flash('Only owner can edit board', 'error'); return redirect(url_for('dashboard'))
        if request.method == 'POST':
            name = request.form.get('name').strip()
            desc = request.form.get('description','').strip()
            cur.execute("UPDATE boards SET name=%s, description=%s WHERE id=%s", (name, desc, board_id))
            conn.commit(); flash('Board updated', 'success'); return redirect(url_for('dashboard'))
            socketio.emit('board_update')
    finally:
        cur.close(); conn.close()
    return render_template('edit_board.html', board=board)

# ---------- DELETE BOARD ----------
@app.route('/delete_board/<int:board_id>')
def delete_board(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT owner_id FROM boards WHERE id=%s", (board_id,))
        r = cur.fetchone()
        if not r:
            flash('Board not found', 'error'); return redirect(url_for('dashboard'))
        if r[0] != session['user']['id']:
            flash('Only owner can delete', 'error'); return redirect(url_for('dashboard'))
        cur.execute("DELETE FROM boards WHERE id=%s", (board_id,))
        conn.commit(); flash('Board deleted', 'success')
        socketio.emit('board_update')
    except Exception as e:
        conn.rollback(); flash('Delete board error: '+str(e), 'error')
    finally:
        cur.close(); conn.close()
    return redirect(url_for('dashboard'))

@app.route('/invite_member/<int:board_id>', methods=['POST'])
def invite_member(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT owner_id FROM boards WHERE id=%s", (board_id,))
        row = cur.fetchone()
        if not row:
            flash('Board not found.', 'error'); return redirect(url_for('dashboard'))
        if session['user']['id'] != row[0]:
            flash('Only owner can invite members.', 'error'); return redirect(url_for('board_view', board_id=board_id))
        email = request.form.get('email','').strip().lower()
        if not email:
            flash('Email required', 'error'); return redirect(url_for('board_view', board_id=board_id))
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        user = cur.fetchone()
        if not user:
            flash('User not found, ask them to register.', 'error'); return redirect(url_for('board_view', board_id=board_id))
        user_id = user[0]
        cur.execute("SELECT 1 FROM user_boards WHERE user_id=%s AND board_id=%s", (user_id, board_id))
        if cur.fetchone():
            flash('User already a member.', 'info'); return redirect(url_for('board_view', board_id=board_id))
        cur.execute("INSERT INTO user_boards (user_id, board_id) VALUES (%s,%s)", (user_id, board_id))
        conn.commit(); flash('Member invited successfully!', 'success')
        socketio.emit('member_update', {'board_id': board_id}, room=f'board_{board_id}')
    except Exception as e:
        conn.rollback(); flash('Invite error: '+str(e), 'error')
    finally:
        cur.close(); conn.close()
    return redirect(url_for('board_view', board_id=board_id))

@app.route('/remove_member/<int:board_id>/<int:user_id>')
def remove_member(board_id, user_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT owner_id FROM boards WHERE id=%s", (board_id,))
        row = cur.fetchone()
        if not row:
            flash('Board not found.', 'error'); return redirect(url_for('dashboard'))
        if session['user']['id'] != row[0]:
            flash('Only owner can remove members.', 'error'); return redirect(url_for('board_view', board_id=board_id))
        if user_id == row[0]:
            flash('Owner cannot remove themselves.', 'error'); return redirect(url_for('board_view', board_id=board_id))
        cur.execute("DELETE FROM user_boards WHERE user_id=%s AND board_id=%s", (user_id, board_id))
        conn.commit(); flash('Member removed successfully.', 'success')
        socketio.emit('member_update', {'board_id': board_id}, room=f'board_{board_id}')
    except Exception as e:
        conn.rollback(); flash('Remove member error: '+str(e), 'error')
    finally:
        cur.close(); conn.close()
    return redirect(url_for('board_view', board_id=board_id))

@app.route('/history/<int:board_id>')
def board_history(board_id):
    if not session.get('user'):
        return redirect(url_for('login'))
    conn = get_db_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT h.*, u.username
            FROM history h
            LEFT JOIN users u ON h.user_id = u.id
            WHERE h.board_id = %s
            ORDER BY h.timestamp DESC
        """, (board_id,))
        logs = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    return render_template('history.html', logs=logs)

@app.route('/delete_history/<int:log_id>', methods=['POST'])
def delete_history(log_id):
    if not session.get('user'):
        return "Unauthorized", 403
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM history WHERE id=%s", (log_id,))
        conn.commit()
        return "Deleted", 200
    except Exception:
        conn.rollback()
        return "Error", 500
    finally:
        cur.close()
        conn.close()

# ---------- SOCKET.IO EVENTS ----------
@socketio.on('join_board')
def handle_join_board(data):
    board_id = data.get('board_id')
    if board_id:
        join_room(f'board_{board_id}')

@socketio.on('leave_board')
def handle_leave_board(data):
    board_id = data.get('board_id')
    if board_id:
        leave_room(f'board_{board_id}')

@socketio.on('join_dashboard')
def join_dashboard():
    join_room('dashboard')

@socketio.on('send_message')
def handle_send(data):
    board_id = data.get('board_id')
    user = session.get('user')
    if not user or not board_id:
        return
    msg = data.get('message', '').strip()
    if not msg:
        return
    conn = get_db_conn(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO messages (board_id, sender_id, message)
            VALUES (%s,%s,%s) RETURNING id, sent_at
        """, (board_id, user['id'], msg))
        row = cur.fetchone()
        conn.commit()
        payload = {
            'id': row['id'],
            'message': msg,
            'sender_id': user['id'],
            'sender_name': user['username'],
            'sent_at': row['sent_at'].strftime('%H:%M')
        }
        socketio.emit('new_message', payload, room=f'board_{board_id}')
    finally:
        cur.close(); conn.close()


@socketio.on('delete_message')
def handle_delete(data):
    msg_id = data.get('message_id')
    board_id = data.get('board_id')
    delete_for_all = data.get('for_all', False)
    user = session.get('user')
    if not msg_id or not user:
        return
    conn = get_db_conn(); cur = conn.cursor()
    try:
        if delete_for_all:
            cur.execute("UPDATE messages SET deleted_for_all=TRUE WHERE id=%s", (msg_id,))
            conn.commit()
            socketio.emit('delete_message_all', {'id': msg_id}, room=f'board_{board_id}')
        else:
            cur.execute("INSERT INTO message_deletions (message_id,user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (msg_id, user['id']))
            conn.commit()
            emit('delete_message_self', {'id': msg_id})
    finally:
        cur.close(); conn.close()


@socketio.on('edit_message')
def handle_edit(data):
    msg_id = data.get('message_id')
    new_text = data.get('new_text', '').strip()
    board_id = data.get('board_id')
    user = session.get('user')
    if not msg_id or not user or not new_text:
        return
    conn = get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("UPDATE messages SET message=%s WHERE id=%s AND sender_id=%s",
                    (new_text, msg_id, user['id']))
        conn.commit()
        socketio.emit('edit_message', {'id': msg_id, 'new_text': new_text}, room=f'board_{board_id}')
    finally:
        cur.close(); conn.close()

# ------------------- ACTIVE MEMBERS TRACKER (In-Memory) -------------------

active_members = {}  # { board_id: { user_id: {"username": ..., "last_active": datetime} } }

def emit_active_members(board_id):
    """Broadcast the current active/last-seen list to everyone in the board."""
    now = datetime.utcnow()
    board_data = active_members.get(board_id, {})
    result = []

    for uid, info in board_data.items():
        last_seen = info["last_active"]
        delta = now - last_seen
        is_online = delta < timedelta(seconds=30)
        time_ago = ""
        if not is_online:
            seconds = int(delta.total_seconds())
            if seconds < 60:
                time_ago = f"{seconds}s ago"
            elif seconds < 3600:
                time_ago = f"{seconds // 60}m ago"
            else:
                time_ago = f"{seconds // 3600}h ago"

        result.append({
            "username": info["username"],
            "is_online": is_online,
            "last_active": time_ago
        })

    emit("active_members_update", {"board_id": board_id, "members": result}, room=f"board_{board_id}")


@socketio.on('join_board')
def handle_join_board(data):
    board_id = data.get('board_id')
    user = session.get('user')
    if not board_id or not user:
        return

    join_room(f"board_{board_id}")

    if board_id not in active_members:
        active_members[board_id] = {}

    active_members[board_id][user['id']] = {
        "username": user['username'],
        "last_active": datetime.utcnow()
    }

    emit_active_members(board_id)


@socketio.on('leave_board')
def handle_leave_board(data):
    board_id = data.get('board_id')
    user = session.get('user')
    if not board_id or not user:
        return

    leave_room(f"board_{board_id}")

    if board_id in active_members and user['id'] in active_members[board_id]:
        active_members[board_id][user['id']]["last_active"] = datetime.utcnow()

    emit_active_members(board_id)


@socketio.on('user_active')
def handle_user_active(data):
    board_id = data.get('board_id')
    user = session.get('user')
    if not board_id or not user:
        return

    if board_id not in active_members:
        active_members[board_id] = {}

    active_members[board_id][user['id']] = {
        "username": user['username'],
        "last_active": datetime.utcnow()
    }

    emit_active_members(board_id)


if __name__ == '__main__':
    socketio.run(app, debug=True)

