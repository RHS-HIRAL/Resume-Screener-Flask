# app/__init__.py — Application Factory: creates and configures the Flask app.

from flask import Flask
from flask_login import LoginManager
from flask_cors import CORS

from config import Config
from app.models.user import User
from app.db.users import get_user_by_id


def create_app() -> Flask:
    """
    Flask application factory.
    Creates the app, registers extensions, and wires up all blueprints.
    Called by run.py and any test harness.
    """
    app = Flask(__name__)
    app.secret_key = Config.SECRET_KEY

    # ── Extensions ────────────────────────────────────────────────────────────
    CORS(app)

    login_manager = LoginManager()
    login_manager.init_app(app)
    # Must match the blueprint name + function: "auth" blueprint, "login" function
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str):
        """Reload the User object from the database on every request."""
        try:
            user_data = get_user_by_id(int(user_id))
            if user_data:
                return User(
                    user_data["id"],
                    user_data["username"],
                    user_data["is_admin"],
                )
        except Exception:
            pass
        return None

    # ── Blueprints ────────────────────────────────────────────────────────────
    # Import here (inside factory) to avoid circular imports at module load time
    from app.routes.auth import auth_bp
    from app.routes.views import views_bp
    from app.routes.api_analysis import api_analysis_bp
    from app.routes.api_candidates import api_candidates_bp
    from app.routes.api_sharepoint import api_sharepoint_bp
    from app.routes.api_qa import api_qa_bp
    from app.routes.api_upload import api_upload_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_analysis_bp)
    app.register_blueprint(api_candidates_bp)
    app.register_blueprint(api_sharepoint_bp)
    app.register_blueprint(api_qa_bp)
    app.register_blueprint(api_upload_bp)

    return app
