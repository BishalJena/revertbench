from __future__ import annotations

import attr

class TestTransformHook:
    """
    Tests for `attrs(tranform_value_serializer=func)`
    """
    def test_hook_generator(self):
        """
        Ensure that `attrs.fields` are correctly recorded when field_transformer is a generator

        Regression test for #1417
        """

        def hook(cls, attribs):
            yield from attribs

        @attr.s(auto_attribs=True, field_transformer=hook)
        class Base:
            x: int

        assert ["x"] == [a.name for a in attr.fields(Base)]
