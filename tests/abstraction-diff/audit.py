#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

"""
Audit apparmor.d's abstractions & tunables for co-installing alongside the stock
apparmor package. Read-only; see README.md.

Each abstraction is NEW, COLLISION (shares a stock include-name) or EXTENSION (a
`NAME.d` drop-in dir). A non-`+=` tunable assignment to a stock-defined variable
is a duplicate `@{VAR} =` that apparmor_parser rejects, aborting the whole load,
unless version-gated away from stock-providing apparmor versions.
"""

import argparse
import os
import re
import sys
from pathlib import Path

# apparmor.d dirs default to this repo (this file lives in tests/abstraction-diff/);
# stock dirs to the standard apparmor install root, matching aa-sync's stock= and
# the apparmor utils' /etc/apparmor default. Override either with the flags/env
# vars in build_parser().
REPO = Path(__file__).resolve().parents[2]
STOCK = Path("/etc/apparmor.d")

DEF_AAD_ABS = str(REPO / "apparmor.d" / "abstractions")
DEF_AAD_TUN = str(REPO / "apparmor.d" / "tunables")
DEF_STOCK_ABS = str(STOCK / "abstractions")
DEF_STOCK_TUN = str(STOCK / "tunables")

EXIT_OK, EXIT_VIOLATION, EXIT_USAGE = 0, 1, 2

# apparmor upstreamed the shared primitives (@{d}, @{hex*}, ...) in 4.1; a
# definition only ever built below this can never collide.
STOCK_VAR_FLOOR = 4.1

NEW, COLLISION, EXTENSION = "NEW", "COLLISION", "EXTENSION"

# `#aa:only|exclude ...` directive. `lead` non-empty => inline (gates that one
# line); empty => paragraph (gates to the next blank line).
RE_TUNABLE = re.compile(r"^\s*@\{(?P<var>[A-Za-z0-9_]+)\}\s*(?P<op>\+=|:=|\?=|=)")
RE_AA_FILTER = re.compile(r"^(?P<lead>.*?)#aa:(?P<kw>only|exclude)\b(?P<args>[^\n]*)$")
RE_APPARMOR_COND = re.compile(r"apparmor(?P<op><=|>=|<|>|==|=)(?P<ver>[0-9]+(?:\.[0-9]+)?)")


def _hook_re(base):
    """`include if exists <abstractions/BASE.d>` in any spelling apparmor accepts."""
    return re.compile(r'^\s*#?\s*include\s+if\s+exists\s+[<"]?\s*abstractions/%s\.d\s*[>"]?\s*,?\s*$'
                      % re.escape(base))


def _version_gated(kw, args):
    """True if the directive guarantees the content is never built for apparmor
    >= STOCK_VAR_FLOOR, so it cannot collide with the stock definition."""
    m = RE_APPARMOR_COND.search(args)
    if not m:
        return False
    op, ver = m.group("op"), float(m.group("ver"))
    if kw == "only":
        return (op == "<" and ver <= STOCK_VAR_FLOOR) or (op == "<=" and ver < STOCK_VAR_FLOOR)
    return (op == ">=" and ver <= STOCK_VAR_FLOOR) or (op == ">" and ver < STOCK_VAR_FLOOR)


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def significant_rules(text):
    """Non-comment, whitespace-normalized rule lines, for set-diffing."""
    return {re.sub(r"\s+", " ", s.strip())
            for s in text.splitlines() if s.strip() and not s.strip().startswith("#")}


class Abstraction:
    def __init__(self, name, kind, hook_ok=True, added=None, removed=None):
        self.name = name          # include-name, e.g. "bus/session" or "python.d"
        self.kind = kind
        self.hook_ok = hook_ok    # EXTENSION: stock base exists AND has the hook
        self.added = added or []      # COLLISION: rules vs stock
        self.removed = removed or []


class Violation:
    def __init__(self, file, line, var, op):
        self.file = file
        self.line = line
        self.var = var
        self.op = op


