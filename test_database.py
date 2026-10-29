import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import financial_pipeline as pipeline


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'financial_data.db'
        self.rows = [('2026-10-01', "Client's payment", 12345, 0),
                     ('2026-10-02', 'Food', 0, 50)]

    def query(self, sql):
        with contextlib.closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(sql).fetchall()

    def test_persists_exact_cents_and_source(self):
        self.assertTrue(pipeline.save_transactions(self.db, self.rows, 'sample.csv'))
        self.assertEqual(self.query('SELECT date, description, income_cents, expenses_cents FROM transactions ORDER BY row_number'), self.rows)
        self.assertEqual(self.query('SELECT source_name, row_count FROM imports'), [('sample.csv', 2)])

    def test_rerun_and_renamed_copy_do_not_duplicate(self):
        pipeline.save_transactions(self.db, self.rows, 'sample.csv')
        self.assertFalse(pipeline.save_transactions(self.db, self.rows, 'sample.csv'))
        self.assertFalse(pipeline.save_transactions(self.db, self.rows, 'renamed.csv'))
        self.assertEqual(self.query('SELECT COUNT(*) FROM transactions'), [(2,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM imports'), [(1,)])

    def test_identical_rows_within_batch_are_preserved(self):
        rows = [self.rows[0], self.rows[0]]
        pipeline.save_transactions(self.db, rows, 'sample.csv')
        self.assertEqual(self.query('SELECT COUNT(*) FROM transactions'), [(2,)])
        self.assertFalse(pipeline.save_transactions(self.db, rows, 'sample.csv'))

    def test_failed_insert_rolls_back_entire_batch_and_preserves_previous_data(self):
        pipeline.save_transactions(self.db, self.rows, 'sample.csv')
        broken_rows = [('2026-10-03', 'Valid first row', 100, 0),
                       ('2026-10-04', 'Invalid second row', -100, 0)]
        with self.assertRaises(sqlite3.IntegrityError):
            pipeline.save_transactions(self.db, broken_rows, 'broken.csv')
        self.assertEqual(self.query('SELECT COUNT(*) FROM imports'), [(1,)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM transactions'), [(2,)])

    def test_separate_batch_is_added(self):
        pipeline.save_transactions(self.db, self.rows, 'sample.csv')
        self.assertTrue(pipeline.save_transactions(self.db, [self.rows[1]], 'new.csv'))
        self.assertEqual(self.query('SELECT COUNT(*) FROM transactions'), [(3,)])

    def test_empty_batch_does_not_create_database(self):
        self.assertFalse(pipeline.save_transactions(self.db, [], 'empty.csv'))
        self.assertFalse(self.db.exists())

    def test_invalid_csv_never_reaches_database(self):
        (self.root / 'financial_data.csv').write_text(
            'date,description,income,expenses\n2026-10-01,Valid,1.00,0.00\n2026-10-02,Invalid,abc,0.00\n')
        with patch.object(pipeline, 'PROJECT_DIR', self.root), \
             patch.object(pipeline, 'configure_logging'), \
             self.assertLogs(pipeline.LOGGER, level='ERROR'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pipeline.main(), 1)
        self.assertFalse(self.db.exists())
        self.assertNotIn('Financial summary', output.getvalue())

    def test_database_failure_returns_failure_without_summary(self):
        with patch.object(pipeline, 'configure_logging'), \
             patch.object(pipeline, 'read_transactions', return_value=self.rows), \
             patch.object(pipeline, 'save_transactions', side_effect=sqlite3.OperationalError('database is locked')), \
             self.assertLogs(pipeline.LOGGER, level='ERROR') as logs, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pipeline.main(), 1)
        self.assertIn('Database import failed', logs.output[0])
        self.assertNotIn('Financial summary', output.getvalue())

    def test_cents_storage_boundary(self):
        amount = pipeline.parse_amount('92233720368547758.07', 'income', 2)
        pipeline.save_transactions(self.db, [('2026-10-01', 'Limit', int(amount * 100), 0)], 'limit.csv')
        self.assertEqual(self.query('SELECT income_cents FROM transactions'), [(9223372036854775807,)])
        with self.assertRaises(pipeline.DataValidationError):
            pipeline.parse_amount('92233720368547758.08', 'income', 2)


if __name__ == '__main__':
    unittest.main()
