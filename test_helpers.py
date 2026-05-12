import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

class _Stub:
    def __getattr__(self, _):
        return _Stub
    def __call__(self, *a, **k):
        return _Stub()

def _make_stub(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda _attr: _Stub
    return m

for _name in ('adsk', 'adsk.core', 'adsk.fusion', 'adsk.cam'):
    sys.modules.setdefault(_name, _make_stub(_name))
sys.modules['adsk'].core = sys.modules['adsk.core']
sys.modules['adsk'].fusion = sys.modules['adsk.fusion']
sys.modules['adsk'].cam = sys.modules['adsk.cam']

import BatchParametricExport as bpe


class ExpandRangeTests(unittest.TestCase):
    def test_padded_range(self):
        self.assertEqual(bpe._expand_range('001..003'), ['001', '002', '003'])

    def test_padded_range_width_from_max(self):
        self.assertEqual(bpe._expand_range('01..099')[:3], ['001', '002', '003'])
        self.assertEqual(bpe._expand_range('01..099')[-1], '099')

    def test_unpadded_range(self):
        self.assertEqual(bpe._expand_range('1..5'), ['1', '2', '3', '4', '5'])

    def test_descending(self):
        self.assertEqual(bpe._expand_range('003..001'), ['003', '002', '001'])

    def test_single_token_not_range(self):
        self.assertIsNone(bpe._expand_range('001'))

    def test_negative(self):
        self.assertEqual(bpe._expand_range('-2..2'), ['-2', '-1', '0', '1', '2'])

    def test_whitespace_tolerated(self):
        self.assertEqual(bpe._expand_range(' 1 .. 3 '), ['1', '2', '3'])


class ParseValuesNumericTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(bpe._parse_values_list('1; 2.5; 3', False), ['1', '2.5', '3'])

    def test_preserves_leading_zeros(self):
        self.assertEqual(bpe._parse_values_list('001; 042; 100', False), ['001', '042', '100'])

    def test_skips_empty_tokens(self):
        self.assertEqual(bpe._parse_values_list('1;;2;  ;3', False), ['1', '2', '3'])

    def test_range_expands(self):
        self.assertEqual(bpe._parse_values_list('001..003', False), ['001', '002', '003'])

    def test_mixed_range_and_singles(self):
        self.assertEqual(bpe._parse_values_list('001..003; 010', False), ['001', '002', '003', '010'])

    def test_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            bpe._parse_values_list('hello', False)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            bpe._parse_values_list('', False)
        with self.assertRaises(ValueError):
            bpe._parse_values_list(' ; ; ', False)


class ParseValuesTextTests(unittest.TestCase):
    def test_accepts_arbitrary_strings(self):
        self.assertEqual(bpe._parse_values_list('a; bb; c-1', True), ['a', 'bb', 'c-1'])

    def test_strips_surrounding_quotes(self):
        self.assertEqual(bpe._parse_values_list("'001'; \"002\"", True), ['001', '002'])

    def test_range_in_text(self):
        self.assertEqual(bpe._parse_values_list('001..003', True), ['001', '002', '003'])

    def test_preserves_padded_strings(self):
        self.assertEqual(bpe._parse_values_list('001; 002; 099', True), ['001', '002', '099'])


class BuildFilenameTests(unittest.TestCase):
    def test_basic_substitution(self):
        out = bpe._build_filename('{name}_{TagIndex}.obj', 'tag', {'TagIndex': '001'})
        self.assertEqual(out, 'tag_001.obj')

    def test_preserves_leading_zero_string(self):
        out = bpe._build_filename('{name}_{idx}.obj', 'tag', {'idx': '007'})
        self.assertEqual(out, 'tag_007.obj')

    def test_sanitizes_obj_name(self):
        out = bpe._build_filename('{name}.obj', 'foo/bar:baz', {})
        self.assertEqual(out, 'foobarbaz.obj')

    def test_sanitizes_param_values(self):
        out = bpe._build_filename('{name}_{p}.obj', 'tag', {'p': 'a/b'})
        self.assertEqual(out, 'tag_ab.obj')

    def test_multiple_params(self):
        out = bpe._build_filename('{name}_{a}_{b}.obj', 'x', {'a': '001', 'b': '5.5'})
        self.assertEqual(out, 'x_001_5.5.obj')


class SimpleLiteralTests(unittest.TestCase):
    def test_matches_with_unit(self):
        self.assertTrue(bpe._is_simple_literal('2 mm'))
        self.assertTrue(bpe._is_simple_literal('-3.5 deg'))

    def test_matches_without_unit(self):
        self.assertTrue(bpe._is_simple_literal('42'))

    def test_rejects_formula(self):
        self.assertFalse(bpe._is_simple_literal('a + 1'))
        self.assertFalse(bpe._is_simple_literal('19/param3*3.14'))

    def test_rejects_text_expression(self):
        self.assertFalse(bpe._is_simple_literal("'001'"))


if __name__ == '__main__':
    unittest.main()
