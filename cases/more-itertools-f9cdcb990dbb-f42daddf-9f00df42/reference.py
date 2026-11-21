import more_itertools as mi

from unittest import TestCase as _revertbench_test_case

def test_eq_above_maxsize():
    self = _revertbench_test_case()
    stop = 10**30
    cases = [
        ((0, stop, 1), (0, stop, 1), True),
        ((0, stop, 2), (0, stop - 1, 2), True),
        ((stop, 0, -2), (stop, 1, -2), True),
        ((0, stop, 1), (0, stop + 1, 1), False),
        ((0, stop, 1), (1, stop + 1, 1), False),
        ((0, stop, 1), (0, stop * 2, 2), False),
    ]
    for args1, args2, expected in cases:
        with self.subTest(args1=args1, args2=args2):
            range1 = mi.numeric_range(*args1)
            range2 = mi.numeric_range(*args2)
            self.assertEqual(range(*args1) == range(*args2), expected)
            self.assertEqual(range1 == range2, expected)
