import os
import tempfile

import pytest

# Override DB path before any imports touch the config
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")

from backend.db.engine import get_db, reset_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Give every test a fresh database."""
    reset_db()
    db = get_db()
    yield db
    reset_db()
    # clean up the db file
    try:
        os.remove(os.environ["DATABASE_PATH"])
    except FileNotFoundError:
        pass
