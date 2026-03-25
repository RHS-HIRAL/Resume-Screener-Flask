# app/routes/auth.py — Blueprint for user authentication (Login, Register, Logout).

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, login_required, logout_user

from app.models.user import User
from app.db.users import verify_user, create_user

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please enter both username and password.", "warning")
            return redirect(url_for("auth.login"))

        user_data = verify_user(username, password)
        if user_data:
            user = User(
                user_data["id"],
                user_data["username"],
                user_data["is_admin"],
                role=user_data.get("role", "recruiter"),
            )
            login_user(user)

            # Intelligent redirect: Send user back to the page they originally requested
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/"):
                return redirect(next_page)

            return redirect(url_for("views.dashboard"))

        flash("Invalid username or password", "danger")

    return render_template("login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or not password:
            flash("All fields are required.", "warning")
            return redirect(url_for("auth.register"))

        if password != confirm:
            flash("Passwords do not match", "danger")
            return redirect(url_for("auth.register"))

        # Self-registered users get 'recruiter' role by default.
        # An admin must elevate their role via the user management panel.
        success, msg = create_user(username, password, is_admin=0, role="recruiter")
        if success:
            flash("Registration successful! Please login.", "success")
            return redirect(url_for("auth.login"))

        flash(msg, "danger")

    return render_template("register.html")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been successfully logged out.", "info")
    return redirect(url_for("auth.login"))
