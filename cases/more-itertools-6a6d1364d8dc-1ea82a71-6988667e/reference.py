import more_itertools as mi

from unittest import TestCase as _revertbench_test_case

def test_zero_nonempty():
    self = _revertbench_test_case()
    iterable = [1, 2, 3]
    actual = [list(c) for c in mi.ichunked(iterable, 0)]
    self.assertEqual(actual, [])

    source = iter([1, 2, 3])
    self.assertEqual([list(c) for c in mi.ichunked(source, 0)], [])
    self.assertEqual(list(source), [1, 2, 3])
