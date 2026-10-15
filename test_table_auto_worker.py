import os
import unittest
from unittest.mock import patch

from table_auto_worker import configured_tables


class TableAutoWorkerTests(unittest.TestCase):
    def test_three_physical_tables_are_returned(self):
        values = {
            'LEO_TABLE_ID': 'leo', 'FEISHU_TABLE_ID': 'fallback',
            'LIBBY_TABLE_ID': 'libby', 'CHEN_TABLE_ID': 'chen',
        }
        with patch.dict(os.environ, values, clear=False):
            self.assertEqual(configured_tables(), [
                ('LEO', 'leo'), ('Libby', 'libby'), ('Chen', 'chen')])

    def test_duplicate_table_ids_are_scanned_once(self):
        values = {
            'LEO_TABLE_ID': 'same', 'LIBBY_TABLE_ID': 'same',
            'CHEN_TABLE_ID': 'same',
        }
        with patch.dict(os.environ, values, clear=False):
            self.assertEqual(configured_tables(), [('LEO', 'same')])


if __name__ == '__main__':
    unittest.main()