def stock_names(stock_abs):
    if not stock_abs.is_dir():
        return set()
    return {p.relative_to(stock_abs).as_posix() for p in stock_abs.rglob("*") if p.is_file()}


def enumerate_abstractions(aad_abs, stock_abs):
    if not aad_abs.is_dir():
        raise SystemExit(f"apparmor.d abstractions dir not found: {aad_abs}")
    stock = stock_names(stock_abs)
    out = []

    # `NAME.d` snippet dirs are EXTENSIONs, verified against the stock base hook.
    dotd = {d for d in aad_abs.rglob("*.d") if d.is_dir()}
    for d in dotd:
        base = d.relative_to(aad_abs).as_posix()[:-2]    # strip trailing ".d"
        base_path = stock_abs / base
        hook_ok = base_path.is_file() and any(
            _hook_re(Path(base).name).match(ln) for ln in read_text(base_path).splitlines())
        out.append(Abstraction(base + ".d", EXTENSION, hook_ok=hook_ok))

    for f in sorted(aad_abs.rglob("*")):
        if not f.is_file() or any(p in dotd for p in f.parents):
            continue
        name = f.relative_to(aad_abs).as_posix()
        if name in stock:
            aad_r = significant_rules(read_text(f))
            stock_r = significant_rules(read_text(stock_abs / name))
            out.append(Abstraction(name, COLLISION,
                                   added=sorted(aad_r - stock_r), removed=sorted(stock_r - aad_r)))
        else:
            out.append(Abstraction(name, NEW))

    out.sort(key=lambda a: (a.kind, a.name))
    return out


def stock_tunable_vars(stock_tun):
    vars_ = set()
    if stock_tun.is_dir():
        for f in stock_tun.rglob("*"):
            if f.is_file():
                vars_.update(m.group("var")
                             for m in map(RE_TUNABLE.match, read_text(f).splitlines()) if m)
    return vars_


def tunable_violations(aad_tun, stock_vars):
    out = []
    if not aad_tun.is_dir():
        return out
    for f in sorted(aad_tun.rglob("*")):
        if not f.is_file():
            continue
        paragraph = False    # armed by an own-line gating directive, cleared by a blank line
        for i, line in enumerate(read_text(f).splitlines(), 1):
            fm = RE_AA_FILTER.match(line)
            if fm and not fm.group("lead").strip():
                if _version_gated(fm.group("kw"), fm.group("args")):
                    paragraph = True
                continue
            if not line.strip():
                paragraph = False
                continue
            m = RE_TUNABLE.match(line)
            if not m:
                continue
            gated = paragraph or bool(fm and _version_gated(fm.group("kw"), fm.group("args")))
            if m.group("op") != "+=" and m.group("var") in stock_vars and not gated:
                out.append(Violation(f, i, m.group("var"), m.group("op")))
    return out


class Audit:
    def __init__(self, abstractions, violations, n_stock_vars, is_built, paths):
        self.abstractions = abstractions
        self.violations = violations
        self.n_stock_vars = n_stock_vars
        self.is_built = is_built
        self.paths = paths

    def collisions(self):
        return [a for a in self.abstractions if a.kind == COLLISION]

    def missing_hook(self):
        return [a for a in self.abstractions if a.kind == EXTENSION and not a.hook_ok]

    def count(self, kind):
        return sum(1 for a in self.abstractions if a.kind == kind)


def run_audit(aad_abs, aad_tun, stock_abs, stock_tun, is_built=False):
    aad_abs, aad_tun = Path(aad_abs), Path(aad_tun)
    stock_abs, stock_tun = Path(stock_abs), Path(stock_tun)
    stock_vars = stock_tunable_vars(stock_tun)
    return Audit(
        abstractions=enumerate_abstractions(aad_abs, stock_abs),
        violations=tunable_violations(aad_tun, stock_vars),
        n_stock_vars=len(stock_vars),
        is_built=is_built,
        paths={"aad": aad_abs.parent, "stock": stock_abs.parent},
    )


