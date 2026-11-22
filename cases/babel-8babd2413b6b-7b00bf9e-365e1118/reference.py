from io import BytesIO

from babel.messages import extract

def test_extract_byte_string_does_not_crash():
    """Byte strings like _(b'foo') should be skipped without crashing."""
    buf = BytesIO(b"""\
_(b'foo')
_('hello')
_(b"bar")
_('world')
""")
    messages = list(extract.extract('python', buf, {'_': None}, [], {}))
    assert len(messages) == 2
    assert messages[0][1] == 'hello'
    assert messages[1][1] == 'world'
