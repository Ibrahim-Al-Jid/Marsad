"""English UI translations of server messages must still match the messages the server sends."""
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1]


def test_translated_server_messages_exist_in_the_backend():
    client = (ROOT / "frontend" / "src" / "platformClient.js").read_text(encoding="utf-8")
    block = client[client.index("const knownErrors = {"):client.index("};", client.index("const knownErrors = {"))]
    keys = [json.loads(match) for match in re.findall(r'^  ("[^"]+"):', block, re.M)]
    assert len(keys) >= 15
    source = "".join(path.read_text(encoding="utf-8") for path in (ROOT / "backend").glob("*.py")
                     if not path.name.startswith("test_"))
    assert [key for key in keys if key not in source] == []


def test_every_github_import_message_has_an_english_translation():
    """A GitHub failure whose Arabic text has no translation (and an unmapped status like 413) showed the generic
    "The request could not be completed." in the English interface."""
    client = (ROOT / "frontend" / "src" / "platformClient.js").read_text(encoding="utf-8")
    source = "\n".join((ROOT / "backend" / name).read_text(encoding="utf-8")
                       for name in ("github_import.py", "personal_github.py"))
    constants = dict(re.findall(r'^([A-Z_]+) = ("[^"]+")$', source, re.M))
    raised = re.findall(r'GitHubFailure\(\d+, ("[^"]*[\u0600-\u06ff][^"]*"|[A-Z_]+)\)', source)
    raised += re.findall(r'HTTPException\((?:502|503|504), ("[^"]*[\u0600-\u06ff][^"]*")', source)
    messages = {constants.get(item, item) for item in raised}
    assert len(messages) >= 8
    assert [message for message in sorted(messages) if message not in client] == []


def test_unmapped_statuses_have_a_specific_fallback():
    client = (ROOT / "frontend" / "src" / "platformClient.js").read_text(encoding="utf-8")
    for status in (413, 500, 502, 503, 504):
        assert re.search(rf"^\s+{status}: ", client, re.M)
