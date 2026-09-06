"""In-memory terminal streams and SGR stripping for presentation assertions."""

import io
import re


class Terminal(io.StringIO):
    def isatty(self):
        return True


def plain(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
