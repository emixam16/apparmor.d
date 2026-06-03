#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

import importlib.util
import os
import shutil
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "dists", "gen-stability")


def load_module(name="gen_stability_under_test"):
    loader = SourceFileLoader(name, SCRIPT)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TestClassifyFixture(unittest.TestCase):
    """classify() against a synthetic fixture tree (module globals repointed)."""

    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.mkdtemp(prefix="gen-stability-test.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = self.tmp
        pdir = os.path.join(root, "apparmor.d")
        # Source profiles: some under groups/, some under profiles-*.
        write(os.path.join(pdir, "groups", "net", "uname"))
        write(os.path.join(pdir, "groups", "net", "ping"))
        write(os.path.join(pdir, "profiles-a-f", "cpuid"))
        write(os.path.join(pdir, "profiles-a-f", "broken"))
        write(os.path.join(pdir, "profiles-m-r", "man"))
        # Stuff outside groups/ and profiles-* must NOT count as a source profile.
        write(os.path.join(pdir, "abstractions", "base"))
        # flags: cpuid forced complain -> demoted out of stable even if tested.
        write(os.path.join(root, "dists", "flags", "main.flags"),
              "# header\nuname enforce\ncpuid complain  # noisy\n\n")
        # ignore: one bare profile name + one directory of profiles.
        write(os.path.join(root, "dists", "ignore", "main.ignore"),
              "# header\nman\napparmor.d/groups/wip\n\n")
        write(os.path.join(pdir, "groups", "wip", "broken"))
        # integration tests: uname, cpuid, ping have their own .bats.
        write(os.path.join(root, "tests", "integration", "uname.bats"))
        write(os.path.join(root, "tests", "integration", "sub", "cpuid.bats"))
        write(os.path.join(root, "tests", "integration", "ping.bats"))
        write(os.path.join(root, "tests", "integration", "common.bash"))
        self.mod.ROOT = root
        self.mod.PROFILES = pdir

    def test_source_profiles_only_from_groups_and_profiles(self):
        self.assertEqual(
            self.mod.source_profiles(),
            {"uname", "ping", "cpuid", "broken", "man"},
        )

    def test_classify_partitions(self):
        c = self.mod.classify()
        # uname: tested, not complain, not ignored -> stable.
        # ping:  tested, not complain, not ignored -> stable.
        # cpuid: tested but complain-flagged -> demoted to testing.
        # man:   ignored by name -> disabled.
        # broken: ignored via wip/ dir AND it is a source profile -> disabled.
        self.assertEqual(c["stable"], ["ping", "uname"])
        self.assertEqual(c["testing"], ["cpuid"])
        self.assertEqual(c["disabled"], ["broken", "man"])

    def test_classes_disjoint_and_cover(self):
        c = self.mod.classify()
        stable, testing, disabled = set(c["stable"]), set(c["testing"]), set(c["disabled"])
        self.assertEqual(stable & testing, set())
        self.assertEqual(stable & disabled, set())
        self.assertEqual(testing & disabled, set())
        self.assertEqual(stable | testing | disabled, self.mod.source_profiles())

    def test_complain_demotes_tested(self):
        c = self.mod.classify()
        self.assertNotIn("cpuid", c["stable"])
        self.assertIn("cpuid", c["testing"])

    def test_ignore_wins_over_tested(self):
        # Add a .bats for an ignored profile: disabled must still win.
        write(os.path.join(self.mod.ROOT, "tests", "integration", "man.bats"))
        c = self.mod.classify()
        self.assertIn("man", c["disabled"])
        self.assertNotIn("man", c["stable"])
        self.assertNotIn("man", c["testing"])

    def test_lists_sorted(self):
        c = self.mod.classify()
        for key in ("stable", "testing", "disabled"):
            self.assertEqual(c[key], sorted(c[key]), "%s not sorted" % key)


class TestClassifyRealRepo(unittest.TestCase):
    """Invariants on the actual checked-in repo tree."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_module("gen_stability_real")
        cls.classes = cls.mod.classify()
        cls.sources = cls.mod.source_profiles()

    def test_disjoint(self):
        s = set(self.classes["stable"])
        t = set(self.classes["testing"])
        d = set(self.classes["disabled"])
        self.assertEqual(s & t, set())
        self.assertEqual(s & d, set())
        self.assertEqual(t & d, set())

    def test_cover_source_profiles(self):
        union = set(self.classes["stable"]) | set(self.classes["testing"]) | set(self.classes["disabled"])
        self.assertEqual(union, self.sources)

    def test_render_roundtrips(self):
        text = self.mod.render(self.classes)
        import json
        data = json.loads(text)
        self.assertEqual(data["version"], self.mod.SCHEMA)
        for key in ("stable", "testing", "disabled"):
            self.assertEqual(data[key], self.classes[key])


if __name__ == "__main__":
    unittest.main()
