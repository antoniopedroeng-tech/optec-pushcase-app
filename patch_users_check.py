from sqlalchemy import create_engine, text
import os, sys

# Read DATABASE_URL from environment (Render External Connection String)
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL is not set. Set it to your Render External Connection String (with sslmode=require).")
    sys.exit(1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SQL = '''
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users
ADD CONSTRAINT users_role_check
CHECK (role IN ('admin','comprador','pagador','cliente'));
'''

def main():
    print("Connecting to database...")
    with engine.begin() as conn:
        for stmt in SQL.strip().split(';'):
            s = stmt.strip()
            if not s:
                continue
            print(f"Executing: {s[:80]}{'...' if len(s)>80 else ''}")
            conn.execute(text(s))
    print("✅ Done: users_role_check updated to include 'cliente'.")

if __name__ == '__main__':
    main()
