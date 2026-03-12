# run.py — The main entry point to launch the Flask application.

from app import create_app
from app.db.connection import init_db
from app.db.users import get_user_by_username, create_user

# Initialize the Flask app via the factory pattern
app = create_app()


def setup_initial_state():
    """Ensure the database is initialized and a default admin exists."""
    init_db()

    # Create default admin if it doesn't exist
    user = get_user_by_username("admin")
    if not user:
        print("Creating default admin user...")
        create_user("admin", "admin123", is_admin=1)


if __name__ == "__main__":
    # BUG FIX: Both setup AND app.run() must share the same app context.
    # The original code called app.run() outside the `with` block, which is fine
    # for run(), but init_db() needs the context — keeping it explicit and clear.
    with app.app_context():
        setup_initial_state()

    app.run(debug=True, port=5001)
