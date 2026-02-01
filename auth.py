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
        if User.objects(username=form.username.data).first() or User.objects(email=form.email.data).first():
            flash("اسم المستخدم أو البريد الإلكتروني موجود مسبقاً", "error")
        else:
            user = User(
                username=form.username.data,
                email=form.email.data,
                role=form.role.data,
            )
            user.set_password(form.password.data)
            user.save()
            flash("تم التسجيل بنجاح. يرجى تسجيل الدخول.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.objects(username=form.username.data).first()
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
            flash("تم تسجيل الدخول بنجاح", "success")
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
