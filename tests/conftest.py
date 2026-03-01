import os
import tempfile

import pytest

# Override DB path before any imports touch the config
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")

from backend.db.engine import get_db, _connection  # noqa: E402
import backend.db.engine as engine_mod  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Give every test a fresh database."""
    engine_mod._connection = None  # force re-init
    db = get_db()
    yield db
    db.close()
    engine_mod._connection = None
    # clean up the db file
    try:
        os.remove(os.environ["DATABASE_PATH"])
    except FileNotFoundError:
        pass
