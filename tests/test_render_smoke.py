"""
Runs the canvas renderers and fails if any camera state emits invalid geometry.

This exists because a UI bug of this class is invisible to every other test in
the suite: the Python side was entirely green while the Layers tab was blank in
the browser. The renderers are the only part of the tool with no coverage, and
they are also the part a user looks at first.

Skips cleanly when Node is unavailable, so the suite still runs on a machine
without it -- the extraction and analysis tests are the load-bearing ones and
must not be blocked by a missing optional dependency.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "render_smoke.js")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RenderSmokeTest(unittest.TestCase):
    def test_no_camera_state_emits_invalid_geometry(self):
        proc = subprocess.run(
            ["node", SCRIPT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
        )
        out = proc.stdout.decode("utf-8", "replace")
        self.assertEqual(
            proc.returncode, 0,
            "renderer emitted invalid canvas geometry.\n"
            "A throw inside draw() aborts the frame after clearRect and before\n"
            "the next requestAnimationFrame, so the canvas stays blank while\n"
            "nodes remain clickable at stale positions.\n\n" + out,
        )
        self.assertIn("PASS", out)


if __name__ == "__main__":
    unittest.main()
