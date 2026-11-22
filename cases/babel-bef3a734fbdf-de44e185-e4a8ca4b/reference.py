from io import BytesIO

from babel.messages import extract

def test_issue_1195():
    buf = BytesIO(b"""
foof = {
    'test_string': StringWithMeta(
        # NOTE: Text describing a test string
        string=_(
            'Text string that is on a new line'
        ),
    ),
}
""")
    messages = list(extract.extract('python', buf, {'_': None}, ["NOTE"], {}))
    message = messages[0]
    assert message[0] in (5, 6)  # Depends on whether #1126 is in
    assert message[1] == 'Text string that is on a new line'
    assert message[2] == ['NOTE: Text describing a test string']
