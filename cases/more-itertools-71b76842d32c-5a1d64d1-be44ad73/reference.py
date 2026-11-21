import more_itertools as mi

from unittest import TestCase as _revertbench_test_case

def test_iterator_input():
    self = _revertbench_test_case()
    self.assertEqual(
        mi.product_index(iter(['i', 'a']), iter('snicker'), iter('snack')),
        12,
    )
