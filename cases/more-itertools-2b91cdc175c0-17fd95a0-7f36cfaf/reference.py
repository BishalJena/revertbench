from itertools import permutations

import more_itertools as mi

from unittest import TestCase as _revertbench_test_case

def test_unsortable_hashables():
    self = _revertbench_test_case()
    for iterable in (
        [None, 2, "U"],  # Nothing compares 2U
        ["+", "+", 0, 1],
        ['a', 1, 2, 3],
        ['a', 1, 2, 'b', 3],
        ['a', 1, 2, 'a', 3],
        [1, 2, 0, 4, 'a', 8, 0, 'a'],
        ["", 1, 1, 0, -0.7, 78.23e9],
        ['r', -1, 1, 1, 0, b'\xed'],
        ['ry', 1, 1, 0, b'abc)\xe9'],
        [(0,), 10, 1, 0, 1],
        [(0, 2, 4, 7), 0, 1, 1, (3.45, 2.34, 10.03), 0],
        ['a', (), 7.79, 1, 2, 'bv', b'\x03', (87, 3), "beep"],
        [1, True, 'love'],
    ):
        with self.subTest(iterable=iterable):
            expected = set(permutations(iterable))
            actual = set(mi.distinct_permutations(iter(iterable)))
            self.assertEqual(actual, expected)
