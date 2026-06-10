from flask import (
    Flask, render_template, request,
    redirect, url_for, jsonify, session, flash
)
import os, sqlite3, uuid, re, json, time
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from gtts import gTTS
from datetime import datetime
from groq import Groq
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp
try:
    from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
except ImportError:
    try:
        from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound
    except ImportError:
        TranscriptsDisabled = Exception
        NoTranscriptFound = Exception

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

try:
    from docx import Document
except ImportError:
    Document = None

# ── CONFIG ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "smart_notes_secret_2026"

UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
AUDIO_FOLDER  = os.path.join("static", "audio")
DB_FILE       = "smart_notes.db"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(AUDIO_FOLDER,  exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS     = {"txt", "pdf", "docx"}
MAX_WORDS_BEFORE_CHUNK = 2500
CHUNK_SIZE             = 1500

# ── GROQ ──────────────────────────────────────────────────────────────────────
API_KEY  = "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"
MODEL_ID = "llama-3.1-8b-instant"

def get_client():
    return Groq(api_key=API_KEY)

# ── DATABASE ──────────────────────────────────────────────────────────────────
# ── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS notes (
                        id TEXT PRIMARY KEY, user TEXT, title TEXT,
                        raw_text TEXT, summary_json TEXT, keywords TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        action TEXT, filename TEXT, summary TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        subject TEXT DEFAULT 'General',
                        day TEXT DEFAULT 'Mon',
                        time TEXT DEFAULT '09:00',
                        priority TEXT DEFAULT 'medium',
                        notes TEXT DEFAULT '',
                        completed INTEGER DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()

init_db()  # ← only called ONCE


# ── UTILITIES ─────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def clean_text(text):
    text = re.sub(r'\n\d+\n', '\n', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def chunk_text(text, chunk_size=CHUNK_SIZE):
    words = text.split()
    return [" ".join(words[i:i+chunk_size]) for i in range(0, len(words), chunk_size)]

def add_history(action, filename, summary):
    with sqlite3.connect(DB_FILE) as conn:
        conn.cursor().execute(
            "INSERT INTO history (action, filename, summary) VALUES (?, ?, ?)",
            (action, filename, json.dumps(summary))
        )
        conn.commit()

# ── SAFE JSON PARSE ───────────────────────────────────────────────────────────
def safe_parse_json(raw):
    if not raw:
        return None
    raw = raw.strip()
    raw = re.sub(r'```(?:json)?\s*', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'```\s*$', '', raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find('{')
    end   = raw.rfind('}')
    if start != -1 and end != -1 and end > start:
        candidate = raw[start:end+1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        candidate = re.sub(r',\s*([}\]])', r'\1', candidate)
        candidate = re.sub(r"(?<![\\])'", '"', candidate)
        candidate = re.sub(r'(?<!\\)\n', ' ', candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            open_braces   = candidate.count('{') - candidate.count('}')
            open_brackets = candidate.count('[') - candidate.count(']')
            closing = (']' * max(open_brackets, 0)) + ('}' * max(open_braces, 0))
            trimmed = re.sub(r',\s*$', '', candidate.rstrip()) + closing
            return json.loads(trimmed)
        except Exception:
            pass
    return None

# ── AI CALL HELPER ────────────────────────────────────────────────────────────
def _call_groq(system_msg, user_msg, max_tokens=1500, temperature=0.3):
    client = get_client()
    response = client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": user_msg},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()

# ── AI: GENERATE STRUCTURED OUTPUT ───────────────────────────────────────────
def generate_structured_output(text):
    content = text[:3000]
    SYSTEM = (
        "You are a JSON-only exam study assistant. "
        "Output ONLY a valid JSON object. No markdown, no code fences, no extra text."
    )
    prompt = (
        "Analyze the content and return ONLY this JSON (all fields required):\n"
        '{"title":"5-10 word title",'
        '"definition":"2-3 sentence definition",'
        '"explanation":"4-6 sentence explanation of concepts, usage, importance",'
        '"bullet_points":["point 1","point 2","point 3","point 4","point 5"],'
        '"important_keywords":["kw1","kw2","kw3","kw4","kw5","kw6","kw7","kw8"],'
        '"five_mark_answer":"100-150 word exam answer covering definition and key points",'
        '"ten_mark_answer":"200-250 word exam answer with Introduction, Explanation, Examples, Conclusion",'
        '"viva_questions":["Q1?","Q2?","Q3?","Q4?","Q5?"],'
        '"mcqs":[{"question":"Q?","options":["A) o1","B) o2","C) o3","D) o4"],"answer":"A) o1"},'
        '{"question":"Q?","options":["A) o1","B) o2","C) o3","D) o4"],"answer":"B) o2"},'
        '{"question":"Q?","options":["A) o1","B) o2","C) o3","D) o4"],"answer":"C) o3"}]}\n\n'
        'CONTENT:\n' + content
    )
    try:
        raw = _call_groq(SYSTEM, prompt, max_tokens=1500)
        result = safe_parse_json(raw)
        if result is None:
            print("=== RAW (parse failed) ===")
            print(raw[:1000])
            print("=========================")
            return {"error": "AI returned invalid JSON. Please try again."}
        if not result.get("explanation") and not result.get("definition"):
            return {"error": "AI returned empty content. Please try again."}
        return result
    except Exception as e:
        err = str(e)
        if "429" in err:
            wait = "a few minutes"
            match = re.search(r'try again in (\d+)m', err, re.IGNORECASE)
            if match:
                wait = f"{match.group(1)} minutes"
            return {"error": f"Daily AI limit reached. Please try again in {wait}."}
        return {"error": f"AI Error: {err}"}

# ── AI: MERGE CHUNK RESULTS ───────────────────────────────────────────────────
def merge_chunk_results(chunk_results):
    valid = [r for r in chunk_results if r and not r.get("error")]
    if not valid:
        return {"error": "All chunks failed to process."}
    combined_text = "\n\n".join([
        f"Section {i+1}:\n"
        f"Title: {r.get('title','')}\n"
        f"Definition: {r.get('definition','')}\n"
        f"Explanation: {r.get('explanation','')}\n"
        f"Key Points: {chr(10).join(r.get('bullet_points', []))}\n"
        f"Keywords: {', '.join(r.get('important_keywords', []))}"
        for i, r in enumerate(valid)
    ])
    return generate_structured_output(combined_text)

# ── YOUTUBE ───────────────────────────────────────────────────────────────────
def extract_video_id(url):
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})',
        r'(?:shorts\/)([0-9A-Za-z_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def _snippets_to_text(snippets):
    """Extract plain text from transcript snippets (works for v0.x dicts and v1.x objects)."""
    parts = []
    for s in snippets:
        try:
            if hasattr(s, 'text'):           # v1.0+ object
                parts.append(s.text)
            elif isinstance(s, dict):         # v0.x dict
                parts.append(s.get('text', ''))
        except Exception:
            continue
    return " ".join(filter(None, parts)).strip()


def fetch_transcript(video_id, retries=3):
    """
    Strong YouTube transcript fetcher with multi-language + fallback
    """

    last_error = "No transcript available. Try another video with subtitles."

    for attempt in range(retries):
        try:
            # Step 1: Try direct transcript (fast)
            try:
                data = YouTubeTranscriptApi.get_transcript(
                    video_id,
                    languages=['en', 'en-IN', 'en-US', 'hi', 'mr']
                )
                text = _snippets_to_text(data)
                if text:
                    return text, None
            except Exception:
                pass

            # Step 2: Try transcript list (best fallback)
            try:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

                # 1. Manual transcripts
                for lang in ['en', 'hi', 'mr']:
                    try:
                        t = transcript_list.find_manually_created_transcript([lang])
                        text = _snippets_to_text(t.fetch())
                        if text:
                            return text, None
                    except Exception:
                        continue

                # 2. Auto-generated transcripts
                for lang in ['en', 'hi', 'mr']:
                    try:
                        t = transcript_list.find_generated_transcript([lang])
                        text = _snippets_to_text(t.fetch())
                        if text:
                            return text, None
                    except Exception:
                        continue

                # 3. ANY available transcript
                for t in transcript_list:
                    try:
                        text = _snippets_to_text(t.fetch())
                        if text:
                            return text, None
                    except Exception:
                        continue

            except Exception as e:
                print("Transcript list error:", e)

            # Retry
            if attempt < retries - 1:
                time.sleep(2)
                continue

            return None, last_error

        except Exception as e:
            err = str(e).lower()
            print("Fetch error:", err)

            if "disabled" in err:
                return None, "❌ Captions are disabled for this video."

            if "no transcript" in err:
                return None, "❌ No subtitles found."

            if "429" in err or "too many requests" in err:
                return None, "⚠️ Too many requests. Wait 2–5 minutes."

            if "blocked" in err or "bot" in err:
                return None, "⚠️ YouTube blocked request. Try later."

            return None, f"Error: {str(e)}"

    return None, "Failed after multiple attempts."

def summarize_youtube_transcript(text):
    return generate_structured_output(text)

# ── AUTH HELPER ───────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/test")
def test():
    return "Server working"

@app.route("/")
def home():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Please fill in all fields.", "error")
            return render_template("login.html")
        with sqlite3.connect(DB_FILE) as conn:
            user = conn.cursor().execute(
                "SELECT id, username, password FROM users WHERE username = ?",
                (username,)
            ).fetchone()
        if user and check_password_hash(user[2], password):
            session["user"]    = user[1]
            session["user_id"] = user[0]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if not username or not password:
            flash("Please fill in all fields.", "error")
            return render_template("login.html")

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("login.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("login.html")

        try:
            with sqlite3.connect(DB_FILE) as conn:
                c = conn.cursor()
                existing = c.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()

                if existing:
                    flash("Username already taken. Please choose another.", "error")
                    return render_template("login.html")

                hashed = generate_password_hash(password)
                c.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, hashed)
                )
                conn.commit()  # ← single connection, single cursor, commits properly

            flash("Account created! Please sign in.", "success")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            flash("Username already taken. Please choose another.", "error")
            return render_template("login.html")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM notes")
        notes_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM history")
        summaries_count = c.fetchone()[0]
        c.execute("SELECT action, filename, timestamp FROM history ORDER BY timestamp DESC LIMIT 5")
        recent_activity = c.fetchall()
    return render_template("dashboard.html",
        stats={"notes": notes_count, "summaries": summaries_count, "plans": 0},
        recent_activity=recent_activity,
        title="Dashboard")

@app.route("/summary", methods=["GET", "POST"])
@login_required
def summary_page():
    original_text = ""
    summary = None
    message = None
    if request.method == "POST":
        original_text = request.form.get("text", "").strip()
        if not original_text:
            message = "Please enter some text to summarize."
        else:
            result = generate_structured_output(original_text)
            if not result or result.get("error"):
                message = result.get("error", "Error generating summary.")
            else:
                summary = {
                    "title":              result.get("title", "Untitled"),
                    "short_summary":      result.get("explanation", ""),
                    "definition":         result.get("definition", ""),
                    "explanation":        result.get("explanation", ""),
                    "bullet_points":      result.get("bullet_points", []),
                    "important_keywords": result.get("important_keywords", []),
                    "five_mark_answer":   result.get("five_mark_answer", ""),
                    "ten_mark_answer":    result.get("ten_mark_answer", ""),
                    "viva_questions":     result.get("viva_questions", []),
                    "mcqs":               result.get("mcqs", []),
                }
                add_history("Text Summarize", "Manual Input", summary)
    return render_template("summary.html",
        original_text=original_text, summary=summary,
        message=message, title="Smart Summary")

@app.route("/youtube", methods=["GET", "POST"])
@login_required
def youtube_page():
    summary = None
    message = ""

    if request.method == "POST":
        url = request.form.get("youtube_url", "").strip()

        if not url:
            message = "Please enter a YouTube URL."
        else:
            video_id = extract_video_id(url)

            if not video_id:
                message = "Invalid YouTube URL."
            else:
                # 1️⃣ Try transcript first
                full_text, error = fetch_transcript(video_id)

                # 2️⃣ If transcript fails → use audio AI
                if error:
                    print("Transcript failed → Using Audio AI")

                    audio_path, audio_error = download_audio(url)

                    if audio_error:
                        message = f"Audio AI failed: {audio_error}"
                    else:
                        try:
                            client = get_client()

                            with open(audio_path, "rb") as f:
                                transcription = client.audio.transcriptions.create(
                                    file=f,
                                    model="whisper-large-v3"
                                )

                            full_text = transcription.text
                            summary = summarize_youtube_transcript(full_text)
                            add_history("YouTube Audio AI", url, summary)

                        except Exception as e:
                            message = f"Audio AI failed: {str(e)}"

                else:
                    summary = summarize_youtube_transcript(full_text)
                    add_history("YouTube AI", url, summary)

    return render_template("youtube.html",
        summary=summary,
        message=message,
        title="YouTube Structured AI")

import yt_dlp

def download_audio(video_url):
    try:
        output_path = os.path.join(AUDIO_FOLDER, f"{uuid.uuid4()}.mp3")

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_path.replace(".mp3", ".%(ext)s"),
            'quiet': True,
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])

        return output_path, None

    except Exception as e:
        return None, str(e)


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_page():
    summary = None
    message = ""
    if request.method == "POST":
        file = request.files.get("notes_file")
        if not file or not allowed_file(file.filename):
            message = "Invalid file. Please upload a PDF, TXT, or DOCX."
        else:
            filename = secure_filename(file.filename)
            path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(path)
            try:
                text = ""
                ext = filename.rsplit(".", 1)[1].lower()
                if ext == "txt":
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                elif ext == "pdf":
                    if PyPDF2 is None:
                        message = "PyPDF2 not installed. Cannot read PDF."
                    else:
                        reader = PyPDF2.PdfReader(path)
                        for page in reader.pages:
                            text += page.extract_text() or ""
                elif ext == "docx":
                    if Document is None:
                        message = "python-docx not installed. Cannot read DOCX."
                    else:
                        doc = Document(path)
                        for para in doc.paragraphs:
                            text += para.text + "\n"
                if message:
                    pass
                elif len(text.strip()) < 200:
                    message = "File appears unreadable or too short (min 200 characters needed)."
                else:
                    text = clean_text(text)
                    if len(text.split()) > MAX_WORDS_BEFORE_CHUNK:
                        chunks = chunk_text(text)
                        chunk_results = [generate_structured_output(c) for c in chunks]
                        result = merge_chunk_results(chunk_results)
                    else:
                        result = generate_structured_output(text)
                    if not result or result.get("error"):
                        message = result.get("error", "Error generating summary. Please try again.")
                    else:
                        summary = {
                            "title":              result.get("title") or filename,
                            "definition":         result.get("definition", ""),
                            "explanation":        result.get("explanation", ""),
                            "bullet_points":      result.get("bullet_points", []),
                            "important_keywords": result.get("important_keywords", []),
                            "five_mark_answer":   result.get("five_mark_answer", ""),
                            "ten_mark_answer":    result.get("ten_mark_answer", ""),
                            "viva_questions":     result.get("viva_questions", []),
                            "mcqs":               result.get("mcqs", []),
                        }
                        with sqlite3.connect(DB_FILE) as conn:
                            conn.cursor().execute("""
                                INSERT INTO notes (id, user, title, raw_text, summary_json, keywords)
                                VALUES (?, ?, ?, ?, ?, ?)""",
                                (str(uuid.uuid4()), session.get("user", "anonymous"),
                                 summary["title"], text, json.dumps(summary),
                                 json.dumps(summary["important_keywords"][:15])))
                            conn.commit()
                        add_history("Upload AI", filename, summary)
                        message = "Notes processed successfully."
            except Exception as e:
                message = f"Processing error: {str(e)}"
    return render_template("upload.html", summary=summary, message=message)

