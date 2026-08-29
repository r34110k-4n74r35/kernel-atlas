"""Formatting primitives shared by terminal renderers."""


def paint(text: str, code: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on and text else text
