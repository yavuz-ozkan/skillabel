"""The sticky comment step edits only the bot's own comment (H13 / T13).

The filter is taken from action.yml as it is and run with jq on a made-up
page of comments: a stranger's comment that holds the marker comes first.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from test_action import _run_scripts

MARKER = "<!-- skillabel -->"
COMMENTS = [
    {"id": 1, "user": {"login": "stranger", "type": "User"}, "body": f"{MARKER} fake label: all clean"},
    {"id": 2, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": "unrelated bot note"},
    {"id": 3, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": f"{MARKER}\n## Skillabel"},
    {"id": 4, "user": {"login": "github-actions[bot]", "type": "User"}, "body": MARKER},
]


def _filter() -> str:
    post = dict(_run_scripts())["Post sticky PR comment"]
    m = re.search(r'--jq "((?:[^"\\]|\\.)*)"', post)
    assert m, "no --jq filter in the post step"
    return m.group(1).replace('\\"', '"').replace("$MARKER", MARKER)


def test_only_the_bots_own_marked_comment_is_picked():
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is not on this machine")
    out = subprocess.run([jq, "-r", _filter()], input=json.dumps(COMMENTS), capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["3"]


def test_no_bot_comment_means_a_new_one_is_made():
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq is not on this machine")
    out = subprocess.run([jq, "-r", _filter()], input=json.dumps(COMMENTS[:2]), capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
