"""
Database migration manager using yoyo-migrations.

Auto-installs yoyo-migrations if not present and applies pending migrations.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from pathlib import Path

from yoyo import get_backend, read_migrations

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"
# Use absolute path to ensure migrations work regardless of cwd
DB_PATH = str(Path(__file__).parent.parent / "task_ninja.db")


def ensure_yoyo_installed() -> bool:
    """Check if yoyo-migrations is installed, install if not."""
    try:
        import yoyo  # noqa: F401

        return True
    except ImportError:
        logger.info("yoyo-migrations not found, installing...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "yoyo-migrations>=9.0.0"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("yoyo-migrations installed successfully")
            return True
        except subprocess.CalledProcessError as e:
            logger.error("Failed to install yoyo-migrations: %s", e)
            return False


def run_migrations(db_path: str = DB_PATH) -> tuple[int, int]:
    """
    Run all pending migrations.

    Returns:
        Tuple of (applied_count, total_pending)
    """
    # Ensure migrations directory exists
    if not MIGRATIONS_DIR.exists():
        logger.error("Migrations directory not found: %s", MIGRATIONS_DIR)
        return 0, 0

    # Connect to database
    backend = get_backend(f"sqlite:///{db_path}")
    migrations = read_migrations(str(MIGRATIONS_DIR))

    with backend.lock():
        # Get pending migrations
        to_apply = backend.to_apply(migrations)
        pending_count = len(to_apply)

        if pending_count == 0:
            logger.debug("Database is up to date")
            return 0, 0

        logger.info("Found %d pending migration(s)", pending_count)

        # Apply migrations
        backend.apply_migrations(to_apply)

        logger.info("Applied %d migration(s)", pending_count)
        return pending_count, pending_count


def get_migration_status(db_path: str = DB_PATH) -> dict:
    """
    Get current migration status.

    Returns:
        Dict with applied and pending migration info
    """
    backend = get_backend(f"sqlite:///{db_path}")
    migrations = read_migrations(str(MIGRATIONS_DIR))

    with backend.lock():
        to_apply = backend.to_apply(migrations)
        applied = [m for m in migrations if m not in to_apply]

        return {
            "applied": [m.id for m in applied],
            "pending": [m.id for m in to_apply],
            "total": len(migrations),
        }


if __name__ == "__main__":
    # CLI for manual migration runs
    import argparse

    parser = argparse.ArgumentParser(description="Task Ninja database migrator")
    parser.add_argument("--db", default=DB_PATH, help="Database path")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    args = parser.parse_args()

    ensure_yoyo_installed()

    if args.status:
        status = get_migration_status(args.db)
        print(f"Applied: {len(status['applied'])} migrations")
        for m in status["applied"]:
            print(f"  ✓ {m}")
        print(f"Pending: {len(status['pending'])} migrations")
        for m in status["pending"]:
            print(f"  ○ {m}")
    else:
        applied, _ = run_migrations(args.db)
        print(f"Done. Applied {applied} migration(s).")
