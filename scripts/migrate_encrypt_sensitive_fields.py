"""
scripts/migrate_encrypt_sensitive_fields.py
One-time migration: encrypts existing plain-text values in sensitive columns.

Run ONCE after setting HR_ENCRYPTION_KEY in .env:
    python scripts/migrate_encrypt_sensitive_fields.py

Safe to re-run: already-encrypted values (starting with 'enc:') are skipped.
"""

import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv

load_dotenv()

from app.db.connection import get_cursor
from app.utils.encryption import encrypt_field, is_encrypted, _get_fernet

SENSITIVE_COLUMNS = ["current_ctc", "expected_ctc", "offer_in_hand", "ta_hr_comments"]


def run_migration(dry_run: bool = False) -> None:
    fernet = _get_fernet()
    if fernet is None:
        print(
            "ERROR: HR_ENCRYPTION_KEY is not set or invalid in .env.\n"
            "Generate a key with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            "Then add HR_ENCRYPTION_KEY=<key> to your .env file."
        )
        sys.exit(1)

    print(
        f"{'[DRY RUN] ' if dry_run else ''}Starting sensitive field encryption migration..."
    )

    with get_cursor(commit=not dry_run) as cur:
        # Fetch all candidates that have at least one non-null sensitive field
        placeholders = " OR ".join(f"{col} IS NOT NULL" for col in SENSITIVE_COLUMNS)
        cur.execute(
            f"SELECT id, {', '.join(SENSITIVE_COLUMNS)} FROM candidates WHERE {placeholders}"
        )
        rows = cur.fetchall()

        total = len(rows)
        encrypted_count = 0
        skipped_count = 0

        for row in rows:
            row = dict(row)
            cid = row["id"]
            updates = {}

            for col in SENSITIVE_COLUMNS:
                val = row.get(col)
                if not val:
                    continue
                if is_encrypted(val):
                    skipped_count += 1
                    continue
                encrypted_val = encrypt_field(val)
                updates[col] = encrypted_val

            if updates:
                set_clause = ", ".join(f"{col} = %s" for col in updates)
                values = list(updates.values()) + [cid]
                if not dry_run:
                    cur.execute(
                        f"UPDATE candidates SET {set_clause} WHERE id = %s",
                        values,
                    )
                encrypted_count += len(updates)
                if dry_run:
                    print(
                        f"  [DRY RUN] Would encrypt {list(updates.keys())} for candidate id={cid}"
                    )

    print(
        f"\nMigration {'(DRY RUN) ' if dry_run else ''}complete:\n"
        f"  Total candidates checked : {total}\n"
        f"  Fields encrypted         : {encrypted_count}\n"
        f"  Already encrypted (skip) : {skipped_count}\n"
    )
    if dry_run:
        print("Run without --dry-run to apply changes.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_migration(dry_run=dry)
