import more_itertools as mi

from unittest import TestCase as _revertbench_test_case

def test_pred_is_none():
    self = _revertbench_test_case()
    falses, trues = mi.partition(None, range(3))
    self.assertEqual(list(falses), [0])
    self.assertEqual(list(trues), [1, 2])
