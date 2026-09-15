"""postgres connections. pass values as %s params, never f-string into SQL."""

import os
import psycopg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    # one connection per call, no pool. the caller's `with` closes it.
    # psycopg_pool is the upgrade if churn ever shows up under load.
    return psycopg.connect(DATABASE_URL)


if __name__ == "__main__":
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT %s", (1,))
        print(cursor.fetchone())

