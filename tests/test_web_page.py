"""The one page, checked the only three ways a Python test usefully can.

Nothing here runs the JavaScript. What it does catch is the class of mistake
that made this file worth writing: a route the page calls that the server never
had, an element id the script reaches for that the markup never grew, and a
setting that renders nowhere because its group has no box - each one invisible
until somebody clicks the thing and gets a blank panel with no error.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import store  # noqa: E402
import web  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")
MAIN = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
WEB_SOURCE = open(os.path.join(APP, "web.py"), encoding="utf-8", newline="").read()


class PageText(unittest.TestCase):
    def test_the_page_has_no_dashes_or_smart_quotes_and_unix_line_endings(self):
        # A dash or a smart quote here is served to every browser and pasted
        # into every screenshot, and \r\n in a served page is a diff nobody can
        # read afterwards.
        #
        # This asserts the rule itself rather than ASCII-only, which was the old
        # proxy for it. A middle dot separator is explicitly allowed in display
        # text, and the ASCII test banned it - so the page had to spell it
        # "&middot;", which esc() then escaped into the literal text "&middot;"
        # on the one line that joined it before escaping. Ban the characters
        # that are actually banned, and the correct character stays available.
        for bad in ("—", "–", "‒", "―", "−",  # dashes
                    "‘", "’", "“", "”",           # smart quotes
                    "\r\n"):
            self.assertNotIn(bad, WEB_SOURCE)
        # It must still be encodable as what it is actually served as.
        web.PAGE.encode("utf-8")

    def test_separators_are_literal_characters_not_html_entities(self):
        # The regression this pins: a separator joined into a string BEFORE
        # esc() runs comes out as the visible text "&middot;", which is what a
        # real QNAP showed under CPU. Literal characters survive either side of
        # escaping, so the page must not carry the entity form at all.
        page_without_comments = re.sub(r"//[^\n]*", "", web.PAGE)
        for entity in ("&middot;", "&deg;", "&bull;", "&ndash;", "&mdash;"):
            self.assertNotIn(entity, page_without_comments)


def handler_bodies():
    """The source of each do_VERB, so a route can be checked against the VERB
    that carries it and not merely against the file."""
    parts = re.split(r"\n    def do_(GET|PUT|POST|DELETE)\(", MAIN)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def calls():
    """(method, path) for every api()/fetch() in the page. A call with no
    method is a GET, the same as the browser's default."""
    found = set()
    for m in re.finditer(r"(?:fetch|api)\('(/[^']*)'(.{0,60})", web.PAGE, re.S):
        method = re.search(r"method:'([A-Z]+)'", m.group(2))
        found.add((method.group(1) if method else "GET", m.group(1)))
    return found


class Routes(unittest.TestCase):
    def test_every_route_the_page_calls_exists_in_main(self):
        import main

        called = {path for _method, path in calls()}
        self.assertIn("/api/login", called)   # the regex still finds anything
        self.assertIn("/healthz", called)
        for raw in sorted(called):
            route, _api = main._route(raw)
            # Trailing slash because the page builds /api/sessions/ + an id; the
            # handler matches that with a regex containing the same prefix.
            self.assertIn(route.rstrip("/"), MAIN, f"the page calls {raw} and main.py has no such route")

    def test_every_route_is_called_with_a_method_that_handles_it(self):
        """The path existing somewhere in main.py is not the question.

        /api/settings has a GET and a PUT and no POST. The trash tab posted to
        it, fell through do_POST to the catch-all, and answered "Not Found" at
        somebody who had just typed a number into a box - while the older test
        here passed, because the string /api/settings is certainly in main.py.
        """
        import main

        bodies = handler_bodies()
        self.assertEqual(set(bodies), {"GET", "PUT", "POST", "DELETE"}, "the handler split stopped working")
        for method, raw in sorted(calls()):
            route, _api = main._route(raw)
            # assertTrue, not assertIn: assertIn prints the haystack, and the
            # haystack here is an entire request handler.
            self.assertTrue(route.rstrip("/") in bodies[method],
                            f"the page sends {method} {raw}, and do_{method} has no such route "
                            f"- it falls through to the catch-all 404")


class Elements(unittest.TestCase):
    def test_every_id_the_script_reaches_for_is_in_the_markup(self):
        ids = set(re.findall(r'\sid="([A-Za-z0-9_]+)"', web.PAGE))
        wanted = set(re.findall(r"\$\('([A-Za-z0-9_]+)'\)", web.PAGE))
        # say() and saveSettings() take ids as bare strings rather than through
        # $(), so a typo in one of those is exactly as fatal and exactly as
        # silent.
        for a, b in re.findall(r"saveSettings\('([A-Za-z0-9_]+)','([A-Za-z0-9_]+)'\)", web.PAGE):
            wanted |= {a, b}
        wanted |= set(re.findall(r"say\('([A-Za-z0-9_]+)'", web.PAGE))
        self.assertTrue(wanted, "the id regex matched nothing, so this test proves nothing")
        self.assertEqual(set(), wanted - ids)

    def test_every_tab_button_has_a_section(self):
        tabs = set(re.findall(r'<button data-tab="([a-z]+)"', web.PAGE))
        sections = set(re.findall(r'<section id="([a-z]+)"', web.PAGE))
        self.assertEqual(tabs, sections)


class SettingsCoverage(unittest.TestCase):
    def test_every_visible_setting_group_has_a_box_on_the_page(self):
        # The settings form renders itself from the server's specs, which is
        # only true for a group the page names. Adding a Spec in a new group and
        # nothing else must fail here rather than ship a control nobody can see.
        boxes = dict(re.findall(r"\['([a-z]+form)','([A-Za-z]+)'\]", web.PAGE))
        for spec in store.SPECS:
            if spec.hidden:
                continue
            self.assertIn(spec.group, boxes.values(),
                          f"setting {spec.key} is in group {spec.group}, which renders nowhere")

    def test_the_page_offers_a_password_field_for_a_secret(self):
        # store.redact_secrets sends back a mask, and a text input would show
        # the stars to the room while making them look like a real value.
        self.assertIn("spec.secret", web.PAGE)


if __name__ == "__main__":
    unittest.main()
