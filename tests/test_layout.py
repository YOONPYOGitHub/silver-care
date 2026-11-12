from html.parser import HTMLParser
from pathlib import Path


class LayoutParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.paths = {}
        self.errors = []

    def handle_starttag(self, tag, attrs):
        metadata = dict(attrs)
        identifier = metadata.get("id")
        node = identifier or metadata.get("class") or tag
        if identifier:
            if identifier in self.paths:
                self.errors.append(f"Duplicate id: {identifier}")
            self.paths[identifier] = [entry[1] for entry in self.stack] + [node]
        if tag not in {"meta", "link", "img", "input", "br", "hr"}:
            self.stack.append((tag, node))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append(f"Unbalanced closing tag: {tag}")
        else:
            self.stack.pop()


def test_scroll_and_dock_are_separate_balanced_regions_with_unique_controls():
    root = Path(__file__).resolve().parents[1]
    parser = LayoutParser()
    parser.feed((root / "silvercare/static/index.html").read_text())
    assert not parser.errors
    assert not parser.stack
    assert parser.paths["conversation-scroll"][-2:] == ["conversation", "conversation-scroll"]
    assert parser.paths["conversation-dock"][-2:] == ["conversation", "conversation-dock"]
    for control in ("mode-text", "mode-voice", "chat-form", "voice-panel", "new-answer"):
        assert "conversation-dock" in parser.paths[control]
        assert "conversation-scroll" not in parser.paths[control]
    for control in ("welcome", "topics", "messages"):
        assert "conversation-scroll" in parser.paths[control]
        assert "conversation-dock" not in parser.paths[control]
    for control in ("font-normal", "font-large", "close-settings"):
        assert "screen-settings" in parser.paths[control]
    assert "voice-to-text" not in parser.paths
