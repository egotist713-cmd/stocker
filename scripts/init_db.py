from app.database.db import DEFAULT_DB_PATH, init_database


if __name__ == "__main__":
    init_database()

    print("SQLite OK")
    print(f"Database: {DEFAULT_DB_PATH}")
    print(f"Exists: {DEFAULT_DB_PATH.exists()}")
