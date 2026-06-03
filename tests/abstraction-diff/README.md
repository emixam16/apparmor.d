<!-- SPDX-License-Identifier: GPL-2.0-only -->

# Abstraction & tunable co-install audit

`audit.py` is a read-only check for shipping apparmor.d **alongside** the distro's
stock `apparmor` package instead of clobbering its abstractions and tunables. It
compares apparmor.d's abstraction/tunable trees against the stock ones and flags
what would silently break a co-installed deployment. It never modifies anything.

## What it checks

1. **Classifies every apparmor.d abstraction**:
   - `NEW` — no stock abstraction shares its include-name; cannot collide.
   - `COLLISION` — same include-name as a stock abstraction; shipping it would
     replace the stock file (reported with the +added/-removed rule delta).
   - `EXTENSION` — a `NAME.d/` snippet dir pulled into the stock `NAME`
     abstraction via its `include if exists <abstractions/NAME.d>` hook.
2. **`.d` drop-in coverage**: each `NAME.d/` needs stock to ship `NAME` *and*
   contain the include hook, else the snippet is silently dropped.
3. **Additive-only tunables**: a non-`+=` assignment to a variable stock already
   defines is a duplicate declaration — `apparmor_parser` rejects a second
   `@{VAR} =` and aborts the **whole** policy load. Such a definition must be
   `+=` or version-gated (`#aa:only apparmor<4.1`, paragraph or inline) so it is
   never built where stock provides it. This is how `multiarch.d/base` ships the
   primitives apparmor upstreamed in 4.1 (`@{d}`, `@{hex*}`, …). An *ungated*
   redefinition is flagged.

## Source vs built mode

- **Source (default)** audits the in-repo `apparmor.d/{abstractions,tunables}` —
  what the repo carries, before per-version build configuration.
- **`--built <DIR>`** audits `<DIR>/{abstractions,tunables}` from a prebuild
  output — what actually ships after `pkg/configure` drops the files the target
  apparmor version provides itself:

  | target | removed (relevant here) |
  | --- | --- |
  | apparmor &ge; 4.1 | `abstractions/devices-usb`, `devices-usb-read`, `nameservice-strict`, `tunables/multiarch.d/base` |
  | apparmor &ge; 5.0 | also `dig`, `free`, `nslookup` |

So a &ge; 4.1 build resolves the three collisions and the `multiarch.d/base`
primitives; a &lt; 4.1 build keeps them.

## Usage

```sh
python3 audit.py                                             # summary, source tree
python3 audit.py --check                                     # CI gate, source tree
python3 audit.py --built .build/enforce/apparmor.d           # summary of a build
python3 audit.py --check --built .build/enforce/apparmor.d   # CI gate on a build
```

"Stock" defaults to `/etc/apparmor.d/{abstractions,tunables}`. All four trees are
overridable by flag (`--aad-abstractions`, `--aad-tunables`,
`--stock-abstractions`, `--stock-tunables`) or the matching `AAD_*` / `STOCK_*`
environment variables; `--built` overrides the two `--aad-*` paths.

## Exit codes (`--check`)

| code | meaning |
| --- | --- |
| `0` | no fatal findings. |
| `1` | a `.d` extension with no stock hook (any mode), or — with `--built` — a tunable redefinition / collision that survived the build. Offending `file:line` / names go to stderr. |
| `2` | usage error (a required tree is missing / not a directory). |

Without `--check` the tool is informational and always exits `0`. Tunable
redefinitions and collisions are fatal **only** in built mode, since in source
they may be dropped per target by the build; a missing `.d` hook is fatal in any
mode, as the build cannot add a hook to stock.
