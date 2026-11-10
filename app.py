from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import qrcode
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("CAPOEIRA_DB_PATH", INSTANCE_DIR / "capoeira.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
TZ = ZoneInfo("America/Sao_Paulo")
WEEKDAYS = [
    (0, "Segunda-feira", "Seg"),
    (1, "Terça-feira", "Ter"),
    (2, "Quarta-feira", "Qua"),
    (3, "Quinta-feira", "Qui"),
    (4, "Sexta-feira", "Sex"),
    (5, "Sábado", "Sáb"),
    (6, "Domingo", "Dom"),
]
WEEKDAY_LABELS = {n: full for n, full, _ in WEEKDAYS}

app = Flask(__name__)
secret_file = INSTANCE_DIR / "flask_secret"
if secret_file.exists():
    app.secret_key = secret_file.read_text(encoding="utf-8").strip()
else:
    generated_secret = secrets.token_hex(32)
    secret_file.write_text(generated_secret, encoding="utf-8")
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    app.secret_key = generated_secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("CAPOEIRA_COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with db_connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS teachers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                qr_token_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                belt TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '',
                belt TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                is_extra INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL REFERENCES teachers(id),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS class_schedules (
                class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
                PRIMARY KEY (class_id, weekday)
            );
            CREATE TABLE IF NOT EXISTS class_teachers (
                class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                PRIMARY KEY (class_id, teacher_id)
            );
            CREATE TABLE IF NOT EXISTS class_students (
                class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                enrolled_at TEXT NOT NULL,
                PRIMARY KEY (class_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS extra_class_links (
                regular_class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                extra_class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                PRIMARY KEY (regular_class_id, extra_class_id),
                CHECK(regular_class_id <> extra_class_id)
            );
            CREATE TABLE IF NOT EXISTS attendance_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL REFERENCES classes(id),
                class_date TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES teachers(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(class_id, class_date)
            );
            CREATE TABLE IF NOT EXISTS attendance_session_students (
                session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES students(id),
                PRIMARY KEY(session_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS attendance_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id),
                target_class_id INTEGER NOT NULL REFERENCES classes(id),
                target_date TEXT NOT NULL,
                source_class_id INTEGER NOT NULL REFERENCES classes(id),
                source_date TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('regular', 'extra_actual', 'extra_credit')),
                source_session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
                marked_by INTEGER NOT NULL REFERENCES teachers(id),
                created_at TEXT NOT NULL,
                UNIQUE(student_id, target_class_id, target_date)
            );
            CREATE INDEX IF NOT EXISTS idx_attendance_target ON attendance_records(target_class_id, target_date);
            CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance_records(student_id, target_date);
            """
        )


init_db()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_local() -> date:
    return datetime.now(TZ).date()


def get_teacher() -> sqlite3.Row | None:
    teacher_id = session.get("teacher_id")
    if not teacher_id:
        return None
    with db_connect() as db:
        return db.execute("SELECT * FROM teachers WHERE id = ? AND active = 1", (teacher_id,)).fetchone()


def is_admin(teacher: sqlite3.Row | None = None) -> bool:
    teacher = teacher or get_teacher()
    return bool(teacher and teacher["is_admin"])


def teacher_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        teacher = get_teacher()
        if not teacher:
            session.clear()
            flash("Escaneie o crachá de um professor para continuar.", "warning")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @teacher_required
    def wrapper(*args, **kwargs):
        if not is_admin():
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def class_access(class_id: int, teacher: sqlite3.Row | None = None) -> bool:
    teacher = teacher or get_teacher()
    if not teacher:
        return False
    if teacher["is_admin"]:
        return True
    with db_connect() as db:
        return bool(db.execute(
            "SELECT 1 FROM class_teachers WHERE class_id = ? AND teacher_id = ?",
            (class_id, teacher["id"]),
        ).fetchone())


def class_manager(class_id: int, teacher: sqlite3.Row | None = None) -> bool:
    teacher = teacher or get_teacher()
    if not teacher:
        return False
    if teacher["is_admin"]:
        return True
    with db_connect() as db:
        row = db.execute("SELECT created_by FROM classes WHERE id = ?", (class_id,)).fetchone()
        if not row:
            return False
        assigned = db.execute(
            "SELECT 1 FROM class_teachers WHERE class_id = ? AND teacher_id = ?",
            (class_id, teacher["id"]),
        ).fetchone()
    return row["created_by"] == teacher["id"] or bool(assigned)


def csrf_value() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


@app.context_processor
def inject_globals():
    teacher = get_teacher()
    return {
        "current_teacher": teacher,
        "is_admin_user": bool(teacher and teacher["is_admin"]),
        "csrf_token": csrf_value,
        "weekdays": WEEKDAYS,
        "today": today_local().isoformat(),
    }


@app.before_request
def protect_posts():
    if request.method == "POST":
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        expected = session.get("csrf_token")
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            abort(400, description="Token de segurança inválido. Atualize a página e tente novamente.")


def teacher_token_for(public_id: str) -> str:
    # Deterministic HMAC keeps a badge re-printable without storing its secret in plaintext.
    key = app.secret_key.encode("utf-8") if isinstance(app.secret_key, str) else app.secret_key
    return hmac.new(key, f"CAPOEIRA-PROF|{public_id}".encode("utf-8"), hashlib.sha256).hexdigest()


def new_teacher_credential() -> tuple[str, str]:
    public_id = str(uuid.uuid4())
    return public_id, teacher_token_for(public_id)


def qr_payload(teacher: sqlite3.Row) -> str:
    return f"CAPOEIRA-PROF|{teacher['public_id']}|{teacher_token_for(teacher['public_id'])}"


def create_badge_pdf(kind: str, person: sqlite3.Row) -> io.BytesIO:
    width, height = 90 * mm, 55 * mm
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    pdf.setTitle(f"Crachá - {person['name']}")
    pdf.setFillColor(colors.HexColor("#111827"))
    pdf.roundRect(3 * mm, 3 * mm, width - 6 * mm, height - 6 * mm, 3 * mm, fill=1, stroke=0)
    pdf.setFillColor(colors.HexColor("#F8FAFC"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(7 * mm, height - 10 * mm, "CAPOEIRA CHAMADA")
    role = "PROFESSOR" if kind == "teacher" else "ALUNO"
    pdf.setFillColor(colors.HexColor("#A7F3D0"))
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawString(7 * mm, height - 15 * mm, role)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(7 * mm, height - 25 * mm, person["name"][:28])
    pdf.setFont("Helvetica", 9)
    nickname = person["nickname"] or "—"
    pdf.drawString(7 * mm, height - 32 * mm, f"Apelido: {nickname[:25]}")
    pdf.drawString(7 * mm, height - 38 * mm, f"Corda: {person['belt'] or 'Não informada'}"[:38])
    qr = qrcode.QRCode(box_size=7, border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    if kind == "teacher":
        payload = qr_payload(person)
    else:
        payload = f"CAPOEIRA-ALUNO|{person['public_id']}"
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    qr_buffer = io.BytesIO()
    image.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    from reportlab.lib.utils import ImageReader
    pdf.drawImage(ImageReader(qr_buffer), width - 34 * mm, 9 * mm, 26 * mm, 26 * mm, preserveAspectRatio=True, anchor="c")
    pdf.setFillColor(colors.HexColor("#CBD5E1"))
    pdf.setFont("Helvetica", 5.5)
    pdf.drawString(7 * mm, 7 * mm, "Identificação individual • Apresente o QR Code para identificação")
    pdf.save()
    buffer.seek(0)
    return buffer


def parse_qr_credential(value: str) -> tuple[str, str] | None:
    parts = (value or "").strip().split("|")
    if len(parts) != 3 or parts[0] != "CAPOEIRA-PROF":
        return None
    return parts[1], parts[2]


def parse_weekday_values(values) -> list[int]:
    result = []
    for value in values:
        try:
            number = int(value)
            if 0 <= number <= 6 and number not in result:
                result.append(number)
        except (TypeError, ValueError):
            continue
    return sorted(result)


def get_class_weekdays(db: sqlite3.Connection, class_id: int) -> list[int]:
    return [r[0] for r in db.execute(
        "SELECT weekday FROM class_schedules WHERE class_id = ? ORDER BY weekday", (class_id,)
    ).fetchall()]


def get_roster(db: sqlite3.Connection, class_row: sqlite3.Row) -> list[sqlite3.Row]:
    if not class_row["is_extra"]:
        return db.execute(
            "SELECT s.* FROM students s JOIN class_students cs ON cs.student_id=s.id "
            "WHERE cs.class_id=? AND s.active=1 ORDER BY COALESCE(NULLIF(s.nickname,''),s.name), s.name",
            (class_row["id"],),
        ).fetchall()
    return db.execute(
        """SELECT DISTINCT s.* FROM students s
           JOIN class_students cs ON cs.student_id=s.id
           WHERE s.active=1 AND (
             cs.class_id=? OR cs.class_id IN (
               SELECT regular_class_id FROM extra_class_links WHERE extra_class_id=?
             )
           )
           ORDER BY COALESCE(NULLIF(s.nickname,''),s.name), s.name""",
        (class_row["id"], class_row["id"]),
    ).fetchall()


def rebuild_attendance_records(db: sqlite3.Connection) -> None:
    """Rebuild the effective presence ledger from the submitted roll-call sessions.

    Regular attendance takes priority over an extra-class credit for the same
    student/class/date. An extra class may credit several regular classes and
    each regular class may have several linked extra classes.
    """
    db.execute("DELETE FROM attendance_records")
    sessions = db.execute(
        """SELECT a.*, c.is_extra FROM attendance_sessions a
           JOIN classes c ON c.id=a.class_id
           ORDER BY c.is_extra ASC, a.class_date ASC, a.id ASC"""
    ).fetchall()
    for item in sessions:
        stamp = item["updated_at"] or item["created_at"]
        attendees = db.execute(
            "SELECT student_id FROM attendance_session_students WHERE session_id=? ORDER BY student_id",
            (item["id"],),
        ).fetchall()
        if not item["is_extra"]:
            for attendee in attendees:
                db.execute(
                    """INSERT OR IGNORE INTO attendance_records
                       (student_id,target_class_id,target_date,source_class_id,source_date,kind,source_session_id,marked_by,created_at)
                       VALUES (?,?,?,?,?,'regular',?,?,?)""",
                    (attendee["student_id"], item["class_id"], item["class_date"], item["class_id"],
                     item["class_date"], item["id"], item["created_by"], stamp),
                )
            continue

        linked_regulars = db.execute(
            "SELECT c.id FROM classes c JOIN extra_class_links l ON l.regular_class_id=c.id WHERE l.extra_class_id=? AND c.active=1",
            (item["class_id"],),
        ).fetchall()
        session_date = date.fromisoformat(item["class_date"])
        monday = session_date - timedelta(days=session_date.weekday())
        sunday = monday + timedelta(days=6)
        for attendee in attendees:
            student_id = attendee["student_id"]
            # The extra class itself keeps its own attendance record.
            db.execute(
                """INSERT OR IGNORE INTO attendance_records
                   (student_id,target_class_id,target_date,source_class_id,source_date,kind,source_session_id,marked_by,created_at)
                   VALUES (?,?,?,?,?,'extra_actual',?,?,?)""",
                (student_id, item["class_id"], item["class_date"], item["class_id"],
                 item["class_date"], item["id"], item["created_by"], stamp),
            )
            for regular in linked_regulars:
                regular_id = regular["id"]
                enrolled = db.execute(
                    "SELECT 1 FROM class_students WHERE class_id=? AND student_id=?",
                    (regular_id, student_id),
                ).fetchone()
                if not enrolled:
                    continue
                scheduled = get_class_weekdays(db, regular_id)
                for offset in range(7):
                    target_date = monday + timedelta(days=offset)
                    if target_date.weekday() not in scheduled:
                        continue
                    db.execute(
                        """INSERT OR IGNORE INTO attendance_records
                           (student_id,target_class_id,target_date,source_class_id,source_date,kind,source_session_id,marked_by,created_at)
                           VALUES (?,?,?,?,?,'extra_credit',?,?,?)""",
                        (student_id, regular_id, target_date.isoformat(), item["class_id"],
                         item["class_date"], item["id"], item["created_by"], stamp),
                    )


def get_class_or_404(db: sqlite3.Connection, class_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM classes WHERE id=? AND active=1", (class_id,)).fetchone()
    if not row:
        abort(404)
    return row


@app.get("/")
def index():
    with db_connect() as db:
        teacher_count = db.execute("SELECT COUNT(*) FROM teachers").fetchone()[0]
    if teacher_count == 0:
        return redirect(url_for("setup"))
    if get_teacher():
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    with db_connect() as db:
        count = db.execute("SELECT COUNT(*) FROM teachers").fetchone()[0]
    if count:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        nickname = request.form.get("nickname", "").strip()
        belt = request.form.get("belt", "").strip()
        if not name:
            flash("Informe o nome do administrador.", "danger")
            return render_template("setup.html")
        public_id, token = new_teacher_credential()
        with db_connect() as db:
            cur = db.execute(
                """INSERT INTO teachers(public_id,qr_token_hash,name,nickname,belt,is_admin,created_at)
                   VALUES(?,?,?,?,?,1,?)""",
                (public_id, hashlib.sha256(token.encode()).hexdigest(), name, nickname, belt, now_iso()),
            )
            teacher_id = cur.lastrowid
        session["teacher_id"] = teacher_id
        session[f"badge_token_{teacher_id}"] = token
        flash("Administrador criado. Guarde o crachá: ele será usado para acessar o sistema.", "success")
        return redirect(url_for("teacher_badge", teacher_id=teacher_id))
    return render_template("setup.html")


@app.post("/api/scan")
def api_scan():
    payload = request.get_json(silent=True) or {}
    credential = parse_qr_credential(payload.get("qr_value", ""))
    if not credential:
        return jsonify(ok=False, error="QR Code não reconhecido como crachá de professor."), 400
    public_id, token = credential
    with db_connect() as db:
        teacher = db.execute(
            "SELECT * FROM teachers WHERE public_id=? AND active=1", (public_id,)
        ).fetchone()
        if not teacher or not secrets.compare_digest(
            teacher["qr_token_hash"], hashlib.sha256(token.encode()).hexdigest()
        ):
            return jsonify(ok=False, error="Crachá inválido ou revogado."), 401
    session.clear()
    session["teacher_id"] = teacher["id"]
    session["csrf_token"] = secrets.token_urlsafe(24)
    return jsonify(ok=True, redirect=url_for("dashboard"), name=teacher["name"])


@app.post("/api/classes/<int:class_id>/scan-student")
@teacher_required
def api_scan_student(class_id):
    """Validate a student badge and confirm the student belongs to this roll call."""
    if not class_access(class_id):
        abort(403)
    payload = request.get_json(silent=True) or {}
    raw_qr = str(payload.get("qr_value") or "").strip()
    parts = raw_qr.split("|")
    if len(parts) != 2 or parts[0] != "CAPOEIRA-ALUNO" or not parts[1]:
        return jsonify(ok=False, error="QR Code não reconhecido como crachá de aluno."), 400

    with db_connect() as db:
        class_row = get_class_or_404(db, class_id)
        student = db.execute(
            "SELECT * FROM students WHERE public_id=? AND active=1", (parts[1],)
        ).fetchone()
        if not student:
            return jsonify(ok=False, error="Crachá de aluno inválido ou cadastro inativo."), 404
        is_on_roster = any(row["id"] == student["id"] for row in get_roster(db, class_row))
        if not is_on_roster:
            return jsonify(
                ok=False,
                error="Este aluno não está matriculado nesta turma nem nas turmas vinculadas.",
            ), 409

    return jsonify(
        ok=True,
        student={
            "id": student["id"],
            "name": student["name"],
            "nickname": student["nickname"],
            "belt": student["belt"],
        },
    )


@app.post("/api/classes/<int:class_id>/attendance/quick-student")
@teacher_required
def api_quick_student(class_id):
    """Create a student and enroll them in this class without leaving the roll call."""
    if not class_access(class_id):
        abort(403)
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    nickname = str(payload.get("nickname") or "").strip()
    belt = str(payload.get("belt") or "").strip()
    if not name:
        return jsonify(ok=False, error="Informe o nome completo do aluno."), 400
    if len(name) > 120 or len(nickname) > 80 or len(belt) > 80:
        return jsonify(ok=False, error="Um dos campos excede o tamanho permitido."), 400

    enroll_regular_ids = payload.get("regular_class_ids") or []
    if not isinstance(enroll_regular_ids, list):
        enroll_regular_ids = []
    parsed_regular_ids = set()
    for value in enroll_regular_ids:
        try:
            parsed_regular_ids.add(int(value))
        except (TypeError, ValueError):
            continue

    with db_connect() as db:
        class_row = get_class_or_404(db, class_id)
        linked_regulars = {
            row["regular_class_id"]
            for row in db.execute(
                "SELECT regular_class_id FROM extra_class_links WHERE extra_class_id=?",
                (class_id,),
            ).fetchall()
        } if class_row["is_extra"] else set()
        allowed_regular_ids = parsed_regular_ids.intersection(linked_regulars)

        public_id = str(uuid.uuid4())
        cur = db.execute(
            "INSERT INTO students(public_id,name,nickname,belt,created_at) VALUES(?,?,?,?,?)",
            (public_id, name, nickname, belt, now_iso()),
        )
        student_id = cur.lastrowid
        stamp = now_iso()
        # Enroll in the current roll-call class so the new student is immediately eligible.
        db.execute(
            "INSERT INTO class_students(class_id,student_id,enrolled_at) VALUES(?,?,?)",
            (class_id, student_id, stamp),
        )
        # For an extra class, optionally enroll the student in the linked regular classes
        # as well, which makes them eligible for the weekly extra-class credit.
        for regular_id in sorted(allowed_regular_ids):
            db.execute(
                "INSERT OR IGNORE INTO class_students(class_id,student_id,enrolled_at) VALUES(?,?,?)",
                (regular_id, student_id, stamp),
            )
        student = db.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()

    return jsonify(
        ok=True,
        message="Aluno cadastrado e adicionado à chamada.",
        student={
            "id": student["id"],
            "public_id": student["public_id"],
            "name": student["name"],
            "nickname": student["nickname"],
            "belt": student["belt"],
        },
        badge_url=url_for("student_badge", student_id=student_id),
        enrolled_regular_count=len(allowed_regular_ids),
    ), 201


@app.get("/dashboard")
@teacher_required
def dashboard():
    teacher = get_teacher()
    with db_connect() as db:
        if teacher["is_admin"]:
            classes = db.execute(
                "SELECT * FROM classes WHERE active=1 ORDER BY is_extra,name"
            ).fetchall()
        else:
            classes = db.execute(
                """SELECT c.* FROM classes c JOIN class_teachers ct ON ct.class_id=c.id
                   WHERE c.active=1 AND ct.teacher_id=? ORDER BY c.is_extra,c.name""",
                (teacher["id"],),
            ).fetchall()
        class_data = []
        for c in classes:
            days = get_class_weekdays(db, c["id"])
            count = len(get_roster(db, c))
            linked = db.execute("SELECT COUNT(*) FROM extra_class_links WHERE extra_class_id=?", (c["id"],)).fetchone()[0]
            class_data.append({"class": c, "days": [WEEKDAY_LABELS[d] for d in days], "count": count, "linked_count": linked})
        student_count = db.execute("SELECT COUNT(*) FROM students WHERE active=1").fetchone()[0]
        teacher_count = db.execute("SELECT COUNT(*) FROM teachers WHERE active=1").fetchone()[0]
    return render_template("dashboard.html", class_data=class_data, student_count=student_count, teacher_count=teacher_count)


@app.post("/logout")
@teacher_required
def logout():
    session.clear()
    flash("Sessão encerrada.", "success")
    return redirect(url_for("index"))


@app.get("/classes")
@teacher_required
def classes_list():
    with db_connect() as db:
        classes = db.execute(
            "SELECT * FROM classes WHERE active=1 ORDER BY is_extra,name"
        ).fetchall()
        rows = []
        for c in classes:
            days = get_class_weekdays(db, c["id"])
            linked = db.execute(
                """SELECT r.name FROM classes r JOIN extra_class_links l ON l.regular_class_id=r.id
                   WHERE l.extra_class_id=? ORDER BY r.name""", (c["id"],)
            ).fetchall() if c["is_extra"] else db.execute(
                """SELECT e.name FROM classes e JOIN extra_class_links l ON l.extra_class_id=e.id
                   WHERE l.regular_class_id=? ORDER BY e.name""", (c["id"],)
            ).fetchall()
            rows.append({"class": c, "days": [WEEKDAY_LABELS[d] for d in days],
                         "linked": [x["name"] for x in linked], "can_validate": class_access(c["id"])})
    return render_template("classes.html", classes=rows)


@app.route("/classes/new", methods=["GET", "POST"])
@teacher_required
def class_new():
    teacher = get_teacher()
    with db_connect() as db:
        regulars = db.execute("SELECT id,name FROM classes WHERE active=1 AND is_extra=0 ORDER BY name").fetchall()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        is_extra = 1 if request.form.get("class_type") == "extra" else 0
        weekdays = parse_weekday_values(request.form.getlist("weekdays"))
        links = [int(x) for x in request.form.getlist("regular_classes") if x.isdigit()]
        valid_regular_ids = {row["id"] for row in regulars}
        links = sorted(set(links).intersection(valid_regular_ids))
        if not name or not weekdays:
            flash("Informe o nome e pelo menos um dia de aula.", "danger")
            return render_template("class_form.html", class_row=None, regulars=regulars, selected_days=weekdays, selected_links=links)
        with db_connect() as db:
            cur = db.execute(
                "INSERT INTO classes(name,is_extra,created_by,created_at) VALUES(?,?,?,?)",
                (name, is_extra, teacher["id"], now_iso()),
            )
            class_id = cur.lastrowid
            db.execute("INSERT INTO class_teachers(class_id,teacher_id) VALUES(?,?)", (class_id, teacher["id"]))
            for day in weekdays:
                db.execute("INSERT INTO class_schedules(class_id,weekday) VALUES(?,?)", (class_id, day))
            if is_extra:
                for regular_id in links:
                    valid = db.execute("SELECT 1 FROM classes WHERE id=? AND is_extra=0 AND active=1", (regular_id,)).fetchone()
                    if valid:
                        db.execute("INSERT OR IGNORE INTO extra_class_links(regular_class_id,extra_class_id) VALUES(?,?)", (regular_id, class_id))
        flash("Turma cadastrada.", "success")
        return redirect(url_for("class_detail", class_id=class_id))
    return render_template("class_form.html", class_row=None, regulars=regulars, selected_days=[], selected_links=[])


@app.get("/classes/<int:class_id>")
@teacher_required
def class_detail(class_id):
    with db_connect() as db:
        c = get_class_or_404(db, class_id)
        days = get_class_weekdays(db, class_id)
        enrolled = get_roster(db, c)
        direct_ids = {r[0] for r in db.execute("SELECT student_id FROM class_students WHERE class_id=?", (class_id,)).fetchall()}
        roster_ids = [student["id"] for student in enrolled]
        if roster_ids:
            placeholders = ",".join("?" for _ in roster_ids)
            available_students = db.execute(
                f"SELECT * FROM students WHERE active=1 AND id NOT IN ({placeholders}) ORDER BY name", roster_ids
            ).fetchall()
        else:
            available_students = db.execute("SELECT * FROM students WHERE active=1 ORDER BY name").fetchall()
        if c["is_extra"]:
            linked = db.execute(
                "SELECT c.* FROM classes c JOIN extra_class_links l ON l.regular_class_id=c.id WHERE l.extra_class_id=? ORDER BY c.name", (class_id,)
            ).fetchall()
            link_options = db.execute(
                "SELECT id,name FROM classes WHERE active=1 AND is_extra=0 ORDER BY name"
            ).fetchall()
            selected_link_ids = {row["id"] for row in linked}
        else:
            linked = db.execute(
                "SELECT c.* FROM classes c JOIN extra_class_links l ON l.extra_class_id=c.id WHERE l.regular_class_id=? ORDER BY c.name", (class_id,)
            ).fetchall()
            link_options = db.execute(
                "SELECT id,name FROM classes WHERE active=1 AND is_extra=1 ORDER BY name"
            ).fetchall()
            selected_link_ids = {row["id"] for row in linked}
        teachers = db.execute(
            "SELECT t.* FROM teachers t JOIN class_teachers ct ON ct.teacher_id=t.id WHERE ct.class_id=? AND t.active=1 ORDER BY t.name", (class_id,)
        ).fetchall()
        all_teachers = db.execute("SELECT * FROM teachers WHERE active=1 ORDER BY name").fetchall() if is_admin() else []
    return render_template(
        "class_detail.html", class_row=c, days=[WEEKDAY_LABELS[d] for d in days], enrolled=enrolled,
        available_students=available_students, linked=linked, link_options=link_options,
        selected_link_ids=selected_link_ids, teachers=teachers, all_teachers=all_teachers,
        direct_ids=direct_ids,
        can_validate=class_access(class_id), can_manage=class_manager(class_id),
    )


@app.post("/classes/<int:class_id>/enroll")
@teacher_required
def class_enroll(class_id):
    student_id = request.form.get("student_id", type=int)
    with db_connect() as db:
        get_class_or_404(db, class_id)
        student = db.execute("SELECT id FROM students WHERE id=? AND active=1", (student_id,)).fetchone()
        if not student:
            flash("Aluno não encontrado.", "danger")
        else:
            db.execute("INSERT OR IGNORE INTO class_students(class_id,student_id,enrolled_at) VALUES(?,?,?)", (class_id, student_id, now_iso()))
            flash("Aluno adicionado à turma.", "success")
    return redirect(url_for("class_detail", class_id=class_id))


@app.post("/classes/<int:class_id>/unenroll/<int:student_id>")
@teacher_required
def class_unenroll(class_id, student_id):
    with db_connect() as db:
        get_class_or_404(db, class_id)
        db.execute("DELETE FROM class_students WHERE class_id=? AND student_id=?", (class_id, student_id))
    flash("Aluno removido da turma. O histórico de presenças foi preservado.", "success")
    return redirect(url_for("class_detail", class_id=class_id))


@app.post("/classes/<int:class_id>/links")
@teacher_required
def class_update_links(class_id):
    """Update extra/regular relationships from either side of the relationship."""
    if not class_manager(class_id):
        abort(403)
    selected_ids = sorted({int(value) for value in request.form.getlist("linked_class_ids") if value.isdigit()})
    with db_connect() as db:
        current = get_class_or_404(db, class_id)
        if current["is_extra"]:
            valid_rows = db.execute(
                "SELECT id FROM classes WHERE active=1 AND is_extra=0"
            ).fetchall()
            valid_ids = {row["id"] for row in valid_rows}
            db.execute("DELETE FROM extra_class_links WHERE extra_class_id=?", (class_id,))
            for regular_id in sorted(set(selected_ids).intersection(valid_ids)):
                db.execute(
                    "INSERT OR IGNORE INTO extra_class_links(regular_class_id,extra_class_id) VALUES(?,?)",
                    (regular_id, class_id),
                )
        else:
            valid_rows = db.execute(
                "SELECT id FROM classes WHERE active=1 AND is_extra=1"
            ).fetchall()
            valid_ids = {row["id"] for row in valid_rows}
            db.execute("DELETE FROM extra_class_links WHERE regular_class_id=?", (class_id,))
            for extra_id in sorted(set(selected_ids).intersection(valid_ids)):
                db.execute(
                    "INSERT OR IGNORE INTO extra_class_links(regular_class_id,extra_class_id) VALUES(?,?)",
                    (class_id, extra_id),
                )
        # Credits are derived from sessions + current links/enrollments. Rebuild
        # to add or remove the affected weekly credits without duplicating rows.
        rebuild_attendance_records(db)
    flash("Vínculos entre turmas atualizados. As presenças compensadas foram recalculadas.", "success")
    return redirect(url_for("class_detail", class_id=class_id))


@app.post("/classes/<int:class_id>/teachers")
@admin_required
def class_assign_teachers(class_id):
    teacher_ids = [int(x) for x in request.form.getlist("teacher_ids") if x.isdigit()]
    with db_connect() as db:
        get_class_or_404(db, class_id)
        db.execute("DELETE FROM class_teachers WHERE class_id=?", (class_id,))
        for teacher_id in teacher_ids:
            db.execute("INSERT OR IGNORE INTO class_teachers(class_id,teacher_id) SELECT ?,id FROM teachers WHERE id=? AND active=1", (class_id, teacher_id))
    flash("Professores autorizados atualizados.", "success")
    return redirect(url_for("class_detail", class_id=class_id))


@app.get("/students")
@teacher_required
def students_list():
    query = request.args.get("q", "").strip()
    with db_connect() as db:
        if query:
            students = db.execute(
                "SELECT * FROM students WHERE active=1 AND (name LIKE ? OR nickname LIKE ?) ORDER BY name",
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
        else:
            students = db.execute("SELECT * FROM students WHERE active=1 ORDER BY name").fetchall()
    return render_template("students.html", students=students, query=query)


@app.route("/students/new", methods=["GET", "POST"])
@teacher_required
def student_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        nickname = request.form.get("nickname", "").strip()
        belt = request.form.get("belt", "").strip()
        if not name:
            flash("O nome do aluno é obrigatório.", "danger")
        else:
            with db_connect() as db:
                cur = db.execute(
                    "INSERT INTO students(public_id,name,nickname,belt,created_at) VALUES(?,?,?,?,?)",
                    (str(uuid.uuid4()), name, nickname, belt, now_iso()),
                )
                student_id = cur.lastrowid
            flash("Aluno cadastrado.", "success")
            return redirect(url_for("student_detail", student_id=student_id))
    return render_template("student_form.html", student=None)


@app.get("/students/<int:student_id>")
@teacher_required
def student_detail(student_id):
    with db_connect() as db:
        student = db.execute("SELECT * FROM students WHERE id=? AND active=1", (student_id,)).fetchone()
        if not student:
            abort(404)
        classes = db.execute(
            "SELECT c.* FROM classes c JOIN class_students cs ON cs.class_id=c.id WHERE cs.student_id=? AND c.active=1 ORDER BY c.is_extra,c.name",
            (student_id,),
        ).fetchall()
        attendance = db.execute(
            """SELECT ar.*, tc.name AS target_name, sc.name AS source_name
               FROM attendance_records ar JOIN classes tc ON tc.id=ar.target_class_id
               JOIN classes sc ON sc.id=ar.source_class_id
               WHERE ar.student_id=? ORDER BY ar.target_date DESC LIMIT 30""", (student_id,)
        ).fetchall()
    return render_template("student_detail.html", student=student, classes=classes, attendance=attendance)


@app.route("/students/<int:student_id>/edit", methods=["GET", "POST"])
@teacher_required
def student_edit(student_id):
    with db_connect() as db:
        student = db.execute("SELECT * FROM students WHERE id=? AND active=1", (student_id,)).fetchone()
    if not student:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        nickname = request.form.get("nickname", "").strip()
        belt = request.form.get("belt", "").strip()
        if not name:
            flash("O nome é obrigatório.", "danger")
        else:
            with db_connect() as db:
                db.execute("UPDATE students SET name=?,nickname=?,belt=? WHERE id=?", (name, nickname, belt, student_id))
            flash("Cadastro atualizado.", "success")
            return redirect(url_for("student_detail", student_id=student_id))
    return render_template("student_form.html", student=student)


@app.get("/teachers")
@admin_required
def teachers_list():
    with db_connect() as db:
        teachers = db.execute("SELECT * FROM teachers ORDER BY name").fetchall()
    return render_template("teachers.html", teachers=teachers)


@app.route("/teachers/new", methods=["GET", "POST"])
@admin_required
def teacher_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        nickname = request.form.get("nickname", "").strip()
        belt = request.form.get("belt", "").strip()
        admin_flag = 1 if request.form.get("is_admin") else 0
        if not name:
            flash("O nome do professor é obrigatório.", "danger")
        else:
            public_id, token = new_teacher_credential()
            with db_connect() as db:
                cur = db.execute(
                    "INSERT INTO teachers(public_id,qr_token_hash,name,nickname,belt,is_admin,created_at) VALUES(?,?,?,?,?,?,?)",
                    (public_id, hashlib.sha256(token.encode()).hexdigest(), name, nickname, belt, admin_flag, now_iso()),
                )
                teacher_id = cur.lastrowid
            session[f"badge_token_{teacher_id}"] = token
            flash("Professor cadastrado. Gere o crachá para entregar a credencial de acesso.", "success")
            return redirect(url_for("teacher_badge", teacher_id=teacher_id))
    return render_template("teacher_form.html")


@app.route("/teachers/<int:teacher_id>/edit", methods=["GET", "POST"])
@admin_required
def teacher_edit(teacher_id):
    with db_connect() as db:
        teacher = db.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        nickname = request.form.get("nickname", "").strip()
        belt = request.form.get("belt", "").strip()
        admin_flag = 1 if request.form.get("is_admin") else 0
        active = 1 if request.form.get("active") else 0
        if not name:
            flash("O nome é obrigatório.", "danger")
        else:
            with db_connect() as db:
                db.execute("UPDATE teachers SET name=?,nickname=?,belt=?,is_admin=?,active=? WHERE id=?",
                           (name, nickname, belt, admin_flag, active, teacher_id))
                if not active and session.get("teacher_id") == teacher_id:
                    session.clear()
            flash("Professor atualizado.", "success")
            return redirect(url_for("teachers_list"))
    return render_template("teacher_form.html", teacher=teacher)


@app.post("/teachers/<int:teacher_id>/regenerate-badge")
@admin_required
def teacher_regenerate_badge(teacher_id):
    with db_connect() as db:
        teacher = db.execute("SELECT id FROM teachers WHERE id=? AND active=1", (teacher_id,)).fetchone()
        if not teacher:
            abort(404)
        public_id = str(uuid.uuid4())
        token = teacher_token_for(public_id)
        db.execute("UPDATE teachers SET public_id=?,qr_token_hash=? WHERE id=?",
                   (public_id, hashlib.sha256(token.encode()).hexdigest(), teacher_id))
    session.pop(f"badge_token_{teacher_id}", None)
    flash("Credencial anterior revogada. Emita o novo crachá.", "success")
    return redirect(url_for("teacher_badge", teacher_id=teacher_id))


@app.get("/badges/student/<int:student_id>.pdf")
@teacher_required
def student_badge(student_id):
    with db_connect() as db:
        student = db.execute("SELECT * FROM students WHERE id=? AND active=1", (student_id,)).fetchone()
    if not student:
        abort(404)
    pdf = create_badge_pdf("student", student)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"cracha-aluno-{student_id}.pdf")


@app.get("/badges/teacher/<int:teacher_id>.pdf")
@teacher_required
def teacher_badge(teacher_id):
    current = get_teacher()
    if not current["is_admin"] and current["id"] != teacher_id:
        abort(403)
    with db_connect() as db:
        teacher = db.execute("SELECT * FROM teachers WHERE id=? AND active=1", (teacher_id,)).fetchone()
    if not teacher:
        abort(404)
    pdf = create_badge_pdf("teacher", teacher)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"cracha-professor-{teacher_id}.pdf")


@app.route("/classes/<int:class_id>/attendance", methods=["GET", "POST"])
@teacher_required
def attendance(class_id):
    teacher = get_teacher()
    if not class_access(class_id, teacher):
        abort(403)
    with db_connect() as db:
        c = get_class_or_404(db, class_id)
        weekdays = get_class_weekdays(db, class_id)
        today = today_local()
        selected_date = request.form.get("class_date") if request.method == "POST" else request.args.get("date")
        if not selected_date:
            selected_date = today.isoformat()
        try:
            date_obj = date.fromisoformat(selected_date)
        except (ValueError, TypeError):
            date_obj = today
            selected_date = today.isoformat()
        selected_date_is_scheduled = date_obj.weekday() in weekdays
        roster = get_roster(db, c)
        existing_session = db.execute(
            "SELECT * FROM attendance_sessions WHERE class_id=? AND class_date=?", (class_id, selected_date)
        ).fetchone()
        checked = set()
        if existing_session:
            checked = {r[0] for r in db.execute(
                "SELECT student_id FROM attendance_session_students WHERE session_id=?", (existing_session["id"],)
            ).fetchall()}
        linked_regular_classes = []
        if c["is_extra"]:
            linked_regular_classes = db.execute(
                "SELECT c.id,c.name FROM classes c JOIN extra_class_links l ON l.regular_class_id=c.id "
                "WHERE l.extra_class_id=? AND c.active=1 ORDER BY c.name",
                (class_id,),
            ).fetchall()
        if request.method == "POST":
            raw_ids = {int(x) for x in request.form.getlist("student_ids") if x.isdigit()}
            roster_ids = {s["id"] for s in roster}
            present_ids = raw_ids.intersection(roster_ids)
            stamp = now_iso()
            if existing_session:
                session_id = existing_session["id"]
                db.execute("UPDATE attendance_sessions SET created_by=?,updated_at=? WHERE id=?",
                           (teacher["id"], stamp, session_id))
            else:
                cur = db.execute(
                    "INSERT INTO attendance_sessions(class_id,class_date,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (class_id, selected_date, teacher["id"], stamp, stamp),
                )
                session_id = cur.lastrowid
            db.execute("DELETE FROM attendance_session_students WHERE session_id=?", (session_id,))
            for student_id in present_ids:
                db.execute("INSERT INTO attendance_session_students(session_id,student_id) VALUES(?,?)", (session_id, student_id))
            rebuild_attendance_records(db)
            flash(f"Chamada salva: {len(present_ids)} presença(s) registrada(s).", "success")
            return redirect(url_for("attendance", class_id=class_id, date=selected_date))
    return render_template(
        "attendance.html", class_row=c, roster=roster, checked=checked,
        selected_date=selected_date, date_obj=date_obj, weekdays=[WEEKDAY_LABELS[d] for d in weekdays],
        selected_date_is_scheduled=selected_date_is_scheduled, existing_session=bool(existing_session),
        linked_regular_classes=linked_regular_classes,
    )


def count_scheduled_days(start_date: date, end_date: date, weekdays: list[int]) -> int:
    """Count occurrences of configured weekdays in an inclusive date range."""
    if end_date < start_date or not weekdays:
        return 0
    total_days = (end_date - start_date).days + 1
    full_weeks, remaining_days = divmod(total_days, 7)
    total = full_weeks * len(set(weekdays))
    configured = set(weekdays)
    total += sum(1 for offset in range(remaining_days)
                 if (start_date.weekday() + offset) % 7 in configured)
    return total


@app.get("/reports")
@teacher_required
def reports_list():
    teacher = get_teacher()
    with db_connect() as db:
        if teacher["is_admin"]:
            classes = db.execute(
                "SELECT id,name,is_extra FROM classes WHERE active=1 ORDER BY is_extra,name"
            ).fetchall()
        else:
            classes = db.execute(
                """SELECT DISTINCT c.id,c.name,c.is_extra FROM classes c
                   LEFT JOIN class_teachers ct ON ct.class_id=c.id
                   WHERE c.active=1 AND (ct.teacher_id=? OR c.created_by=?)
                   ORDER BY c.is_extra,c.name""",
                (teacher["id"], teacher["id"]),
            ).fetchall()
    return render_template("reports.html", classes=classes)


@app.get("/reports/combined")
@teacher_required
def combined_report():
    """Build one report from several classes, respecting each student's enrollments."""
    raw_ids = request.args.getlist("class_ids")
    class_ids = sorted({int(value) for value in raw_ids if value.isdigit()})
    if not class_ids:
        flash("Selecione pelo menos uma turma para gerar o relatório combinado.", "warning")
        return redirect(url_for("reports_list"))
    if any(not class_access(class_id) for class_id in class_ids):
        abort(403)

    today = today_local()
    period = request.args.get("period", "monthly").lower()
    if period not in {"monthly", "annual", "custom"}:
        period = "monthly"
    if period == "annual":
        start_date, end_date = date(today.year, 1, 1), today
        period_label = "ANUAL"
    elif period == "custom":
        try:
            start_date = date.fromisoformat(request.args.get("start", ""))
            end_date = date.fromisoformat(request.args.get("end", ""))
        except (ValueError, TypeError):
            flash("Informe datas válidas para o período personalizado.", "warning")
            return redirect(url_for("reports_list"))
        if start_date > end_date:
            start_date, end_date = end_date, start_date
            flash("As datas foram invertidas para manter o início antes do fim.", "warning")
        period_label = f"DE {start_date.strftime('%d/%m/%Y')} A {end_date.strftime('%d/%m/%Y')}"
    else:
        start_date, end_date = date(today.year, today.month, 1), today
        period_label = "MENSAL"

    start, end = start_date.isoformat(), end_date.isoformat()
    with db_connect() as db:
        placeholders = ",".join("?" for _ in class_ids)
        selected_classes = db.execute(
            f"SELECT * FROM classes WHERE active=1 AND id IN ({placeholders}) ORDER BY is_extra,name",
            class_ids,
        ).fetchall()
        if len(selected_classes) != len(class_ids):
            abort(404)
        regular_classes = [item for item in selected_classes if not item["is_extra"]]
        extra_classes = [item for item in selected_classes if item["is_extra"]]
        regular_ids = [item["id"] for item in regular_classes]
        extra_ids = [item["id"] for item in extra_classes]

        regular_metrics: dict[int, int] = {}
        total_regular_lessons = 0
        for class_row in regular_classes:
            created_date = date.fromisoformat(class_row["created_at"][:10])
            count_start = max(start_date, created_date)
            calendar_total = count_scheduled_days(
                count_start, end_date, get_class_weekdays(db, class_row["id"])
            )
            saved_total = db.execute(
                "SELECT COUNT(*) FROM attendance_sessions WHERE class_id=? AND class_date BETWEEN ? AND ?",
                (class_row["id"], start, end),
            ).fetchone()[0]
            regular_metrics[class_row["id"]] = max(calendar_total, saved_total)
            total_regular_lessons += regular_metrics[class_row["id"]]

        extra_metrics: dict[int, int] = {}
        extra_only_rosters: dict[int, set[int]] = {}
        total_extra_lessons = 0
        for class_row in extra_classes:
            extra_metrics[class_row["id"]] = db.execute(
                "SELECT COUNT(*) FROM attendance_sessions WHERE class_id=? AND class_date BETWEEN ? AND ?",
                (class_row["id"], start, end),
            ).fetchone()[0]
            total_extra_lessons += extra_metrics[class_row["id"]]
            linked_regular_ids = [
                row[0] for row in db.execute(
                    "SELECT regular_class_id FROM extra_class_links WHERE extra_class_id=?",
                    (class_row["id"],),
                ).fetchall()
            ]
            direct_extra_members = {
                row[0] for row in db.execute(
                    "SELECT student_id FROM class_students WHERE class_id=?",
                    (class_row["id"],),
                ).fetchall()
            }
            linked_regular_members: set[int] = set()
            if linked_regular_ids:
                linked_placeholders = ",".join("?" for _ in linked_regular_ids)
                linked_regular_members = {
                    row[0] for row in db.execute(
                        f"SELECT DISTINCT student_id FROM class_students WHERE class_id IN ({linked_placeholders})",
                        linked_regular_ids,
                    ).fetchall()
                }
            historical_extra_students = {
                row[0] for row in db.execute(
                    "SELECT DISTINCT student_id FROM attendance_records "
                    "WHERE target_class_id=? AND target_date BETWEEN ? AND ? AND kind='extra_actual'",
                    (class_row["id"], start, end),
                ).fetchall()
            }
            # Only direct extra-class students with no enrollment in any regular
            # class served by this extra, plus historical attendees whose current
            # link is no longer present, belong in the extra-only group.
            extra_only_rosters[class_row["id"]] = (
                direct_extra_members | historical_extra_students
            ) - linked_regular_members

        # Current enrollments define which regular denominators apply to a
        # student. Historical effective records keep former students visible.
        regular_memberships: dict[int, set[int]] = {}
        for class_id in regular_ids:
            members = {
                row[0] for row in db.execute(
                    "SELECT student_id FROM class_students WHERE class_id=?", (class_id,)
                ).fetchall()
            }
            if extra_ids:
                source_clause = f" OR (kind='extra_credit' AND source_class_id IN ({','.join('?' for _ in extra_ids)}))"
                params = [class_id, start, end, *extra_ids]
            else:
                source_clause = ""
                params = [class_id, start, end]
            historical = db.execute(
                "SELECT DISTINCT student_id FROM attendance_records "
                "WHERE target_class_id=? AND target_date BETWEEN ? AND ? "
                f"AND (kind='regular'{source_clause})",
                params,
            ).fetchall()
            members.update(row[0] for row in historical)
            regular_memberships[class_id] = members

        regular_student_classes: dict[int, set[int]] = {}
        for class_id, members in regular_memberships.items():
            for student_id in members:
                regular_student_classes.setdefault(student_id, set()).add(class_id)

        # Students in the selected extra-class rosters who are not enrolled in
        # any selected regular class form an extra-only group. They get only
        # the extra sessions they belong to in their denominator.
        extra_only_memberships: dict[int, set[int]] = {}
        for extra_id, roster_ids in extra_only_rosters.items():
            for student_id in roster_ids:
                if student_id not in regular_student_classes:
                    extra_only_memberships.setdefault(student_id, set()).add(extra_id)

        relevant_student_ids = set(regular_student_classes) | set(extra_only_memberships)
        student_info = {}
        if relevant_student_ids:
            student_placeholders = ",".join("?" for _ in relevant_student_ids)
            student_rows = db.execute(
                f"SELECT id,name,nickname FROM students WHERE id IN ({student_placeholders})",
                sorted(relevant_student_ids),
            ).fetchall()
            student_info = {row["id"]: dict(row) for row in student_rows}

        regular_presence: dict[int, int] = {}
        extra_only_presence: dict[int, int] = {}
        if regular_ids:
            regular_placeholders = ",".join("?" for _ in regular_ids)
            if extra_ids:
                extra_placeholders = ",".join("?" for _ in extra_ids)
                presence_sql = (
                    f"SELECT student_id,COUNT(*) AS total FROM attendance_records "
                    f"WHERE target_class_id IN ({regular_placeholders}) AND target_date BETWEEN ? AND ? "
                    f"AND (kind='regular' OR (kind='extra_credit' AND source_class_id IN ({extra_placeholders}))) "
                    "GROUP BY student_id"
                )
                presence_params = [*regular_ids, start, end, *extra_ids]
            else:
                presence_sql = (
                    f"SELECT student_id,COUNT(*) AS total FROM attendance_records "
                    f"WHERE target_class_id IN ({regular_placeholders}) AND target_date BETWEEN ? AND ? "
                    "AND kind='regular' GROUP BY student_id"
                )
                presence_params = [*regular_ids, start, end]
            regular_presence = {
                row["student_id"]: row["total"]
                for row in db.execute(presence_sql, presence_params).fetchall()
            }

        if extra_ids:
            extra_placeholders = ",".join("?" for _ in extra_ids)
            actual_rows = db.execute(
                f"SELECT student_id,COUNT(*) AS total FROM attendance_records "
                f"WHERE target_class_id IN ({extra_placeholders}) AND target_date BETWEEN ? AND ? "
                "AND kind='extra_actual' GROUP BY student_id",
                [*extra_ids, start, end],
            ).fetchall()
            extra_only_presence = {row["student_id"]: row["total"] for row in actual_rows}

    regular_summaries = []
    for student_id in regular_student_classes:
        info = student_info.get(student_id)
        if not info:
            continue
        denominator = sum(regular_metrics[class_id] for class_id in regular_student_classes[student_id])
        present = regular_presence.get(student_id, 0)
        percent = (present / denominator * 100) if denominator else 0
        display_name = (info["nickname"] or info["name"]).strip()
        regular_summaries.append({
            "student_id": student_id, "display_name": display_name,
            "present": present, "total": denominator, "percent": percent,
            "group": "regular",
        })

    extra_only_summaries = []
    for student_id, memberships in extra_only_memberships.items():
        info = student_info.get(student_id)
        if not info:
            continue
        denominator = sum(extra_metrics[extra_id] for extra_id in memberships)
        present = extra_only_presence.get(student_id, 0)
        percent = (present / denominator * 100) if denominator else 0
        display_name = (info["nickname"] or info["name"]).strip()
        extra_only_summaries.append({
            "student_id": student_id, "display_name": display_name,
            "present": present, "total": denominator, "percent": percent,
            "group": "extra",
        })

    sort_key = lambda item: item["display_name"].casefold()
    regular_summaries.sort(key=sort_key)
    extra_only_summaries.sort(key=sort_key)
    selected_names = [item["name"] for item in selected_classes]
    report_lines = [
        f"**RELATÓRIO DE FREQUÊNCIA {period_label}**", "",
        f"Turmas: {', '.join(selected_names)}",
        f"Total de aulas regulares no período: {total_regular_lessons}",
        f"Total de aulas extras no período: {total_extra_lessons}",
        "",
        "**Frequência por aluno das turmas regulares:**",
    ]
    for item in regular_summaries:
        percent = f"{item['percent']:.1f}".replace(".", ",")
        denominator = str(item["total"]) if item["total"] else "N/D"
        report_lines.append(f"- {item['display_name']}: {item['present']}/{denominator} ({percent}%)")
    if not regular_summaries:
        report_lines.append("- Nenhum aluno vinculado às turmas regulares selecionadas.")

    report_lines.extend([
        "", "**Frequência por aluno sem vínculo com as turmas regulares atendidas pelas aulas extras selecionadas:**"
    ])
    for item in extra_only_summaries:
        percent = f"{item['percent']:.1f}".replace(".", ",")
        denominator = str(item["total"]) if item["total"] else "N/D"
        report_lines.append(f"- {item['display_name']}: {item['present']}/{denominator} ({percent}%)")
    if not extra_only_summaries:
        report_lines.append("- Nenhum aluno exclusivo das turmas extras selecionadas.")

    report_text = "\n".join(report_lines)
    all_summaries = regular_summaries + extra_only_summaries
    return render_template(
        "combined_report.html", classes=selected_classes,
        regular_summaries=regular_summaries, extra_only_summaries=extra_only_summaries,
        start=start, end=end, period=period, period_label=period_label,
        total_regular_lessons=total_regular_lessons,
        total_extra_lessons=total_extra_lessons, report_text=report_text,
        student_count=len(all_summaries),
    )


@app.get("/classes/<int:class_id>/report")
@teacher_required
def class_report(class_id):
    if not class_access(class_id):
        abort(403)

    today = today_local()
    period = request.args.get("period", "monthly").lower()
    if period not in {"monthly", "annual", "custom"}:
        period = "monthly"

    custom_start = None
    custom_end = None
    if period == "custom":
        try:
            custom_start = date.fromisoformat(request.args.get("start", ""))
            custom_end = date.fromisoformat(request.args.get("end", ""))
        except (ValueError, TypeError):
            flash("Informe uma data inicial e uma data final válidas.", "warning")
            period = "monthly"

    if period == "annual":
        start_date = date(today.year, 1, 1)
        end_date = today
        period_label = "ANUAL"
    elif period == "custom":
        start_date = custom_start
        end_date = custom_end
        if start_date > end_date:
            start_date, end_date = end_date, start_date
            flash("As datas foram invertidas para manter o início antes do fim.", "warning")
        period_label = f"DE {start_date.strftime('%d/%m/%Y')} A {end_date.strftime('%d/%m/%Y')}"
    else:
        start_date = date(today.year, today.month, 1)
        end_date = today
        period_label = "MENSAL"

    start = start_date.isoformat()
    end = end_date.isoformat()
    with db_connect() as db:
        c = get_class_or_404(db, class_id)
        weekdays = get_class_weekdays(db, class_id)
        # Do not count dates before the class itself existed. The calendar gives
        # the expected number of lessons, but older/incomplete class records may
        # have no saved weekdays. Never report fewer lessons than actual saved
        # roll-call sessions for the selected class and period.
        class_created = date.fromisoformat(c["created_at"][:10])
        count_start = max(start_date, class_created)
        calendar_total = count_scheduled_days(count_start, end_date, weekdays)
        saved_sessions = db.execute(
            """SELECT COUNT(*) FROM attendance_sessions
               WHERE class_id=? AND class_date>=? AND class_date<=?""",
            (class_id, start, end),
        ).fetchone()[0]
        scheduled_total = max(calendar_total, saved_sessions)

        sql = """SELECT ar.*, s.name AS student_name, s.nickname AS student_nickname,
                        sc.name AS source_name, t.name AS teacher_name
                 FROM attendance_records ar JOIN students s ON s.id=ar.student_id
                 JOIN classes sc ON sc.id=ar.source_class_id
                 JOIN teachers t ON t.id=ar.marked_by
                 WHERE ar.target_class_id=? AND ar.target_date>=? AND ar.target_date<=?
                 ORDER BY ar.target_date DESC, COALESCE(NULLIF(s.nickname,''),s.name)"""
        records = db.execute(sql, (class_id, start, end)).fetchall()

        # A turma extra participa do relatório da turma regular, mas continua
        # identificada separadamente como atividade extra no total do período.
        if c["is_extra"]:
            extra_sessions = 0
        else:
            extra_sessions = db.execute(
                """SELECT COUNT(*) FROM attendance_sessions a
                   JOIN extra_class_links l ON l.extra_class_id=a.class_id
                   JOIN classes extra ON extra.id=a.class_id AND extra.active=1
                   WHERE l.regular_class_id=? AND a.class_date>=? AND a.class_date<=?""",
                (class_id, start, end),
            ).fetchone()[0]

        # Include currently enrolled students and anyone with attendance in the
        # selected period, so former students remain visible in historical reports.
        students = db.execute(
            """SELECT DISTINCT s.id,s.name,s.nickname,s.belt
               FROM students s
               WHERE s.id IN (SELECT student_id FROM class_students WHERE class_id=?)
                  OR EXISTS (SELECT 1 FROM attendance_records ar WHERE ar.student_id=s.id
                             AND ar.target_class_id=? AND ar.target_date BETWEEN ? AND ?)
                  OR (?=1 AND EXISTS (
                       SELECT 1 FROM class_students cs JOIN extra_class_links l
                         ON l.regular_class_id=cs.class_id
                       WHERE l.extra_class_id=? AND cs.student_id=s.id))
               ORDER BY COALESCE(NULLIF(s.nickname,''),s.name),s.name""",
            (class_id, class_id, start, end, int(bool(c["is_extra"])), class_id),
        ).fetchall()

    counts: dict[int, dict[str, int]] = {
        student["id"]: {"regular": 0, "extra_credit": 0, "extra_actual": 0}
        for student in students
    }
    for record in records:
        bucket = counts.setdefault(record["student_id"],
                                   {"regular": 0, "extra_credit": 0, "extra_actual": 0})
        if record["kind"] in bucket:
            bucket[record["kind"]] += 1

    student_summaries = []
    report_lines = [
        f"**RELATÓRIO DE FREQUÊNCIA {period_label}**",
        "",
        f"Turma: {c['name']}",
    ]
    total_label = f"Total de aulas no período: {scheduled_total}"
    if extra_sessions:
        extra_word = "extra" if extra_sessions == 1 else "extras"
        total_label += f" (+{extra_sessions} {extra_word})"
    report_lines.extend([total_label, "", "**Frequência por aluno:**"])

    for student in students:
        item_counts = counts.get(student["id"],
                                 {"regular": 0, "extra_credit": 0, "extra_actual": 0})
        if c["is_extra"]:
            present = item_counts["extra_actual"]
            breakdown = f"{present} presença(s) na aula extra"
        else:
            present = item_counts["regular"] + item_counts["extra_credit"]
            breakdown = (f"{item_counts['regular']} regular(es), "
                         f"{item_counts['extra_credit']} compensada(s) por aula extra")
        percent = (present / scheduled_total * 100) if scheduled_total else 0
        display_name = student["nickname"] or student["name"]
        if student["nickname"] and student["nickname"].strip().lower() != student["name"].strip().lower():
            display_name = f"{student['nickname']} ({student['name']})"
        student_summaries.append({
            "student_id": student["id"],
            "name": student["name"],
            "nickname": student["nickname"],
            "display_name": display_name,
            "present": present,
            "regular": item_counts["regular"],
            "extra_credit": item_counts["extra_credit"],
            "extra_actual": item_counts["extra_actual"],
            "total": scheduled_total,
            "percent": percent,
            "breakdown": breakdown,
        })
        percentage_br = f"{percent:.1f}".replace(".", ",")
        denominator = str(scheduled_total) if scheduled_total else "N/D"
        # The copy/share report intentionally stays compact: use the nickname
        # when available and show only the attendance fraction and percentage.
        export_name = (student["nickname"] or student["name"]).strip()
        report_lines.append(
            f"- {export_name}: {present}/{denominator} ({percentage_br}%)"
        )

    if not students:
        report_lines.append("- Nenhum aluno matriculado no período.")

    report_text = "\n".join(report_lines)
    return render_template(
        "report.html", class_row=c, records=records,
        student_summaries=student_summaries, start=start, end=end,
        period=period, period_label=period_label,
        scheduled_total=scheduled_total, saved_sessions=saved_sessions,
        extra_sessions=extra_sessions, report_text=report_text,
    )


def export_bundle() -> io.BytesIO:
    with db_connect() as db:
        def rows(query, params=()):
            return [dict(r) for r in db.execute(query, params).fetchall()]
        data = {
            "exported_at": now_iso(),
            "timezone": "America/Sao_Paulo",
            "teachers": rows("SELECT id,public_id,name,nickname,belt,is_admin,active,created_at FROM teachers ORDER BY id"),
            "students": rows("SELECT id,public_id,name,nickname,belt,active,created_at FROM students ORDER BY id"),
            "classes": rows("SELECT * FROM classes ORDER BY id"),
            "class_schedules": rows("SELECT * FROM class_schedules ORDER BY class_id,weekday"),
            "class_teachers": rows("SELECT * FROM class_teachers ORDER BY class_id,teacher_id"),
            "class_students": rows("SELECT * FROM class_students ORDER BY class_id,student_id"),
            "extra_class_links": rows("SELECT * FROM extra_class_links ORDER BY extra_class_id,regular_class_id"),
            "attendance_sessions": rows("SELECT * FROM attendance_sessions ORDER BY class_date,class_id"),
            "attendance_session_students": rows("SELECT * FROM attendance_session_students ORDER BY session_id,student_id"),
            "attendance_records": rows(
                """SELECT ar.*, s.name AS student_name, tc.name AS target_class_name,
                          sc.name AS source_class_name, t.name AS teacher_name
                   FROM attendance_records ar JOIN students s ON s.id=ar.student_id
                   JOIN classes tc ON tc.id=ar.target_class_id
                   JOIN classes sc ON sc.id=ar.source_class_id
                   JOIN teachers t ON t.id=ar.marked_by
                   ORDER BY ar.target_class_id,ar.target_date,student_name"""
            ),
            "attendance_kind_legend": {
                "regular": "Presença registrada em aula da própria turma regular",
                "extra_actual": "Presença física registrada na turma extra",
                "extra_credit": "Presença compensada pela turma extra para uma aula regular da mesma semana",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = Path(temp_dir) / "capoeira-backup.sqlite3"
            backup = sqlite3.connect(backup_path)
            db.backup(backup)
            backup.close()
            backup_bytes = backup_path.read_bytes()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dados.json", json.dumps(data, ensure_ascii=False, indent=2))
        archive.writestr("README.txt", (
            "Exportação do Capoeira Chamada\n\n"
            "dados.json contém todos os cadastros, vínculos, sessões e presenças efetivas.\n"
            "presencas_por_turma.csv diferencia regular, extra_actual e extra_credit.\n"
            "banco.sqlite3 é uma cópia integral do banco no momento da exportação.\n\n"
            "Tipos de presença:\n"
            "regular: presença na aula regular da própria turma.\n"
            "extra_actual: presença na aula extra.\n"
            "extra_credit: presença compensada pela aula extra em uma turma regular.\n"
        ))
        archive.writestr("banco.sqlite3", backup_bytes)
        csv_specs = {
            "turmas.csv": ("classes", ["id","name","is_extra","created_by","active","created_at"]),
            "professores.csv": ("teachers", ["id","public_id","name","nickname","belt","is_admin","active","created_at"]),
            "alunos.csv": ("students", ["id","public_id","name","nickname","belt","active","created_at"]),
            "horarios_turmas.csv": ("class_schedules", ["class_id","weekday"]),
            "professores_por_turma.csv": ("class_teachers", ["class_id","teacher_id"]),
            "alunos_por_turma.csv": ("class_students", ["class_id","student_id","enrolled_at"]),
            "vinculos_turmas_extras.csv": ("extra_class_links", ["regular_class_id","extra_class_id"]),
            "sessoes_chamada.csv": ("attendance_sessions", ["id","class_id","class_date","created_by","created_at","updated_at"]),
            "presencas_por_turma.csv": ("attendance_records", ["student_id","student_name","target_class_id","target_class_name","target_date","source_class_id","source_class_name","source_date","kind","source_session_id","marked_by","teacher_name","created_at"]),
        }
        for filename, (key, headers) in csv_specs.items():
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in data[key]:
                writer.writerow(row)
            archive.writestr(filename, "\ufeff" + stream.getvalue())
    output.seek(0)
    return output


@app.get("/export")
@admin_required
def export_data():
    output = export_bundle()
    filename = f"capoeira-chamada-exportacao-{today_local().isoformat()}.zip"
    return send_file(output, mimetype="application/zip", as_attachment=True, download_name=filename)


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="Você não tem autorização para acessar este recurso."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="O recurso solicitado não foi encontrado."), 404


@app.errorhandler(400)
def bad_request(error):
    message = getattr(error, "description", "Solicitação inválida.")
    return render_template("error.html", code=400, message=message), 400


@app.template_filter("date_br")
def date_br(value):
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return value


if __name__ == "__main__":
    host = os.environ.get("CAPOEIRA_HOST", "0.0.0.0")
    port = int(os.environ.get("CAPOEIRA_PORT", "5000"))
    debug = os.environ.get("CAPOEIRA_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