@app.route("/translate", methods=["POST"])
@login_required
def translate_text():
    data = request.get_json()
    text = data.get("text", "").strip()
    lang = data.get("lang", "hi")
    lang_names = {
        "hi": "Hindi", "mr": "Marathi", "es": "Spanish",
        "fr": "French", "de": "German", "ja": "Japanese", "zh": "Chinese"
    }
    try:
        client = get_client()
        response = client.chat.completions.create(
            model=MODEL_ID,
            messages=[
                {"role": "system", "content": f"Translate the following text to {lang_names.get(lang, lang)}. Return only the translated text, nothing else."},
                {"role": "user",   "content": text}
            ],
            temperature=0.2, max_tokens=2048
        )
        return jsonify({"translated": response.choices[0].message.content.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/planner", methods=["GET"])
@login_required
def planner_page():
    with sqlite3.connect(DB_FILE) as conn:
        tasks = [dict(zip(["id","name","subject","day","time","priority","notes","completed","created_at"], row))
                 for row in conn.cursor().execute(
                     "SELECT id,name,subject,day,time,priority,notes,completed,created_at FROM tasks"
                 ).fetchall()]
    today = datetime.now().strftime("%a")
    return render_template("planner.html", tasks=tasks, today=today, title="Smart Planner")

@app.route("/planner/add", methods=["POST"])
@login_required
def planner_add():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("planner_page"))
    with sqlite3.connect(DB_FILE) as conn:
        conn.cursor().execute(
            "INSERT INTO tasks (name,subject,day,time,priority,notes) VALUES (?,?,?,?,?,?)",
            (name, request.form.get("subject","General"), request.form.get("day","Mon"),
             request.form.get("time","09:00"), request.form.get("priority","medium"),
             request.form.get("notes",""))
        )
        conn.commit()
    return redirect(url_for("planner_page"))

@app.route("/planner/complete/<int:task_id>", methods=["POST"])
@login_required
def planner_complete(task_id):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        current = c.execute("SELECT completed FROM tasks WHERE id=?", (task_id,)).fetchone()
        if current:
            c.execute("UPDATE tasks SET completed=? WHERE id=?", (0 if current[0] else 1, task_id))
            conn.commit()
    return redirect(url_for("planner_page"))

@app.route("/planner/delete/<int:task_id>", methods=["POST"])
@login_required
def planner_delete(task_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.cursor().execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
    return redirect(url_for("planner_page"))

@app.route("/text-to-speech", methods=["POST"])
def text_to_speech():
    text = request.form.get("text", "").strip()
    lang = request.form.get("lang", "en")

    if not text:
        return jsonify({"error": "No text provided"}), 400

    try:
        os.makedirs(AUDIO_FOLDER, exist_ok=True)

        filename = f"{uuid.uuid4()}.mp3"
        filepath = os.path.join(AUDIO_FOLDER, filename)

        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(filepath)

        return jsonify({
            "audio_url": f"/static/audio/{filename}",
            "success": True
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/history")
@login_required
def history_page():
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.cursor().execute(
            "SELECT action, filename, summary, timestamp FROM history ORDER BY timestamp DESC"
        ).fetchall()
    history_parsed = []
    for row in rows:
        try:
            summary_json = json.loads(row[2])
        except Exception:
            summary_json = {"title": row[1], "summary_text": row[2]}
        history_parsed.append({
            "action": row[0], "filename": row[1],
            "summary": summary_json, "timestamp": row[3]
        })
    return render_template("history.html", history=history_parsed, title="History")

if __name__ == "__main__":
    app.run(debug=True)