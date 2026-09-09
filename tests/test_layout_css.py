"""
The page layout must measure, not guess.

The full-height layout was originally `calc(100vh - 41px - 33px - 30px)`:
three hardcoded guesses at the header, the crumb bar and the footer. Every one
of them is a real element whose height depends on font, wrapping and zoom, so
any drift pushes the bottom of the page off the screen -- which is how the
footer shipped half visible. Flex measures the siblings instead.

This is a static check because the failure is invisible to the renderer tests:
every canvas assertion passed while the footer was clipped.
"""
from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(os.path.dirname(HERE), "cartographer", "ui")


def _read(name):
    with open(os.path.join(UI, name), "r", encoding="utf-8") as fh:
        return fh.read()


class LayoutCssTest(unittest.TestCase):
    def test_body_is_a_flex_column(self):
        css = _read("app.css")
        # Anchored at line start: `\bbody\{` also matches the `body{` inside
        # `html,body{height:100%}`, which is a different rule entirely.
        body = re.search(r"(?:^|\n)body\{([^}]*)\}", css, re.S)
        self.assertIsNotNone(body, "no body rule found")
        decl = body.group(1).replace(" ", "").replace("\n", "")
        self.assertIn("display:flex", decl,
                      "body must be a flex column so the middle region can "
                      "take the remaining height")
        self.assertIn("flex-direction:column", decl)

    def test_main_flexes_rather_than_subtracting_guessed_heights(self):
        css = _read("app.css")
        main = re.search(r"\bmain\{([^}]*)\}", css)
        self.assertIsNotNone(main, "no main rule found")
        decl = main.group(1).replace(" ", "")
        self.assertNotIn("calc(100vh", decl,
                         "main must not subtract hardcoded sibling heights; "
                         "use flex:1 so the browser measures them")
        self.assertIn("flex:1", decl)
        self.assertIn("min-height:0", decl,
                      "a flex child needs min-height:0 or its overflowing "
                      "content refuses to shrink")

    def test_no_hardcoded_viewport_arithmetic_for_full_height_regions(self):
        """
        `max-height: calc(100vh - Npx)` on a scrolling PANEL is fine -- it is a
        cap, and being a little off only changes where a scrollbar appears.
        Using it to SIZE a region that must exactly fill the window is the
        pattern that breaks, so only the latter is rejected.
        """
        offenders = []
        for name in ("app.css", "views.js", "app.js", "index.html"):
            text = _read(name)
            # Blank out block comments before scanning, keeping newlines so
            # line numbers still line up. Checking whether a LINE starts with
            # a comment marker is not enough: prose inside a multi-line
            # comment does not, and this very rule was first reported as an
            # offender by its own explanation.
            text = re.sub(r"/\*.*?\*/",
                          lambda m: re.sub(r"[^\n]", " ", m.group(0)),
                          text, flags=re.S)
            for i, line in enumerate(text.splitlines(), 1):
                if "calc(100vh" not in line:
                    continue
                if line.lstrip().startswith("//"):
                    continue
                if "max-height" in line:
                    continue          # a cap, not a layout dimension
                offenders.append("%s:%d %s" % (name, i, line.strip()[:70]))
        self.assertEqual(offenders, [], "viewport arithmetic used to size a "
                                        "region that should flex")

    def test_footer_exists_and_is_not_floated_over_the_layout(self):
        html = _read("index.html")
        css = _read("app.css")
        self.assertIn('id="foot"', html, "footer missing")
        foot = re.search(r"#foot\{([^}]*)\}", css, re.S)
        self.assertIsNotNone(foot, "footer has no rule")
        decl = foot.group(1).replace(" ", "").replace("\n", "")
        # Floating it would silently steal height from the canvas instead of
        # being accounted for in the layout.
        self.assertNotIn("position:fixed", decl)
        self.assertNotIn("position:absolute", decl)


if __name__ == "__main__":
    unittest.main()
