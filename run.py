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
        create_user("admin", "admin123", is_admin=1, role="admin")
        print("Default admin created. IMPORTANT: change the password immediately.")


if __name__ == "__main__":
    with app.app_context():
        setup_initial_state()

    app.run(debug=True, port=5001)
