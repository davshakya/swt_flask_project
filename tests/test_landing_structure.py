"""Guard against container closures that move landing content outside its grid."""
from html.parser import HTMLParser
from pathlib import Path


class LandingStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = (tag, dict(attrs))
        self.nodes.append((node, list(self.stack)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


def test_hero_and_landing_sections_stay_inside_page_container():
    parser = LandingStructure()
    parser.feed((Path(__file__).parents[1] / "flask_app/templates/login.html").read_text(encoding="utf-8"))
    sections = {"how-it-works", "benefits", "comparison", "demo", "solutions", "proof", "buyer-confidence", "setup-check"}
    found = set()
    for (tag, attrs), parents in parser.nodes:
        if "hero-preview" in attrs.get("class", "").split():
            assert parents[-1][0] == "section"
            assert "hero" in parents[-1][1].get("class", "").split()
        if tag == "section" and attrs.get("id") in sections:
            found.add(attrs["id"])
            assert any("page" in parent_attrs.get("class", "").split() for _, parent_attrs in parents), attrs["id"]
    assert found == sections
