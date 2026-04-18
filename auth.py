from flask import Blueprint, render_template, redirect, url_for, flash, request, session
import secrets
from flask_login import login_user, logout_user, current_user

from .extensions import login_manager
from bson import ObjectId
from .models import User
from .forms import RegisterForm, LoginForm

auth_bp = Blueprint("auth", __name__, template_folder="templates")

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
        if user and user.check_password(form.password.data):
            # Generate a new session token and store it to enforce single-device login
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
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))
        flash("بيانات دخول غير صحيحة", "error")
    return render_template("auth/login.html", form=form)

@auth_bp.route("/logout")
def logout():
    logout_user()
    session.pop('session_token', None)
    # Optionally clear server-side token to avoid stale values
    if current_user.is_authenticated:
        current_user.current_session_token = None
        current_user.save()
    flash("تم تسجيل الخروج", "info")
    return redirect(url_for("index"))
