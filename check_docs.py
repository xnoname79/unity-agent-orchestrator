#!/usr/bin/env python3
"""Guards for the public user guide in documentation/.

What can go wrong quietly, because nothing builds the site:
  - a link, image or #anchor that points nowhere, or an absolute URL that breaks under the
    /unity-agent-orchestrator/ project path on GitHub Pages;
  - a page whose copy of the nav misses a page, since each page repeats it by hand;
  - a dashboard label in the text that the dashboard no longer has;
  - a setting in the text that the server no longer reads;
  - a release shipped without its changelog entry (GITHUB_REF_NAME is the tag in CI).

    python3 check_docs.py
"""
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "documentation"
NAME_RE = re.compile(r"ORCH_[A-Z0-9_]+|CLAUDE_BIN")
fails = []


def fail(where, problem):
    fails.append(f"{where}: {problem}")
    print(f"FAIL {where}: {problem}")


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.sections, self.nav, self.urls, self.labels = set(), set(), set(), [], []
        self.nav_depth = 0
        self.ui_depth = 0
        self.ui_text = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
            if tag == "section":
                self.sections.add(a["id"])
        if a.get("name") and tag == "a":
            self.ids.add(a["name"])
        for key in ("href", "src"):
            if a.get(key) is not None:
                self.urls.append(a[key])
        if tag == "nav":
            self.nav_depth += 1
        elif self.nav_depth and tag == "a" and a.get("href"):
            self.nav.add(a["href"])
        if self.ui_depth:
            if tag == "span":
                self.ui_depth += 1
        elif tag == "span" and "ui" in (a.get("class") or "").split():
            self.ui_depth, self.ui_text = 1, ""

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "span" and self.ui_depth:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "nav" and self.nav_depth:
            self.nav_depth -= 1
        if tag == "span" and self.ui_depth:
            self.ui_depth -= 1
            if not self.ui_depth:
                self.labels.append(self.ui_text.strip())

    def handle_data(self, data):
        if self.ui_depth:
            self.ui_text += data


def is_external(url):
    parts = urlsplit(url)
    return bool(parts.scheme) or url.startswith("//")


def check_url(where, url, pages):
    """Every internal URL must be relative and resolve inside documentation/, #anchor included."""
    if is_external(url):
        return
    if url.startswith("/"):
        fail(where, f"absolute URL breaks on a project site: {url}")
        return
    parts = urlsplit(url)
    source = DOCS / where
    target = source if not parts.path else (source.parent / unquote(parts.path)).resolve()
    if DOCS not in target.parents:
        fail(where, f"link leaves documentation/: {url}")
    elif not target.is_file():
        fail(where, f"broken link: {url}")
    elif parts.fragment:
        page = pages.get(target.name) if target.parent == DOCS else None
        if page is None or unquote(parts.fragment) not in page.ids:
            fail(where, f"broken anchor: {url}")


def main():
    if not DOCS.is_dir():
        fail("documentation", "folder is missing")
        return
    pages, raw = {}, {}
    for path in sorted(DOCS.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        parser = Page()
        parser.feed(text)
        parser.close()
        pages[path.name], raw[path.name] = parser, text
    if "changelog.html" not in pages:
        fail("changelog.html", "page is missing")

    static = ROOT / "static" / "orchestrator"
    ui_source = "".join((static / n).read_text(encoding="utf-8") for n in ("index.html", "app.js"))
    code = "".join((ROOT / n).read_text(encoding="utf-8")
                   for n in ("session_orchestrator.py", "team.py", "signal_mcp.py"))

    for name, page in pages.items():
        for url in page.urls:
            check_url(name, url, pages)
        linked = {urlsplit(u).path for u in page.nav if not is_external(u)}
        for other in pages:
            if other != name and other not in linked:
                fail(name, f"nav does not link to {other}")
        for label in page.labels:
            if not label or label not in ui_source:
                fail(name, f'class="ui" label is not in the dashboard: "{label}"')
        for found in sorted(set(NAME_RE.findall(raw[name]))):
            if found not in code:
                fail(name, f"{found} is not read by the server")
    print(f"ok   {len(pages)} pages read")

    css = DOCS / "style.css"
    if css.is_file():
        for url in re.findall(r"""url\(\s*["']?([^"')]+)""", css.read_text(encoding="utf-8")):
            check_url("style.css", url, pages)

    tag = os.environ.get("GITHUB_REF_NAME", "")
    if tag.startswith("v") and "changelog.html" in pages and tag not in pages["changelog.html"].sections:
        fail("changelog.html", f'no <section id="{tag}"> for this release')


main()
print(f"\n{len(fails)} failure(s)" if fails else "\nall docs checks passed")
sys.exit(1 if fails else 0)
