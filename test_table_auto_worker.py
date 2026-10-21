import os
import unittest
from unittest.mock import patch

from table_auto_worker import configured_tables, scan_once


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

    @patch('table_auto_worker._save')
    @patch('table_auto_worker._load', return_value={'existing': 'state'})
    @patch('table_auto_worker.Feishu')
    @patch('table_auto_worker.configured_tables', return_value=[
        ('LEO', 'leo'), ('Libby', 'libby')])
    def test_inaccessible_table_does_not_stop_other_tables(self, tables, feishu, load, save):
        bad = unittest.mock.Mock()
        bad.pages.side_effect = RuntimeError('permission denied')
        good = unittest.mock.Mock()
        good.pages.return_value = []
        feishu.side_effect = [bad, good]
        self.assertEqual(scan_once(), [])
        self.assertEqual(feishu.call_count, 2)


if __name__ == '__main__':
    unittest.main()
