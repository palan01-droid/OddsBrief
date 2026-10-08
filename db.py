import os

from psycopg_pool import ConnectionPool

# load settings from .env (on the server these are already environment variables)
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                name, value = line.split("=", 1)
                if name not in os.environ:
                    os.environ[name] = value

# a few connections that get reused, opening a new one to supabase every time is slow
# (prepare_threshold=None because supabase's pooler doesn't like prepared statements)
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=5,
                      kwargs={"prepare_threshold": None}, open=True)


def query(sql, params=()):
    # for SELECTs (and INSERT ... RETURNING), gives back a list of rows
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def run(sql, params=()):
    # for INSERT/UPDATE/DELETE, gives back how many rows changed
    with pool.connection() as conn:
        return conn.execute(sql, params).rowcount


def run_many(sql, rows):
    # same thing for a whole list of rows at once, much faster than one at a time over the network
    if len(rows) == 0:
        return
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


def setup():
    run("""
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS markets (
        ticker TEXT PRIMARY KEY, series TEXT, event_title TEXT, subtitle TEXT,
        category TEXT, tag TEXT, close_time TEXT, result TEXT, forecast DOUBLE PRECISION
    );
    CREATE TABLE IF NOT EXISTS trades (
        trade_id TEXT PRIMARY KEY, ticker TEXT, title TEXT, time TEXT, side TEXT,
        price DOUBLE PRECISION, dollars DOUBLE PRECISION
    );
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY, username TEXT NOT NULL, password_hash TEXT NOT NULL, created TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users (lower(username));
    CREATE TABLE IF NOT EXISTS likes (
        user_id INTEGER, ticker TEXT, PRIMARY KEY (user_id, ticker)
    );
    CREATE TABLE IF NOT EXISTS comments (
        id SERIAL PRIMARY KEY, user_id INTEGER, ticker TEXT, text TEXT, time TEXT
    );
    CREATE TABLE IF NOT EXISTS docs (
        id TEXT PRIMARY KEY, type TEXT, title TEXT, text TEXT, embedding vector(768)
    );
    CREATE INDEX IF NOT EXISTS docs_embedding ON docs USING hnsw (embedding vector_cosine_ops);
    CREATE TABLE IF NOT EXISTS app_state (
        name TEXT PRIMARY KEY, value TEXT
    );
    """)


setup()
