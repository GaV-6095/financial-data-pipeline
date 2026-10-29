import contextlib
import io
import logging
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import financial_pipeline as pipeline


HEADER = 'date,description,income,expenses\n'


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / 'financial_data.csv'

    def calculate(self, text):
        self.path.write_text(text, encoding='utf-8')
        return pipeline.calculate_totals(self.path)

    def test_sample_totals(self):
        self.assertEqual(
            pipeline.calculate_totals(pipeline.PROJECT_DIR / 'financial_data.csv'),
            (Decimal('3250.00'), Decimal('1395.49'), Decimal('1854.51')),
        )

    def test_exact_decimals_and_negative_balance(self):
        result = self.calculate(HEADER + '2026-10-01,A,0.10,0.00\n2026-10-02,B,0.20,0.40\n')
        self.assertEqual(result, (Decimal('0.30'), Decimal('0.40'), Decimal('-0.10')))

    def test_invalid_amounts(self):
        for amount in ('', 'abc', 'NaN', 'Infinity', '-1.00', '1.001'):
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(pipeline.DataValidationError, "Line 2: 'income'"):
                    self.calculate(HEADER + f'2026-10-01,Example,{amount},0.00\n')

    def test_invalid_rows(self):
        for row in (
            '2026-02-30,Example,1.00,0.00',
            '20261001,Example,1.00,0.00',
            '2026-10-01, ,1.00,0.00',
            '2026-10-01,Example,1.00',
            '2026-10-01,Example,1.00,0.00,extra',
        ):
            with self.subTest(row=row):
                with self.assertRaisesRegex(pipeline.DataValidationError, 'Line 2'):
                    self.calculate(HEADER + row + '\n')

    def test_invalid_headers(self):
        for text in ('', 'date,description\n', 'date,description,income,income,expenses\n'):
            with self.subTest(text=text):
                with self.assertRaises(pipeline.DataValidationError):
                    self.calculate(text)

    def test_malformed_csv(self):
        with self.assertRaisesRegex(pipeline.DataValidationError, 'CSV formatting error'):
            self.calculate(HEADER + '2026-10-01,"unterminated,1.00,0.00\n')

    def test_header_only_warns(self):
        with self.assertLogs(pipeline.LOGGER, level='WARNING'):
            self.assertEqual(self.calculate(HEADER), (Decimal('0'),) * 3)

    def test_bom_and_quoted_description(self):
        result = self.calculate('\ufeff' + HEADER + '2026-10-01,"Food, drinks",0.00,5.50\n')
        self.assertEqual(result[1], Decimal('5.50'))

    def test_failures_return_nonzero_without_summary(self):
        errors = [
            FileNotFoundError(), PermissionError(),
            UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid'),
            pipeline.DataValidationError('Line 3: invalid amount'),
            OSError('read failed'), RuntimeError('unexpected'),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(pipeline, 'configure_logging'), \
                     patch.object(pipeline, 'read_transactions', side_effect=error), \
                     self.assertLogs(pipeline.LOGGER, level='ERROR'), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(pipeline.main(), 1)
                self.assertNotIn('Financial summary', output.getvalue())

    def test_log_file_permission_failure(self):
        output = io.StringIO()
        with patch.object(pipeline, 'configure_logging', side_effect=PermissionError('denied')), \
             contextlib.redirect_stderr(output):
            self.assertEqual(pipeline.main(), 1)
        self.assertIn('Cannot open the log file', output.getvalue())

    def test_logging_writes_terminal_and_file(self):
        log_path = Path(self.temp_dir.name) / 'test.log'
        console = io.StringIO()
        old_handlers = pipeline.LOGGER.handlers[:]
        old_level = pipeline.LOGGER.level
        old_propagate = pipeline.LOGGER.propagate
        pipeline.LOGGER.handlers = []
        try:
            with contextlib.redirect_stderr(console):
                pipeline.configure_logging(log_path)
                pipeline.LOGGER.info('Example run completed')
            text = log_path.read_text(encoding='utf-8')
            self.assertIn('UTC | INFO | Example run completed', text)
            self.assertIn('Example run completed', console.getvalue())
        finally:
            for handler in pipeline.LOGGER.handlers[:]:
                handler.close()
                pipeline.LOGGER.removeHandler(handler)
            pipeline.LOGGER.handlers = old_handlers
            pipeline.LOGGER.setLevel(old_level)
            pipeline.LOGGER.propagate = old_propagate


if __name__ == '__main__':
    unittest.main()
