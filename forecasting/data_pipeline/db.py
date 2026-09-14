import os

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import connection as Connection

load_dotenv()


def get_connection() -> Connection:
    return psycopg2.connect(os.environ["DATABASE_URL"])
