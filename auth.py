from flask import Blueprint, render_template, redirect, url_for, flash, request, session
import secrets
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from flask_login import login_user, logout_user, current_user

from .extensions import login_manager
from bson import ObjectId
from .models import User
from .forms import RegisterForm, LoginForm

auth_bp = Blueprint("auth", __name__, template_folder="templates")

LOGIN_MAX_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW_MINUTES = 15
LOGIN_LOCKOUT_MINUTES = 15


def _safe_next_url(next_url: str) -> str | None:
    raw = (next_url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return None
    if not raw.startswith("/"):
        return None
    if raw.startswith("//"):
        return None
    return raw


def _clear_login_lock_state(user):
    user.failed_login_attempts = 0
    user.failed_login_window_start = None
    user.last_failed_login_at = None
    user.login_locked_until = None


def _register_failed_login(user, now):
    window_start = getattr(user, "failed_login_window_start", None)
    attempts = int(getattr(user, "failed_login_attempts", 0) or 0)
    window = timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES)

    if not window_start or now - window_start > window:
        user.failed_login_window_start = now
        user.failed_login_attempts = 1
    else:
        user.failed_login_attempts = attempts + 1

    user.last_failed_login_at = now
    if int(user.failed_login_attempts or 0) >= LOGIN_MAX_ATTEMPTS:
        user.login_locked_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)

@login_manager.user_loader
def load_user(user_id):
    if not ObjectId.is_valid(str(user_id)):
        return None
    return User.objects(id=ObjectId(str(user_id))).first()

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    form = RegisterForm()
    if form.validate_on_submit():
        # Check if username or phone already exists
        if User.objects(username=form.username.data).first():
            flash("اسم المستخدم موجود مسبقاً", "error")
        elif User.objects(phone=form.phone.data).first():
            flash("رقم الهاتف موجود مسبقاً", "error")
        else:
            try:
                user = User(
                    first_name=form.first_name.data,
                    last_name=form.last_name.data,
                    username=form.username.data,
                    phone=form.phone.data,
                    role="student",
                )
                user.set_password(form.password.data)
                user.save()
                
                # Auto-login after registration
                new_token = secrets.token_hex(16)
                user.current_session_token = new_token
                user.save()
                session.clear()
                login_user(user)
                session['session_token'] = new_token
                session.modified = True
                
                display_name = user.first_name or user.username
                flash(f"تم التسجيل بنجاح! مرحباً بك يا {display_name}", "success")
                return redirect(url_for("index"))
            except Exception as e:
                flash(f"خطأ في التسجيل: {str(e)}", "error")
    return render_template("auth/register.html", form=form)

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    form = LoginForm()
    if form.validate_on_submit():
        # Try to find user by username or phone
        user = User.objects(username=form.username_or_phone.data).first() or User.objects(phone=form.username_or_phone.data).first()
        now = datetime.utcnow()
        if user:
            locked_until = getattr(user, "login_locked_until", None)
            if locked_until and locked_until > now:
                remaining_seconds = int((locked_until - now).total_seconds())
                remaining_minutes = max(1, (remaining_seconds + 59) // 60)
                flash(f"تم قفل الحساب مؤقتاً بسبب محاولات دخول متعددة. حاول بعد {remaining_minutes} دقيقة.", "error")
                return render_template("auth/login.html", form=form)

        if user and user.check_password(form.password.data):
            # Generate a new session token and store it to enforce single-device login
            _clear_login_lock_state(user)
            new_token = secrets.token_hex(16)
            user.current_session_token = new_token
            user.save()
            # Clear any stale session data before logging in to ensure the new token is the only one
            session.clear()
            login_user(user)
            session['session_token'] = new_token
            session.modified = True
            display_name = user.first_name or user.username
            flash(f"تم تسجيل الدخول بنجاح! أهلاً {display_name}", "success")
            next_url = _safe_next_url(request.args.get("next"))
            return redirect(next_url or url_for("index"))

        if user:
            _register_failed_login(user, now)
            user.save()
        flash("بيانات دخول غير صحيحة", "error")
    return render_template("auth/login.html", form=form)

@auth_bp.route("/logout")
def logout():
    user = current_user if current_user.is_authenticated else None
    if user:
        user.current_session_token = None
        user.save()
    logout_user()
    session.pop('session_token', None)
    flash("تم تسجيل الخروج", "info")
    return redirect(url_for("index"))
