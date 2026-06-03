#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "dists", "aa-sync")


def load_module(name="aa_sync_under_test"):
    loader = SourceFileLoader(name, SCRIPT)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


aas = load_module()


class TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aa-sync-test.")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make_config(self, **over):
        """Build a real Config from a minimal, correctly-permissioned conf file."""
        share = os.path.join(self.tmp, over.pop("_share", "share"))
        os.makedirs(os.path.join(share, "profiles"), exist_ok=True)
        conf = os.path.join(self.tmp, "aa-sync-%d.conf" % len(os.listdir(self.tmp)))
        sel = over.get("selection", {})
        stab = over.get("stability", {})
        lines = ["[selection]",
                 "mode=%s" % sel.get("mode", "all"),
                 "groups=%s" % sel.get("groups", ""),
                 "include=%s" % sel.get("include", ""),
                 "include_used=%s" % sel.get("include_used", ""),
                 "exclude=%s" % sel.get("exclude", ""),
                 "[stability]",
                 "stable=%s" % stab.get("stable", "enabled"),
                 "testing=%s" % stab.get("testing", "disabled"),
                 "disabled=%s" % stab.get("disabled", "disabled")]
        with open(conf, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(conf, 0o644)
        args = SimpleNamespace(config=conf, share=share, stock=None,
                               state=None, stability=None)
        return aas.Config(args)

    def make_index(self, profiles, from_index_json=True):
        return aas.Index(profiles, from_index_json=from_index_json)

    def prof(self, name, stability="testing", attach=(), groups=()):
        return aas.Profile(name, Path("/nonexistent") / name,
                           stability=stability, attach=list(attach), groups=list(groups))


# read_stability

class TestReadStability(TestBase):
    def _write(self, obj):
        p = Path(self.tmp) / ("stab-%d.json" % id(obj))
        p.write_text(json.dumps(obj))
        return p

    def test_maps_profile_to_class(self):
        p = self._write({"version": aas.STABILITY_SCHEMA,
                         "stable": ["a", "b"], "testing": ["c"], "disabled": ["d"]})
        self.assertEqual(aas.read_stability(p),
                         {"a": "stable", "b": "stable", "c": "testing", "d": "disabled"})

    def test_absent_default_testing_via_empty_map(self):
        p = self._write({"version": aas.STABILITY_SCHEMA, "stable": ["x"]})
        m = aas.read_stability(p)
        self.assertEqual(m, {"x": "stable"})
        self.assertNotIn("unlisted", m)  # callers fall back to "testing"

    def test_missing_file_returns_empty(self):
        self.assertEqual(aas.read_stability(Path(self.tmp) / "nope.json"), {})

    def test_none_path_returns_empty(self):
        self.assertEqual(aas.read_stability(None), {})

    def test_malformed_json_refuses(self):
        p = Path(self.tmp) / "bad.json"
        p.write_text("{ this is not json")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                aas.read_stability(p)
        self.assertEqual(cm.exception.code, aas.EXIT_CONFIG)

    def test_warns_on_schema_mismatch(self):
        p = self._write({"version": aas.STABILITY_SCHEMA + 999, "stable": ["a"]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            m = aas.read_stability(p)
        self.assertEqual(m, {"a": "stable"})  # still parses despite mismatch
        self.assertIn("schema", buf.getvalue())

    def test_no_warn_on_matching_schema(self):
        p = self._write({"version": aas.STABILITY_SCHEMA, "stable": ["a"]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            aas.read_stability(p)
        self.assertEqual(buf.getvalue(), "")

    def test_no_version_no_warn(self):
        p = self._write({"stable": ["a"]})
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            m = aas.read_stability(p)
        self.assertEqual(m, {"a": "stable"})
        self.assertEqual(buf.getvalue(), "")


# select() / compute_used fast path

class TestSelect(TestBase):
    def _index(self, from_index_json=True):
        return self.make_index({
            "uname": self.prof("uname", "stable", attach=["/{,usr/}bin/uname"], groups=["a"]),
            "ping": self.prof("ping", "testing", attach=["/{,usr/}bin/ping"], groups=["net"]),
            "secret": self.prof("secret", "disabled", attach=["/x/secret"], groups=["sec"]),
        }, from_index_json=from_index_json)

    def test_mode_all_respects_stability(self):
        cfg = self.make_config(selection={"mode": "all"})  # only stable enabled
        sel, reasons = aas.select(cfg, self._index())
        self.assertEqual(sel, {"uname"})
        self.assertIn("not enabled", reasons["ping"])
        self.assertIn("not enabled", reasons["secret"])

    def test_mode_all_multiple_stabilities(self):
        cfg = self.make_config(selection={"mode": "all"},
                               stability={"stable": "enabled", "testing": "enabled"})
        sel, _ = aas.select(cfg, self._index())
        self.assertEqual(sel, {"uname", "ping"})

    def test_mode_no_selects_nothing(self):
        cfg = self.make_config(selection={"mode": "no"})
        sel, _ = aas.select(cfg, self._index())
        self.assertEqual(sel, set())

    def test_include_forces_on_but_not_disabled(self):
        cfg = self.make_config(selection={"mode": "all", "include": "ping secret"})
        sel, reasons = aas.select(cfg, self._index())
        self.assertEqual(sel, {"uname", "ping"})  # secret is disabled -> not forced
        self.assertEqual(reasons["ping"], "force-included")

    def test_include_glob(self):
        cfg = self.make_config(selection={"mode": "no", "include": "p*"},
                               stability={"stable": "enabled", "testing": "enabled"})
        sel, _ = aas.select(cfg, self._index())
        self.assertEqual(sel, {"ping"})

    def test_include_used_needs_the_binary(self):
        idx = self.make_index({
            "ping": self.prof("ping", "testing", attach=["/{,usr/}bin/ping"]),
            "ghost": self.prof("ghost", "testing", attach=["/nonexistent/bin/ghost"]),
            "secret": self.prof("secret", "disabled", attach=["/{,usr/}bin/ping"]),
        })
        cfg = self.make_config(selection={"mode": "no", "include_used": "ping ghost secret"})
        sel, reasons = aas.select(cfg, idx)
        self.assertEqual(sel, {"ping"})  # binary present, stability bypassed
        self.assertEqual(reasons["ping"], "force-included (binary present)")
        self.assertEqual(reasons["ghost"], "include_used: binary not present")
        self.assertNotIn("secret", sel)  # disabled stability still refused

    def test_exclude_beats_include_used(self):
        idx = self.make_index({
            "ping": self.prof("ping", "testing", attach=["/{,usr/}bin/ping"]),
        })
        cfg = self.make_config(selection={"mode": "no", "include_used": "ping",
                                          "exclude": "ping"})
        sel, _ = aas.select(cfg, idx)
        self.assertEqual(sel, set())

    def test_exclude_wins(self):
        cfg = self.make_config(selection={"mode": "all", "exclude": "uname"})
        sel, reasons = aas.select(cfg, self._index())
        self.assertEqual(sel, set())
        self.assertEqual(reasons["uname"], "force-excluded")

    def test_exclude_beats_include(self):
        cfg = self.make_config(selection={"mode": "no", "include": "ping",
                                          "exclude": "ping"},
                               stability={"stable": "enabled", "testing": "enabled"})
        sel, reasons = aas.select(cfg, self._index())
        self.assertEqual(sel, set())
        self.assertEqual(reasons["ping"], "force-excluded")

    def test_mode_group(self):
        cfg = self.make_config(selection={"mode": "group", "groups": "net"},
                               stability={"stable": "enabled", "testing": "enabled"})
        sel, _ = aas.select(cfg, self._index())
        self.assertEqual(sel, {"ping"})

    def test_mode_used_fast_path_matches_existing_binary(self):
        # Create a real file the attach pattern matches; rewrite the index attach
        # patterns to point under the tmp tree.
        bindir = os.path.join(self.tmp, "usr", "bin")
        os.makedirs(bindir)
        open(os.path.join(bindir, "uname"), "w").close()
        idx = self.make_index({
            "uname": self.prof("uname", "stable",
                               attach=[self.tmp + "/{,usr/}bin/uname"]),
            "ping": self.prof("ping", "stable",
                              attach=[self.tmp + "/{,usr/}bin/ping"]),  # absent
        }, from_index_json=True)
        cfg = self.make_config(selection={"mode": "used"})
        sel, reasons = aas.select(cfg, idx)
        self.assertEqual(sel, {"uname"})
        self.assertIn("binary not present", reasons["ping"])

    def test_compute_used_directly(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        open(os.path.join(bindir, "ping"), "w").close()
        idx = self.make_index({
            "ping": self.prof("ping", "stable", attach=[self.tmp + "/bin/ping"]),
            "gone": self.prof("gone", "stable", attach=[self.tmp + "/bin/gone"]),
        }, from_index_json=True)
        cfg = self.make_config(selection={"mode": "used"})
        used = aas.compute_used(cfg, idx, ["ping", "gone"])
        self.assertEqual(used, {"ping"})

    def test_reasons_cover_selected(self):
        cfg = self.make_config(selection={"mode": "all"})
        sel, reasons = aas.select(cfg, self._index())
        for name in sel:
            self.assertIn(name, reasons)


# build_index fallback (no index.json)

class TestBuildIndexFallback(TestBase):
    def test_refuses_without_any_metadata(self):
        cfg = self.make_config()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                aas.build_index(cfg)
        self.assertEqual(cm.exception.code, aas.EXIT_CONFIG)

    def test_scans_with_stability_json(self):
        cfg = self.make_config()
        (cfg.share / "stability.json").write_text(json.dumps(
            {"version": aas.STABILITY_SCHEMA, "stable": ["uname"]}))
        (cfg.share / "profiles" / "uname").write_text(
            "profile uname /usr/bin/uname {\n}\n")
        idx = aas.build_index(cfg)
        self.assertFalse(idx.have_metadata)
        self.assertEqual(idx["uname"].stability, "stable")


# incremental apply

class TestIncrementalApply(TestBase):
    def setUp(self):
        super().setUp()
        self._orig = (aas._run, aas.apparmor_enabled, aas.is_disabled, aas.kernel_loaded_names)
        self.calls = []
        aas._run = lambda cmd: (self.calls.append(cmd), aas._Result(0, ""))[1]
        aas.apparmor_enabled = lambda: True
        aas.is_disabled = lambda: None
        aas.kernel_loaded_names = lambda: set()
        self.addCleanup(self._restore)

    def _restore(self):
        aas._run, aas.apparmor_enabled, aas.is_disabled, aas.kernel_loaded_names = self._orig

    def _cfg(self):
        cfg = self.make_config(selection={"mode": "all"})
        pdir = cfg.share / "profiles"
        (pdir / "foo").write_text("profile foo /usr/bin/foo {\n}\n")
        (pdir / "bar").write_text("profile bar /usr/bin/bar {\n}\n")
        (cfg.share / "stability.json").write_text(json.dumps(
            {"version": aas.STABILITY_SCHEMA, "stable": ["foo", "bar"]}))
        cfg.state = Path(self.tmp) / "state"
        cfg.stock = Path(self.tmp) / "stock"   # empty: hermetic include hash
        return cfg

    def _apply(self, cfg, **kw):
        kw = {"dry_run": False, "no_reload": False, "force": False,
              "now": time.time(), **kw}
        args = SimpleNamespace(**kw)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = aas.do_apply(cfg, args)
        self.assertEqual(rc, aas.EXIT_OK)
        return out.getvalue()

    def _replace_targets(self):
        return [[Path(t).name for t in cmd[cmd.index("--") + 1:]]
                for cmd in self.calls if "--replace" in cmd]

    def test_unchanged_apply_loads_nothing(self):
        cfg = self._cfg()
        self._apply(cfg)
        self.assertEqual(self._replace_targets(), [["bar", "foo"]])
        self.calls.clear()
        out = self._apply(cfg)
        self.assertEqual(self._replace_targets(), [])
        self.assertIn("(2 unchanged)", out)

    def test_modified_profile_reloads_only_it(self):
        cfg = self._cfg()
        self._apply(cfg)
        (cfg.share / "profiles" / "foo").write_text(
            "profile foo /usr/bin/foo {\n  /etc/hosts r,\n}\n")
        self.calls.clear()
        self._apply(cfg)
        self.assertEqual(self._replace_targets(), [["foo"]])

    def test_force_reloads_all(self):
        cfg = self._cfg()
        self._apply(cfg)
        self.calls.clear()
        self._apply(cfg, force=True)
        self.assertEqual(self._replace_targets(), [["bar", "foo"]])

    def test_reboot_forces_full_reload(self):
        cfg = self._cfg()
        self._apply(cfg)
        orig = aas.booted_since
        aas.booted_since = lambda ts: True   # pretend the system rebooted
        self.addCleanup(setattr, aas, "booted_since", orig)
        self.calls.clear()
        self._apply(cfg)
        self.assertEqual(self._replace_targets(), [["bar", "foo"]])

    def test_include_change_forces_full_reload(self):
        cfg = self._cfg()
        self._apply(cfg)
        (cfg.share / "abstractions").mkdir()
        (cfg.share / "abstractions" / "x").write_text("# new abstraction\n")
        self.calls.clear()
        self._apply(cfg)
        self.assertEqual(self._replace_targets(), [["bar", "foo"]])


# explain

class TestExplain(TestBase):
    def test_unselected_shows_enable_hint_not_load_cmd(self):
        cfg = self.make_config(selection={"mode": "all"})
        (cfg.share / "profiles" / "ytdl").write_text("profile ytdl /usr/bin/ytdl {\n}\n")
        (cfg.share / "stability.json").write_text(json.dumps(
            {"version": aas.STABILITY_SCHEMA, "testing": ["ytdl"]}))
        orig = aas.kernel_loaded_names
        aas.kernel_loaded_names = lambda: set()
        self.addCleanup(setattr, aas, "kernel_loaded_names", orig)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = aas.do_explain(cfg, SimpleNamespace(name="ytdl"))
        self.assertEqual(rc, aas.EXIT_OK)
        self.assertIn("enable:    add 'include = ytdl'", out.getvalue())
        self.assertNotIn("load cmd", out.getvalue())


# _remove_files kernel reconciliation

class TestRemoveFiles(TestBase):
    NAMES = {"apt": ["apt", "apt//pager"]}

    def setUp(self):
        super().setUp()
        self._orig = aas.kernel_loaded_names
        self.addCleanup(setattr, aas, "kernel_loaded_names", self._orig)

    def _gone_file(self):
        return [("apt", Path(self.tmp) / "active" / "apt")]

    def test_gone_file_not_loaded_is_completed_drop(self):
        aas.kernel_loaded_names = lambda: {"dpkg"}
        failed = aas._remove_files(self.make_config(), self._gone_file(), names_map=self.NAMES)
        self.assertEqual(failed, set())

    def test_gone_file_still_loaded_is_failure(self):
        aas.kernel_loaded_names = lambda: {"apt//pager"}
        with contextlib.redirect_stderr(io.StringIO()):
            failed = aas._remove_files(self.make_config(), self._gone_file(), names_map=self.NAMES)
        self.assertEqual(failed, {"apt"})

    def test_without_trustworthy_names_fails_loudly(self):
        aas.kernel_loaded_names = lambda: {"dpkg"}
        with contextlib.redirect_stderr(io.StringIO()):
            failed = aas._remove_files(self.make_config(), self._gone_file(), names_map=None)
        self.assertEqual(failed, {"apt"})


# add_exec_fallback

class TestExecFallback(unittest.TestCase):
    def test_converts_px_and_Px(self):
        self.assertEqual(aas.add_exec_fallback("  @{bin}/foo px,\n"),
                         "  @{bin}/foo pux,\n")
        self.assertEqual(aas.add_exec_fallback("  @{bin}/foo rPx,\n"),
                         "  @{bin}/foo rPux,\n")
        self.assertEqual(aas.add_exec_fallback("  /usr/bin/b mrpx,\n"),
                         "  /usr/bin/b mrpux,\n")

    def test_converts_with_explicit_target(self):
        self.assertEqual(aas.add_exec_fallback("  @{bin}/foo rPx -> child,\n"),
                         "  @{bin}/foo rPux -> child,\n")

    def test_leaves_fallback_and_other_modes(self):
        for rule in ("  @{bin}/a pix,\n", "  @{bin}/a rPUx,\n", "  @{bin}/a pux,\n",
                     "  @{bin}/a ix,\n", "  @{bin}/a rCx -> sandbox,\n",
                     "  @{bin}/a ux,\n"):
            self.assertEqual(aas.add_exec_fallback(rule), rule)

    def test_leaves_paths_alone(self):
        self.assertEqual(aas.add_exec_fallback("  /usr/bin/px r,\n"),
                         "  /usr/bin/px r,\n")


# kernel_drift

class TestKernelDrift(TestBase):
    MANIFEST = {"profiles": {"apt": "x"}, "loaded": True, "have_names": True,
                "names": {"apt": ["apt", "apt//pager"]}}

    def setUp(self):
        super().setUp()
        self._orig = aas.kernel_loaded_names, aas.is_disabled, aas.apparmor_enabled
        aas.is_disabled = lambda: None
        aas.apparmor_enabled = lambda: True
        self.addCleanup(self._restore)

    def _restore(self):
        aas.kernel_loaded_names, aas.is_disabled, aas.apparmor_enabled = self._orig

    def test_children_match_kernel_list(self):
        aas.kernel_loaded_names = lambda: {"apt", "apt//pager"}
        missing, why = aas.kernel_drift(self.make_config(), self.MANIFEST)
        self.assertEqual(missing, set())

    def test_missing_child_is_drift(self):
        aas.kernel_loaded_names = lambda: {"apt"}
        missing, why = aas.kernel_drift(self.make_config(), self.MANIFEST)
        self.assertEqual(missing, {"apt//pager"})

    def test_unreadable_kernel_state_is_not_drift(self):
        aas.kernel_loaded_names = lambda: None
        missing, why = aas.kernel_drift(self.make_config(), self.MANIFEST)
        self.assertIsNone(missing)
        self.assertIn("need root", why)


# Config parsing of selection/stability/mode

class TestConfig(TestBase):
    def test_enabled_stability_set(self):
        cfg = self.make_config(stability={"stable": "enabled", "testing": "enabled",
                                           "disabled": "disabled"})
        self.assertEqual(cfg.enabled_stability, {"stable", "testing"})

    def test_split_lists(self):
        cfg = self.make_config(selection={"include": "a, b  c", "exclude": "x"})
        self.assertEqual(cfg.include, ["a", "b", "c"])
        self.assertEqual(cfg.exclude, ["x"])

    def test_validate_rejects_bad_mode(self):
        cfg = self.make_config(selection={"mode": "bogus"})
        with self.assertRaises(SystemExit):
            cfg.validate()

    def test_validate_group_requires_groups(self):
        cfg = self.make_config(selection={"mode": "group", "groups": ""})
        with self.assertRaises(SystemExit):
            cfg.validate()

    def test_validate_accepts_used(self):
        cfg = self.make_config(selection={"mode": "used"})
        cfg.validate()  # must not raise


if __name__ == "__main__":
    unittest.main()
