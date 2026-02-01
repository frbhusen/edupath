from flask import Flask, render_template, session, request, redirect, url_for, flash, send_from_directory
from pathlib import Path
from werkzeug.exceptions import NotFound
from sqlalchemy import text

from .config import Config
from .extensions import db, login_manager
from flask_login import current_user, logout_user

from .auth import auth_bp
from .admin import admin_bp
from .teacher import teacher_bp
from .student import student_bp


def ensure_schema():
    """Add new columns to existing SQLite DBs when users upgrade models."""
    with db.engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(lesson)"))}
        additions = [
            ("link_label", "TEXT"),
            ("link_url", "TEXT"),
            ("link_label_2", "TEXT"),
            ("link_url_2", "TEXT"),
        ]
        for name, col_type in additions:
            if name not in columns:
                conn.execute(text(f"ALTER TABLE lesson ADD COLUMN {name} {col_type}"))

        # Ensure tests can reference lessons
        test_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(test)"))}
        if "lesson_id" not in test_columns:
            conn.execute(text("ALTER TABLE test ADD COLUMN lesson_id INTEGER"))
        if "requires_code" not in test_columns:
            conn.execute(text("ALTER TABLE test ADD COLUMN requires_code BOOLEAN NOT NULL DEFAULT 1"))

        # Ensure Section.requires_code exists
        section_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(section)"))}
        if "requires_code" not in section_columns:
            conn.execute(text("ALTER TABLE section ADD COLUMN requires_code BOOLEAN NOT NULL DEFAULT 0"))

        # Create SectionActivation table if missing
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "section_activation" not in tables:
            conn.execute(text(
                """
                CREATE TABLE section_activation (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  section_id INTEGER NOT NULL,
                  student_id INTEGER NOT NULL,
                  activated_at DATETIME,
                  active BOOLEAN NOT NULL DEFAULT 1,
                  FOREIGN KEY(section_id) REFERENCES section(id) ON DELETE CASCADE,
                  FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Create ActivationCode table if missing
        if "activation_code" not in tables:
            conn.execute(text(
                """
                CREATE TABLE activation_code (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  section_id INTEGER NOT NULL,
                  student_id INTEGER NOT NULL,
                  code TEXT NOT NULL UNIQUE,
                  created_at DATETIME,
                  used_at DATETIME,
                  is_used BOOLEAN NOT NULL DEFAULT 0,
                  FOREIGN KEY(section_id) REFERENCES section(id) ON DELETE CASCADE,
                  FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Drop old TestActivation tables if they exist (no longer used)
        if "test_activation" in tables:
            conn.execute(text("DROP TABLE IF EXISTS test_activation"))
        if "test_activation_code" in tables:
            conn.execute(text("DROP TABLE IF EXISTS test_activation_code"))

        # Create SubjectActivation table if missing
        if "subject_activation" not in tables:
            conn.execute(text(
                """
                CREATE TABLE subject_activation (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    activated_at DATETIME,
                    active BOOLEAN NOT NULL DEFAULT 1,
                    FOREIGN KEY(subject_id) REFERENCES subject(id) ON DELETE CASCADE,
                    FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Create SubjectActivationCode table if missing
        if "subject_activation_code" not in tables:
            conn.execute(text(
                """
                CREATE TABLE subject_activation_code (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject_id INTEGER NOT NULL,
                    student_id INTEGER NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    created_at DATETIME,
                    used_at DATETIME,
                    is_used BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(subject_id) REFERENCES subject(id) ON DELETE CASCADE,
                    FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Ensure Lesson.requires_code exists
        lesson_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(lesson)"))}
        if "requires_code" not in lesson_columns:
            conn.execute(text("ALTER TABLE lesson ADD COLUMN requires_code BOOLEAN NOT NULL DEFAULT 1"))

        # Ensure Question.hint exists
        question_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(question)"))}
        if "hint" not in question_columns:
            conn.execute(text("ALTER TABLE question ADD COLUMN hint TEXT"))

        # Create LessonActivation table if missing
        if "lesson_activation" not in tables:
            conn.execute(text(
                """
                CREATE TABLE lesson_activation (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lesson_id INTEGER NOT NULL,
                  student_id INTEGER NOT NULL,
                  activated_at DATETIME,
                  active BOOLEAN NOT NULL DEFAULT 1,
                  FOREIGN KEY(lesson_id) REFERENCES lesson(id) ON DELETE CASCADE,
                  FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Create LessonActivationCode table if missing
        if "lesson_activation_code" not in tables:
            conn.execute(text(
                """
                CREATE TABLE lesson_activation_code (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  lesson_id INTEGER NOT NULL,
                  student_id INTEGER NOT NULL,
                  code TEXT NOT NULL UNIQUE,
                  created_at DATETIME,
                  used_at DATETIME,
                  is_used BOOLEAN NOT NULL DEFAULT 0,
                  FOREIGN KEY(lesson_id) REFERENCES lesson(id) ON DELETE CASCADE,
                  FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Create lesson_resource table for additional buttons if missing
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "lesson_resource" not in tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE lesson_resource (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lesson_id INTEGER NOT NULL,
                        label TEXT NOT NULL,
                        url TEXT NOT NULL,
                        resource_type TEXT,
                        position INTEGER NOT NULL DEFAULT 0,
                        FOREIGN KEY(lesson_id) REFERENCES lesson(id) ON DELETE CASCADE
                    )
                    """
                )
            )
        else:
            resource_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(lesson_resource)"))}
            if "resource_type" not in resource_columns:
                conn.execute(text("ALTER TABLE lesson_resource ADD COLUMN resource_type TEXT"))

        # Migrate legacy second link into resource rows once
        resource_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(lesson_resource)"))}
        if "label" in resource_cols:
            # only migrate when legacy columns exist; avoid repeated inserts via marker table
            conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)"))
            done = conn.execute(text("SELECT name FROM schema_migrations WHERE name='migrated_lesson_links'"))
            if done.first() is None:
                legacy_rows = conn.execute(text("SELECT id, link_label_2, link_url_2 FROM lesson WHERE link_label_2 IS NOT NULL AND link_url_2 IS NOT NULL"))
                for lesson_id, label2, url2 in legacy_rows:
                    conn.execute(
                        text("INSERT INTO lesson_resource (lesson_id, label, url, position) VALUES (:lesson_id, :label, :url, 1)"),
                        {"lesson_id": lesson_id, "label": label2, "url": url2},
                    )
                conn.execute(text("INSERT INTO schema_migrations (name) VALUES ('migrated_lesson_links')"))

        # Ensure User.current_session_token exists for single-device login enforcement
        user_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(user)"))}
        if "current_session_token" not in user_columns:
            conn.execute(text("ALTER TABLE user ADD COLUMN current_session_token TEXT"))

        # Create CustomTestAttempt table if missing
        tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        if "custom_test_attempt" not in tables:
            conn.execute(text(
                """
                CREATE TABLE custom_test_attempt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id INTEGER NOT NULL,
                    label TEXT NOT NULL DEFAULT 'Custom Test',
                    created_at DATETIME,
                    status TEXT NOT NULL DEFAULT 'active',
                    total INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 0,
                    selections_json TEXT NOT NULL,
                    question_order_json TEXT NOT NULL,
                    answer_order_json TEXT NOT NULL,
                    FOREIGN KEY(student_id) REFERENCES user(id) ON DELETE CASCADE
                )
                """
            ))

        # Create CustomTestAnswer table if missing
        if "custom_test_answer" not in tables:
            conn.execute(text(
                """
                CREATE TABLE custom_test_answer (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    choice_id INTEGER,
                    is_correct BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(attempt_id) REFERENCES custom_test_attempt(id) ON DELETE CASCADE,
                    FOREIGN KEY(question_id) REFERENCES question(id) ON DELETE CASCADE,
                    FOREIGN KEY(choice_id) REFERENCES choice(id) ON DELETE SET NULL
                )
                """
            ))


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    # Enable debug logging for session enforcement diagnostics
    try:
        app.logger.setLevel('DEBUG')
    except Exception:
        pass

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    with app.app_context():
        from .models import User, Subject, Section, Lesson, Test, Question, Choice, Attempt, AttemptAnswer, CustomTestAttempt, CustomTestAnswer
        db.create_all()
        ensure_schema()

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(teacher_bp, url_prefix="/teacher")
    app.register_blueprint(student_bp)

    # Add built-in functions to Jinja globals
    app.jinja_env.globals.update(max=max, min=min, range=range)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/latex-cheatsheet")
    def latex_cheatsheet():
        return render_template("latex_cheatsheet.html")

    @app.route("/pages/<path:filename>")
    @app.route("/study_platform/pages/<path:filename>")
    def pages(filename):
        pages_dir = Path(app.root_path).parent / "pages"
        try:
            return send_from_directory(str(pages_dir), filename)
        except NotFound:
            try:
                return render_template(f"pages/{filename}")
            except Exception:
                return render_template("404.html"), 404
    
    @app.before_request
    def enforce_single_device_login():
        # Skip static assets and auth endpoints to avoid loops
        if request.path.startswith('/static'):
            return None
        if request.endpoint in {'auth.login', 'auth.logout'}:
            return None
        if current_user.is_authenticated:
            token = session.get('session_token')
            # Read fresh value from DB to avoid any stale attribute caching
            from .models import User
            db_user = User.query.get(current_user.id)
            db_token = getattr(db_user, 'current_session_token', None)
            app.logger.debug(f"Single-device check: endpoint={request.endpoint}, path={request.path}, token={token}, db_token={db_token}, user_id={current_user.id}")
            if not token or not db_token or token != db_token:
                # Invalidate this session because another device has logged in
                app.logger.debug("Token mismatch detected; logging out current session.")
                logout_user()
                session.clear()
                # Preserve intended destination
                flash('You were logged out because your account logged in on another device.', 'warning')
                return redirect(url_for('auth.login', next=request.path))

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
