"""Host input validation, also usable in roles without remote worker imports."""


def validate_stdin(value):
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError('stdin must be UTF-8 text or null')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError('stdin must be valid UTF-8 text') from exc
