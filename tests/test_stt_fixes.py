"""Words Whisper keeps getting wrong for this user."""
import re

import pytest

from assistant.stt import FIXES


def fix(text: str) -> str:
    for pattern, word in FIXES:
        text = re.sub(pattern, word, text, flags=re.I)
    return text


@pytest.mark.parametrize("heard, meant", [
    ("You might meet in five minutes.", "Remind me in five minutes."),  # 2026-11-14, real log
    ("My meet in 10 minutes", "Remind me in 10 minutes"),
    ("you might meet in the park", "you might meet in the park"),
    ("Remind me in 5 minutes.", "Remind me in 5 minutes."),
    ("open RIV", "open Riff"),
])
def test_fixes(heard, meant):
    assert fix(heard) == meant
