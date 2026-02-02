from flask import Flask, render_template, session, request, redirect, url_for, flash, send_from_directory
from pathlib import Path
from werkzeug.exceptions import NotFound

from .config import Config
from .extensions import login_manager, init_mongo
from flask_login import current_user, logout_user

from .auth import auth_bp
from .admin import admin_bp
from .teacher import teacher_bp
from .student import student_bp


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    # Enable debug logging for session enforcement diagnostics
    try:
        app.logger.setLevel('DEBUG')
    except Exception:
        pass

    init_mongo(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

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
                return render_template("pages/404.html"), 404
    
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

    return app

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)

