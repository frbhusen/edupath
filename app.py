from flask import Flask, render_template, session, request, redirect, url_for, flash, send_from_directory, g, jsonify
from pathlib import Path
from werkzeug.exceptions import NotFound
import time

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency in some environments
    load_dotenv = None

if load_dotenv:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(dotenv_path=env_path, override=False)

from .config import Config
from .extensions import login_manager, init_mongo, cache
from flask_login import current_user, logout_user, login_required
from bson import ObjectId

from .auth import auth_bp
from .admin import admin_bp
from .teacher import teacher_bp
from .student import student_bp
from .staff_activity import log_staff_activity_from_request


def _migrate_legacy_teacher_role_once(app):
    """One-time migration for old installs where teacher was the full admin role."""
    try:
        from .models import User

        has_modern_admin = bool(User.objects(role__in=["admin", "question_editor"]).first())
        if has_modern_admin:
            return

        legacy_rows = list(User.objects(role="teacher").all())
        if not legacy_rows:
            return

        for row in legacy_rows:
            row.role = "admin"
            row.save()

        app.logger.warning("Migrated %s legacy teacher account(s) to admin role.", len(legacy_rows))
    except Exception as exc:
        app.logger.error("Legacy role migration failed: %s", exc)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    try:
        app.logger.setLevel('DEBUG')
    except Exception:
        pass

    init_mongo(app)
    _migrate_legacy_teacher_role_once(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    
    # Initialize cache
    cache.init_app(app, config={
        'CACHE_TYPE': 'simple',
        'CACHE_DEFAULT_TIMEOUT': 300
    })

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(teacher_bp, url_prefix="/teacher")
    app.register_blueprint(student_bp)

    # Add built-in functions to Jinja globals
    app.jinja_env.globals.update(max=max, min=min, range=range)

    @app.context_processor
    def inject_student_gamification():
        if not current_user.is_authenticated:
            return {"student_gamification_nav": None}
        if (getattr(current_user, "role", "") or "").lower() != "student":
            return {"student_gamification_nav": None}

        try:
            from .models import StudentGamification

            profile = StudentGamification.objects(student_id=current_user.id).first()
            if not profile:
                return {
                    "student_gamification_nav": {
                        "level": 1,
                        "xp_total": 0,
                        "current_streak": 0,
                    }
                }
            return {
                "student_gamification_nav": {
                    "level": profile.level,
                    "xp_total": profile.xp_total,
                    "current_streak": profile.current_streak,
                }
            }
        except Exception:
            return {"student_gamification_nav": None}

    @app.context_processor
    def inject_notifications_counter():
        if not current_user.is_authenticated:
            return {"unread_notifications_count": 0}
        try:
            from .models import NotificationRecipient

            unread = NotificationRecipient.objects(user_id=current_user.id, is_read=False).count()
            return {"unread_notifications_count": int(unread or 0)}
        except Exception:
            return {"unread_notifications_count": 0}

    @app.route("/")
    def index():
        teacher_home_subject = None
        teacher_home_subjects = []
        if current_user.is_authenticated and (getattr(current_user, "role", "") or "").lower() == "teacher":
            try:
                from .models import Subject
                from .permissions import get_staff_subject_ids

                scoped_ids = list(get_staff_subject_ids(current_user.id))
                if scoped_ids:
                    teacher_home_subjects = list(Subject.objects(id__in=scoped_ids).order_by("created_at").all())
                    teacher_home_subject = teacher_home_subjects[0] if teacher_home_subjects else None
            except Exception:
                teacher_home_subject = None
                teacher_home_subjects = []
        return render_template(
            "index.html",
            teacher_home_subject=teacher_home_subject,
            teacher_home_subjects=teacher_home_subjects,
        )

    @app.route("/latex-cheatsheet")
    def latex_cheatsheet():
        return render_template("latex_cheatsheet.html")

    @app.route("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /teacher/",
            "Disallow: /admin/",
            "Disallow: /notifications",
            "Disallow: /notifications/",
            "Disallow: /subjects",
            "Disallow: /results",
            f"Sitemap: {url_for('sitemap_xml', _external=True)}",
        ]
        return app.response_class("\n".join(lines), mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        from datetime import datetime

        pages = [
            url_for("index", _external=True),
            url_for("latex_cheatsheet", _external=True),
            url_for("auth.login", _external=True),
            url_for("auth.register", _external=True),
        ]

        lastmod = datetime.utcnow().strftime("%Y-%m-%d")
        xml_parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for page in pages:
            xml_parts.append("  <url>")
            xml_parts.append(f"    <loc>{page}</loc>")
            xml_parts.append(f"    <lastmod>{lastmod}</lastmod>")
            xml_parts.append("    <changefreq>weekly</changefreq>")
            xml_parts.append("    <priority>0.8</priority>")
            xml_parts.append("  </url>")
        xml_parts.append("</urlset>")

        return app.response_class("\n".join(xml_parts), mimetype="application/xml")

    @app.route("/notifications")
    @login_required
    def notifications_inbox():
        from .models import NotificationRecipient

        items = list(
            NotificationRecipient.objects(user_id=current_user.id)
            .order_by("-created_at")
            .limit(200)
            .all()
        )
        return render_template("notifications/inbox.html", items=items)

    @app.route("/notifications/<recipient_id>/read", methods=["POST"])
    @login_required
    def notifications_mark_read(recipient_id):
        from datetime import datetime
        from .models import NotificationRecipient

        row = NotificationRecipient.objects(id=recipient_id).first() if ObjectId.is_valid(recipient_id) else None
        if row and row.user_id and str(row.user_id.id) == str(current_user.id):
            if not row.is_read:
                row.is_read = True
                row.read_at = datetime.utcnow()
                row.save()
        return redirect(url_for("notifications_inbox"))

    @app.route("/notifications/read-all", methods=["POST"])
    @login_required
    def notifications_mark_all_read():
        from datetime import datetime
        from .models import NotificationRecipient

        NotificationRecipient.objects(user_id=current_user.id, is_read=False).update(
            set__is_read=True,
            set__read_at=datetime.utcnow(),
        )
        return redirect(url_for("notifications_inbox"))

    @app.route("/notifications/popup-feed", methods=["GET"])
    @login_required
    def notifications_popup_feed():
        from .models import NotificationRecipient, Notification

        unread_rows = list(
            NotificationRecipient.objects(user_id=current_user.id, is_read=False)
            .only("id", "notification_id", "created_at")
            .no_dereference()
            .order_by("-created_at")
            .limit(10)
            .all()
        )

        def _ref_id(value):
            if not value:
                return None
            if isinstance(value, ObjectId):
                return value
            if hasattr(value, "id"):
                return value.id
            maybe = getattr(value, "$id", None)
            if maybe:
                return maybe
            return None

        notification_ids = [nid for nid in (_ref_id(getattr(r, "notification_id", None)) for r in unread_rows) if nid]
        notifications = Notification.objects(id__in=notification_ids).only("id", "title", "body", "template_type", "created_at").all() if notification_ids else []
        notifications_by_id = {n.id: n for n in notifications}

        payload = []
        for row in unread_rows:
            notif = notifications_by_id.get(_ref_id(getattr(row, "notification_id", None)))
            if not notif:
                continue
            payload.append(
                {
                    "recipient_id": str(row.id),
                    "title": notif.title,
                    "body": notif.body,
                    "template_type": getattr(notif, "template_type", "note") or "note",
                    "created_at": notif.created_at.isoformat() if notif.created_at else None,
                    "token": f"{row.id}:{getattr(notif, 'created_at', None)}",
                }
            )

        return jsonify({"items": payload})

    @app.route("/notifications/<recipient_id>/read-ajax", methods=["POST"])
    @login_required
    def notifications_mark_read_ajax(recipient_id):
        from datetime import datetime
        from .models import NotificationRecipient

        row = NotificationRecipient.objects(id=recipient_id).first() if ObjectId.is_valid(recipient_id) else None
        if row and row.user_id and str(row.user_id.id) == str(current_user.id):
            if not row.is_read:
                row.is_read = True
                row.read_at = datetime.utcnow()
                row.save()
            return jsonify({"ok": True})

        return jsonify({"ok": False}), 404

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
                return render_template("pages/404.html"), 404
    
    @app.before_request
    def start_timer():
        """Track request start time for performance monitoring"""
        g.start_time = time.time()
    
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
            db_user = User.objects(id=current_user.id).first()
            db_token = getattr(db_user, 'current_session_token', None) if db_user else None
            app.logger.debug(f"Single-device check: endpoint={request.endpoint}, path={request.path}, token={token}, db_token={db_token}, user_id={current_user.id}")
            if not token or not db_token or token != db_token:
                # Invalidate this session because another device has logged in
                app.logger.debug("Token mismatch detected; logging out current session.")
                logout_user()
                session.clear()
                # Preserve intended destination
                flash('You were logged out because your account logged in on another device.', 'warning')
                return redirect(url_for('auth.login', next=request.path))
    
    @app.after_request
    def log_request_time(response):
        """Log request processing time"""
        if hasattr(g, 'start_time'):
            elapsed = time.time() - g.start_time
            if elapsed > 0.5:  # Log slow requests (>500ms)
                app.logger.warning(f"SLOW: {request.method} {request.path} took {elapsed:.3f}s")
            else:
                app.logger.debug(f"{request.method} {request.path} took {elapsed:.3f}s")
        log_staff_activity_from_request(response)
        return response

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)

