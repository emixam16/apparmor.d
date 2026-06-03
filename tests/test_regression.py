#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

"""End-to-end regression test for the deployment tooling (dists/aa-sync,
dists/gen-stability) against a small throwaway fixture. No root, no kernel."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AA_SYNC = ROOT / "dists" / "aa-sync"
GEN_STABILITY = ROOT / "dists" / "gen-stability"

# A literal attachment that exists on essentially every Linux host, and one that
# does not. The brace expands to /bin/... and /usr/bin/...; either match counts.
PRESENT_ATTACH = "/{,usr/}bin/uname"
PRESENT_NAME = "uname"
ABSENT_ATTACH = "/{,usr/}bin/aa-sync-regression-absent-xyz"
ABSENT_NAME = "ghosttool"

PROFILE_TMPL = """\
abi <abi/4.0>,
include <tunables/global>
profile {name} {attach} flags=(attach_disconnected) {{
  include <abstractions/base>
  {attach} mr,
}}
"""


def host_has_present_binary():
    return any(Path(p).exists() for p in ("/bin/uname", "/usr/bin/uname"))


class RegressionTest(unittest.TestCase):
    def setUp(self):
        if not host_has_present_binary():
            self.skipTest("no /bin/uname on this host")
        self.tmp = Path(tempfile.mkdtemp(prefix="aa-sync-regtest."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.share = self.tmp / "share"
        self.state = self.tmp / "state"
        self._build_fixture()

    def _build_fixture(self):
        """Minimal self-contained share tree: two profiles, stub abstractions and
        tunables so the overlay parse aa-sync does has every include resolve."""
        pdir = self.share / "profiles"
        pdir.mkdir(parents=True)
        (self.share / "abstractions").mkdir()
        (self.share / "tunables").mkdir()
        (self.share / "abstractions" / "base").write_text("# stub\n")
        (self.share / "tunables" / "global").write_text("@{etc_ro}=/etc/\n")

        (pdir / PRESENT_NAME).write_text(
            PROFILE_TMPL.format(name=PRESENT_NAME, attach=PRESENT_ATTACH))
        (pdir / ABSENT_NAME).write_text(
            PROFILE_TMPL.format(name=ABSENT_NAME, attach=ABSENT_ATTACH))

        # Both profiles are stable so selection differences come purely from
        # mode=used (binary presence), not the stability gate.
        self.stability = self.share / "stability.json"
        self.stability.write_text(json.dumps({
            "version": 1,
            "stable": [PRESENT_NAME, ABSENT_NAME],
            "testing": [],
            "disabled": [],
        }))

    def _run(self, *cmd_args):
        cmd = [sys.executable, str(AA_SYNC),
               "--share", str(self.share),
               "--stability", str(self.stability),
               "--state", str(self.state)] + list(cmd_args)
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))

    def _build_index(self):
        index_path = self.share / "index.json"
        res = self._run("index", "--out", str(index_path))
        self.assertEqual(res.returncode, 0, res.stderr)
        return index_path

    def test_index_metadata(self):
        index_path = self._build_index()
        data = json.loads(index_path.read_text())
        self.assertGreaterEqual(data.get("version"), 1)
        profiles = data["profiles"]
        self.assertEqual(set(profiles), {PRESENT_NAME, ABSENT_NAME})
        self.assertEqual(profiles[PRESENT_NAME]["stability"], "stable")
        self.assertEqual(profiles[ABSENT_NAME]["stability"], "stable")
        self.assertEqual(profiles[PRESENT_NAME]["names"], [PRESENT_NAME])
        # Attachments are resolved back out of the parsed profile.
        self.assertIn(PRESENT_ATTACH, profiles[PRESENT_NAME]["attach"])
        self.assertIn(ABSENT_ATTACH, profiles[ABSENT_NAME]["attach"])

    def test_selection_is_stable_intersect_used(self):
        """list (and apply --dry-run) select stable profiles whose attached binary
        exists: present -> in, absent -> out."""
        self._build_index()

        res = self._run("list")
        self.assertEqual(res.returncode, 0, res.stderr)
        selected = set(res.stdout.split())
        self.assertIn(PRESENT_NAME, selected)
        self.assertNotIn(ABSENT_NAME, selected)

        res = self._run("apply", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        # dry-run reports additions vs the (empty) applied set.
        self.assertIn("+ " + PRESENT_NAME, res.stdout)
        self.assertNotIn(ABSENT_NAME, res.stdout)

    def test_selection_without_index_json(self):
        """Same verdict via the share-tree parse fallback (no index.json)."""
        self.assertFalse((self.share / "index.json").exists())
        res = self._run("list")
        self.assertEqual(res.returncode, 0, res.stderr)
        selected = set(res.stdout.split())
        self.assertIn(PRESENT_NAME, selected)
        self.assertNotIn(ABSENT_NAME, selected)

    def test_gen_stability_idempotent(self):
        """The committed dists/stability.json reflects the source tree: a fresh
        classification is byte-identical, so --check is clean."""
        res = subprocess.run([sys.executable, str(GEN_STABILITY), "--check"],
                             capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(res.returncode, 0,
                         "gen-stability --check not clean:\n%s" % res.stderr)


if __name__ == "__main__":
    unittest.main()