def print_summary(a, out=sys.stdout):
    w = out.write
    w(f"apparmor.d vs stock audit -- {'BUILT' if a.is_built else 'SOURCE'} mode\n")
    w(f"  apparmor.d: {a.paths['aad']}\n  stock:      {a.paths['stock']}\n\n")
    w(f"Abstractions: {a.count(NEW)} new, {a.count(COLLISION)} collisions, "
      f"{a.count(EXTENSION)} .d extensions\n")
    for c in a.collisions():
        w(f"  collision {c.name}: +{len(c.added)}/-{len(c.removed)} rule(s) vs stock\n")
    n_ext = a.count(EXTENSION)
    missing_hook = a.missing_hook()
    w(f".d extensions: {n_ext - len(missing_hook)}/{n_ext} have a working stock include hook\n")
    for x in missing_hook:
        w(f"  ! {x.name}: no stock include hook (silently ignored)\n")
    w(f"Tunables: {len(a.violations)} redefinition(s) of stock's {a.n_stock_vars} variables"
      f"{' that survived the build' if a.is_built else ' (build-removed for the providing version)'}\n")
    for v in a.violations:
        w(f"  {'!' if a.is_built else '-'} {v.file}:{v.line}: @{{{v.var}}} {v.op}\n")


def build_parser():
    p = argparse.ArgumentParser(
        prog="audit.py",
        description="Audit apparmor.d abstractions/tunables vs the stock apparmor package.")
    p.add_argument("--built", metavar="DIR",
                   help="audit a prebuild output root (DIR/abstractions, DIR/tunables) instead "
                        "of the source tree -- reflects what actually ships for that build")
    p.add_argument("--aad-abstractions", default=os.environ.get("AAD_ABSTRACTIONS", DEF_AAD_ABS))
    p.add_argument("--aad-tunables", default=os.environ.get("AAD_TUNABLES", DEF_AAD_TUN))
    p.add_argument("--stock-abstractions", default=os.environ.get("STOCK_ABSTRACTIONS", DEF_STOCK_ABS))
    p.add_argument("--stock-tunables", default=os.environ.get("STOCK_TUNABLES", DEF_STOCK_TUN))
    p.add_argument("--check", action="store_true",
                   help="CI gate: exit 1 on a missing .d hook, or -- with --built -- a "
                        "tunable redefinition or name collision that survived the build")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.built:
        built = Path(args.built)
        if not built.is_dir():
            sys.stderr.write(f"error: --built not a directory: {built}\n")
            return EXIT_USAGE
        aad_abs, aad_tun = str(built / "abstractions"), str(built / "tunables")
    else:
        aad_abs, aad_tun = args.aad_abstractions, args.aad_tunables

    for label, path in (("apparmor.d abstractions", aad_abs), ("apparmor.d tunables", aad_tun),
                        ("stock abstractions", args.stock_abstractions),
                        ("stock tunables", args.stock_tunables)):
        if not Path(path).is_dir():
            sys.stderr.write(f"error: {label} not a directory: {path}\n")
            return EXIT_USAGE

    a = run_audit(aad_abs, aad_tun, args.stock_abstractions, args.stock_tunables,
                  is_built=bool(args.built))
    print_summary(a)

    if not args.check:
        return EXIT_OK

    problems = []
    # A missing hook is real in any mode (the build cannot add it). Redefinitions
    # and collisions are enforced on the built tree only: in source they may be
    # dropped per-target by pkg/configure (e.g. multiarch.d/base for >=4.1).
    missing_hook = a.missing_hook()
    if missing_hook:
        problems.append(f"{len(missing_hook)} .d extension(s) with no stock include hook")
    if a.is_built and a.violations:
        problems.append(f"{len(a.violations)} tunable redefinition(s) that survived the build")
    if a.is_built:
        collisions = a.collisions()
        if collisions:
            problems.append(f"{len(collisions)} collision(s) that survived the build")
    if problems:
        sys.stderr.write("FAIL: " + "; ".join(problems) + "\n")
        return EXIT_VIOLATION
    print(f"OK ({'built' if a.is_built else 'source'})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
