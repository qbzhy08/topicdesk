import tempfile
import unittest
from pathlib import Path

from app import config, db


class DBTestCase(unittest.TestCase):
    """每个测试用独立的临时数据库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / "test.db"
        db.reset()
        db.init_db()

    def tearDown(self):
        db.reset()
        self._tmp.cleanup()
