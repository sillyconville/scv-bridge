# -*- coding: utf-8 -*-
"""argv invariants. ⭐What this test pins down: "the remote side cannot slip a single character into the command
line" and "not one isolation flag is missing"."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests import helpers

import scv  # noqa: E402


def setUpModule():
    helpers.fresh_home("argv", unittest.addModuleCleanup)

HEAD = ["claude"]


class ClaudeArgv(unittest.TestCase):
    def test_isolation_flags_all_present(self):
        a = scv.claude_argv(HEAD, "haiku", "S.txt", "I.json")
        for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--disable-slash-commands",
                     "--exclude-dynamic-system-prompt-sections", "--include-partial-messages", "--verbose"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--disallowedTools") + 1], "*")
        self.assertEqual(a[a.index("--system-prompt-file") + 1], "S.txt")
        self.assertEqual(a[a.index("--settings") + 1], "I.json")
        self.assertEqual(a[a.index("--input-format") + 1], "stream-json")
        self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
        self.assertNotIn("--bare", a)          # --bare does not read OAuth, so a subscription account cannot log in through it

    def test_effort_is_optional_and_closed(self):
        self.assertNotIn("--effort", scv.claude_argv(HEAD, "haiku", "S", "I"))
        a = scv.claude_argv(HEAD, "haiku", "S", "I", effort="low")
        self.assertEqual(a[a.index("--effort") + 1], "low")
        with self.assertRaises(scv.BridgeError) as cm:
            scv.claude_argv(HEAD, "haiku", "S", "I", effort="low & calc")
        self.assertEqual(cm.exception.klass, "bad_request")


# Task 13c: the `-c` entries layered on top of the player's own config.toml (each scalar is replaced outright).
#   ⭐The criterion must live in exactly one place (13c review M8: these three expected values used to be written
#   twice, once in `CodexArgv` and once in `CodexArgvPairs`) ⇒ both tests read this one tuple. What each one
#   carries, what it blocks:
#   📎 NOTES.md::codex-user-home inside scv.py (zero-quota: drop just this one and that same thing goes back into
#   the request sent to the model).
PLAYER_OVERRIDES = ('developer_instructions=""', "notify=[]", "features.memories=false", "features.multi_agent_v2=false")


class CodexArgv(unittest.TestCase):
    def test_shape(self):
        a = scv.codex_argv(["codex"], "gpt-6-luna", "low")
        self.assertEqual(a[:4], ["codex", "app-server", "--listen", "stdio://"])
        self.assertIn('model="gpt-6-luna"', a)
        self.assertIn('model_reasoning_effort="low"', a)
        self.assertIn("features.shell_tool=false", a)
        for name in scv.CODEX_OFF:
            self.assertIn("features." + name + "=false", a)
        self.assertEqual([x for x in a if "login_method" in x], [])     # B22: does not care how the CLI logs in

    def test_what_the_players_own_config_toml_would_bring_is_overridden(self):
        """Task 13c: codex runs in the player's own CODEX_HOME (no login needed) ⇒ everything in his config.toml
        comes along by default. The `-c` entries sit on top of that layer, replacing the scalar outright: his
        standing instructions (`developer_instructions`), the external program started at the end of every turn
        (`notify`, which gets handed that turn's content), the memory summary (`memories`), the family of tools
        for spawning sub-agents (`multi_agent_v2`).
        ⭐Assert the correct value — never just "does not contain his value": get one character of the value
        wrong (`notify=[""]`) and this test must go red."""
        a = scv.codex_argv(["codex"], "gpt-6-luna", "low")
        settings = [a[i + 1] for i, x in enumerate(a[:-1]) if x == "-c"]
        for want in PLAYER_OVERRIDES:
            self.assertIn(want, settings)

    def test_no_override_that_only_looks_like_it_blocks_something(self):
        """🔴Two flags that "look like they block something but don't" — pin them down so they don't come back:
        leaving them in the "what command line it assembles" section is lying to whoever audits it:
        ① `-c mcp_servers={}`: measured, does not block it (09-24: in config/read the sessionFlags layer is `{}`,
          but his own value is still there in the effective settings; the canary server came up and
          `tools/list` was called) ⇒ MCP is now turned off in thread/start, one name at a time;
        ② `-c skills.include_instructions=false` (present in the 13c first cut): once thread/start's `config`
          carries a `skills` table, it stops taking effect (Fix 1, zero-quota, measured: when thread/start only
          turns off skills without suppressing the manifest, the manifest still goes into the request) ⇒ the
          manifest field moved into that same table in thread/start.
        Both are now pinned by tests/test_30_drivers.py::CodexUserStuffIsTurnedOff."""
        a = scv.codex_argv(["codex"], "gpt-6-luna", "low")
        self.assertEqual([x for x in a if x.startswith(("mcp_servers", "skills."))], [])


class ClosedSet(unittest.TestCase):
    CAT = ["claude/haiku", "claude/sonnet", "codex/gpt-6-luna"]

    def test_known_model_resolves(self):
        self.assertEqual(scv.resolve_model("claude/sonnet", self.CAT), ("claude", "sonnet"))

    def test_anything_else_is_refused(self):
        for bad in ("claude/opus", "sonnet", "claude/sonnet & calc", "claude/../x", "", None, 'claude/so"nnet'):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.resolve_model(bad, self.CAT)
            self.assertEqual(cm.exception.klass, "bad_request")

    def test_extra_models_are_validated_at_load(self):
        found = {"claude": {"head": HEAD, "version": "x", "blocked": ""}}
        cfg = {"extra_models": {"claude": ["fable", "bad name", "x&y"], "codex": []}}
        cat = scv.catalog(cfg, found)
        self.assertIn("claude/fable", cat)
        self.assertEqual([m for m in cat if "bad" in m or "&" in m], [])

    def test_blocked_family_is_not_offered(self):
        found = {"claude": {"head": HEAD, "version": "x", "blocked": "这一版不认 --safe-mode"}}
        self.assertEqual(scv.catalog({"extra_models": {}}, found), [])


class AuthWhitelist(unittest.TestCase):
    def test_claude_status_drops_identity_fields(self):
        raw = ('{"loggedIn": true, "authMethod": "claude.ai", "email": "a@b.c", "orgId": "o", '
               '"orgName": "n", "subscriptionType": "max"}')
        self.assertEqual(scv._claude_auth_from_json(raw), {"logged_in": True, "method": "claude.ai", "plan": "max"})


# ━━━━━━━━━━ Everything below was added beyond the brief: the brief's Step 1 does not cover
#            cli_head/detect/auth_status at all, and carry-forward #2 (--safe-mode is mandatory) lives entirely
#            inside detect().

def _cat(cfg=None):
    """Run a real detect() once (the stub replaces cli_head), then compute the catalog."""
    cfg = dict({"extra_models": {}}, **(cfg or {}))
    with mock.patch.object(scv, "cli_head", helpers.fake_head):
        found = scv.detect(cfg)
    return found, scv.catalog(cfg, found)


_shell_line = helpers.pasted_for


class _StubEnv(unittest.TestCase):
    """Test cases that change the FAKE_* environment variables: must restore them afterward, or a later test goes
    green against someone else's taste."""

    KEYS = ("FAKE_MODE", "FAKE_NO_SAFE_MODE", "FAKE_HELP_ON_STDERR", "FAKE_HELP_RC",
            "FAKE_STATUS_BROKEN", "FAKE_LOG", "CODEX_HOME")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def own_log(self):
        """A stub log of its own: never read the one the module shares (other test cases are appending to it too
        ⇒ which one `[-1]` picks up depends on test run order)."""
        d = tempfile.mkdtemp(prefix="argv-log-")
        self.addCleanup(shutil.rmtree, d, True)
        os.environ["FAKE_LOG"] = os.path.join(d, "fake.jsonl")
        return os.environ["FAKE_LOG"]


class CliHead(_StubEnv):
    """⭐A configured value must be checked for existence before it is used — Codex's self-update swaps out that
    hashed directory, so a path written into config.json yesterday may not be there today."""

    def test_configured_binary_that_exists_wins(self):
        self.assertEqual(scv.cli_head("claude", {"claude_bin": sys.executable}), [sys.executable])

    def test_stale_configured_binary_falls_back_to_path(self):
        """The other half of the negative control: the config has a path that no longer exists ⇒ it must never be
        used as-is, it must fall back to PATH."""
        gone = os.path.join(tempfile.gettempdir(), "scv-no-such-codex-4e7a.exe")
        self.assertFalse(os.path.exists(gone))
        with mock.patch.object(shutil, "which", return_value="/usr/bin/codex"):
            self.assertEqual(scv.cli_head("codex", {"codex_bin": gone}), ["/usr/bin/codex"])

    def test_nothing_anywhere_is_none(self):
        empty = tempfile.mkdtemp(prefix="empty-")
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.object(shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": empty}):
            self.assertIsNone(scv.cli_head("claude", {}))
            self.assertIsNone(scv.cli_head("codex", {}))

    def test_npm_shim_goes_through_cmd_on_windows(self):
        """What npm installs is a .cmd, which can only be started via `cmd /c`; on POSIX, never wrap it in an
        extra layer for no reason."""
        shim = "C:" + chr(92) + "x" + chr(92) + "claude.CMD"
        with mock.patch.object(shutil, "which", return_value=shim):
            head = scv.cli_head("claude", {})
        self.assertEqual(head, ["cmd", "/c", shim] if os.name == "nt" else [shim])

    def test_codex_glob_picks_the_newest_hash_dir(self):
        """Codex's self-update ⇒ a new copy shows up at `%LOCALAPPDATA%/OpenAI/Codex/bin/<hash>/codex.exe`.
        The criterion is the one with the newest mtime — asserting the correct value (the new one), never just
        "not equal to the old one"."""
        root = tempfile.mkdtemp(prefix="localappdata-")
        self.addCleanup(shutil.rmtree, root, True)
        made = []
        for i, name in enumerate(("aaaa1111", "bbbb2222")):
            d = os.path.join(root, "OpenAI", "Codex", "bin", name)
            os.makedirs(d)
            exe = os.path.join(d, "codex.exe")
            with open(exe, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(exe, (time.time() - 100 + i * 50,) * 2)
            made.append(exe)
        with mock.patch.object(shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": root}):
            self.assertEqual(scv.cli_head("codex", {}), [made[1]])


class SafeModeGate(_StubEnv):
    """carry-forward #2: `--safe-mode` is a hard requirement (without it, the user-level ~/.claude/CLAUDE.md still
    makes it into the context). ⇒ if this version of the CLI does not recognize it, the whole family is refused;
    never silently fall back to an argv without it."""

    def test_present_means_not_blocked_and_models_are_offered(self):
        """Negative control: when the stub's --help does have --safe-mode, blocked must be an empty string.
        Without this half, the test below ("blocked without it") would also go green on an implementation that
        is always blocked."""
        found, cat = _cat()
        self.assertEqual(found["claude"]["blocked"], "")
        self.assertIn("claude/haiku", cat)

    def test_missing_safe_mode_blocks_the_whole_family(self):
        os.environ["FAKE_NO_SAFE_MODE"] = "1"
        found, cat = _cat()
        self.assertIn("--safe-mode", found["claude"]["blocked"])        # pins down why it was refused
        self.assertEqual([m for m in cat if m.startswith("claude/")], [])
        self.assertIn("codex/gpt-6-luna", cat)                        # refuse only this family, never implicate the other

    def test_blocked_family_still_reports_itself(self):
        """A refused family must never disappear from detect(): doctor needs its version number to tell the user
        which one to upgrade."""
        os.environ["FAKE_NO_SAFE_MODE"] = "1"
        found, _cat_ = _cat()
        self.assertIn("claude", found)
        self.assertIn("fake claude", found["claude"]["version"])

    def test_version_is_the_first_line_of_the_cli_output(self):
        found, _cat_ = _cat()
        self.assertEqual(found["claude"]["version"], "0.0.0 (fake claude)")
        self.assertEqual(found["codex"]["version"], "0.0.0 (fake codex)")

    @staticmethod
    def _detect_a_binary_that_is_not_there():
        gone = [os.path.join(tempfile.gettempdir(), "scv-no-such-cli-9b31.exe")]
        with mock.patch.object(scv, "cli_head", lambda family, cfg: gone):
            return scv.detect({"extra_models": {}})

    def test_a_cli_that_will_not_start_is_not_silently_fine(self):
        """Errors must be loud: the probe never even ran ⇒ version must carry the OS's own words, never leave an
        empty string pretending everything is fine.
        ⚠️And the claude family must be blocked at this point — being unable to ask --help is the same as "we
        don't know whether it recognizes it", fail-closed.
        This test only pins the direction: whether the reason is right is `ProbeFailuresDoNotLie`'s job (this
        test used to also pin "explaining a startup failure with the version reason" as expected behavior)."""
        found = self._detect_a_binary_that_is_not_there()
        self.assertIn("never ran", found["claude"]["version"])
        self.assertNotEqual(found["claude"]["blocked"], "")
        self.assertEqual([m for m in scv.catalog({"extra_models": {}}, found) if m.startswith("claude/")], [])

    def test_codex_also_fails_closed_when_its_binary_will_not_start(self):
        """Both families' failure direction must fail closed, consistently. On the codex side `detect()` only
        asks `--version`; it used to still not be blocked when that could not be answered ⇒ a codex that cannot
        even run `--version` would still show up in `/v1/models` and only blow up once someone actually clicks
        it. Now the gate for "does it have local credentials" (`_codex_gate`) closes this one too: cannot start
        ⇒ cannot ask about credentials ⇒ blocked."""
        found = self._detect_a_binary_that_is_not_there()
        self.assertIn("never ran", found["codex"]["version"])
        self.assertNotEqual(found["codex"]["blocked"], "")
        self.assertEqual(scv.catalog({"extra_models": {}}, found), [])


class _FreshHome(_StubEnv):
    """`load_config()` mints a token / fills in defaults and writes them to disk ⇒ each test case gets a clean
    SCV_HOME, never let them bleed into each other."""

    def setUp(self):
        super().setUp()
        self.old_home = os.environ.get("SCV_HOME")
        self.home = tempfile.mkdtemp(prefix="scv-cfg-")
        os.environ["SCV_HOME"] = self.home
        self.addCleanup(self._put_home_back)

    def _put_home_back(self):
        if self.old_home is None:
            os.environ.pop("SCV_HOME", None)
        else:
            os.environ["SCV_HOME"] = self.old_home
        shutil.rmtree(self.home, ignore_errors=True)


class NoDedicatedCodexHome(_FreshHome):
    """Task 13c (the maintainer's ruling on 09-24, never re-litigate this): codex goes back to living in the
    player's own CODEX_HOME — no login required.
    This test used to pin "by default, point codex at a dedicated scv home (`~/.scv/codex-home`)": to keep
    `~/.codex/AGENTS.md` out, the cost was that every player had to log in again in that directory. The
    maintainer's own words: "the point is, don't make the user log in. Asking them to log in raises suspicion" ⇒
    the nature of what these tests pinned down is now reversed: scv must never invent a home, must never write it
    into config.json, must never create it in the state directory.

    What each of the deleted tests pinned, whether it's still wanted today, and who guarantees it now:
      - "the default value gets written into config.json / the upgrade path also persists it" — no longer wanted
        (reversed, now pinned by this test: not a single key gets written);
      - "the directory is actually created (codex cannot start against a home that does not exist)" — no longer
        wanted: the home is the player's, we must never create it for him; when he sets it wrong, codex's own
        original words still come through as-is (`TheUsersCodexHomeIsNeverCreated`);
      - "the directory gets chmod 0o700 (for auth.json to be installed there later)" — no longer wanted: scv no
        longer creates any directory meant to hold credentials;
      - "his own configured value must not be overridden by a default" — the nature of this stays, only the
        carrier changed: his CODEX_HOME environment variable now reaches the child process as-is (`ChildEnv`)."""

    def test_first_install_writes_no_codex_home_anywhere(self):
        cfg = scv.load_config()
        self.assertNotIn("codex_home", cfg)
        disk = json.loads((scv.state_dir() / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(disk["local_token"])                    # the ruler is not blind: what got read back really is the one just minted
        self.assertNotIn("codex_home", disk)
        self.assertNotIn("codex-home", os.listdir(self.home))
        with self.assertRaises(ValueError):                     # this name no longer exists in the state directory at all (there is a separate gate over the full WRITES set)
            scv.spath("codex-home")

    def test_an_old_installs_key_is_left_alone_and_not_used(self):
        """An old install's (Task 0c through 13) config.json has `codex_home` written in it — that was our own
        default value back then, never something he chose.
        ⇒ it is never read again (the child process cannot get it), we never rewrite his config.json for him
        either (what's on disk stays as-is), and we certainly never create that directory; doctor says one
        honest line about it when it sees this
        (tests/test_90_cli.py::DoctorCommand::test_an_old_codex_home_key_gets_one_honest_line)."""
        gone = os.path.join(self.home, "old-install", "codex-home")
        p = scv.spath("config.json")
        p.write_text(json.dumps({"local_token": "scv-old", "codex_home": gone}), encoding="utf-8")
        before = p.read_bytes()
        os.environ.pop("CODEX_HOME", None)
        log = self.own_log()
        found, _c = _cat(scv.load_config())
        self.assertEqual(found["codex"]["blocked"], "")         # precondition: the codex family's probe really ran
        rows = [r for r in helpers.read_fake_log(log) if r["family"] == "codex"]
        self.assertEqual({r["codex_home"] for r in rows}, {""}, rows)   # that old value was never passed to codex
        self.assertEqual(p.read_bytes(), before)
        self.assertFalse(os.path.exists(gone))


class CodexLoginGate(_StubEnv):
    """When codex is not logged in, never silently report this family as available — same shape as the
    `--safe-mode` test: refuse this family, and hand him the exact command to run.
    ⭐Since Task 13c that command is `codex login` (logging into exactly the home the bridge uses: his own), it
    no longer carries CODEX_HOME."""

    def test_no_credentials_blocks_the_family(self):
        os.environ["FAKE_MODE"] = "auth"
        found, cat = _cat()
        self.assertIn(" login", found["codex"]["blocked"])
        self.assertNotIn("CODEX_HOME", found["codex"]["blocked"])   # never ask him to log in inside an scv-dedicated home anymore
        self.assertEqual([m for m in cat if m.startswith("codex/")], [])
        self.assertIn("claude/haiku", cat)                      # refuse only this family, never implicate the other

    def test_logged_in_means_not_blocked(self):
        """Negative control: when credentials exist, blocked must be an empty string — without this half, an
        implementation that is always blocked would also be all green."""
        found, cat = _cat()
        self.assertEqual(found["codex"]["blocked"], "")
        self.assertIn("codex/gpt-6-luna", cat)

    def test_the_command_names_the_binary_we_actually_found(self):
        """🔴Measured on this machine: `codex` is not on PATH at all (scv found it through that
        `%LOCALAPPDATA%` glob) ⇒ printing a bare `codex login` for the user, he'd paste it and get "not
        recognized as an internal or external command". Since we already have the resolved path in hand, we
        must use it. ⚠️The command sits alone on its own line (he selects the whole line and pastes it)."""
        os.environ["FAKE_MODE"] = "auth"
        found, _c = _cat()
        want = scv.login_cmd("codex", helpers.fake_head("codex"))       # 13c review M4: the expected value is computed by the gate itself, never hand-assembled (the fixture happens to have no spaces)
        self.assertEqual([w for w in want.splitlines() if w not in found["codex"]["blocked"].splitlines()], [])
        exe, stub = (os.path.abspath(p).replace(os.sep, "/") for p in helpers.fake_head("codex")[:2])
        for line in (helpers.pasted_for(want, s) for s in helpers.PASTE_SHELLS):   # the resolved executable is really present in every line
            self.assertTrue(line is None or (exe in line and stub in line and line.endswith(" login")), want)

    def test_a_windows_path_without_spaces_is_one_line_with_forward_slashes(self):
        """13c review M3: a Windows path without spaces used to be printed on one line as-is — the backslash gets
        eaten inside Git Bash (`C:WindowsSystem32whoami.exe: command not found`, measured during review) ⇒
        switched to `/` (all three shells recognize it, measured on this machine). That the three shells really
        run it is pinned by tests/test_90_cli.py::PasteInRealShells (this test only pins the shape; the old name
        "every shell can paste it" claimed more than it verified)."""
        exe = chr(92).join(("C:", "Users", "小明", "AppData", "Local", "OpenAI", "Codex", "bin", "0a1b", "codex.exe"))
        with mock.patch.object(os, "name", "nt"):
            self.assertEqual(scv.login_cmd("codex", [exe]), "C:/Users/小明/AppData/Local/OpenAI/Codex/bin/0a1b/codex.exe login")
            # An npm-installed `.cmd`: `cli_head` gives `cmd /c <it>`, but the pasted line drops the shell
            # wrapper (Git Bash would rewrite `/c` into `C:/`)
            self.assertEqual(scv.login_cmd("codex", ["cmd", "/c", "C:" + chr(92) + "npm" + chr(92) + "codex.cmd"]),
                             "C:/npm/codex.cmd login")
        with mock.patch.object(os, "name", "posix"):
            self.assertEqual(scv.login_cmd("codex", ["/usr/bin/codex"]), "/usr/bin/codex login")
        self.assertEqual(scv.login_cmd("codex", None), "codex login")

    def test_a_path_with_spaces_gets_a_line_per_shell_on_windows(self):
        """⭐`os.name == "nt"` only tells you the OS, never the shell: a path with spaces needs double quotes in
        cmd / Git Bash, while in PowerShell a bare quoted path is just a string expression
        (`"C:/a b/codex.exe" login` is a parse error) — it has to be written as `& '…' login` ⇒ each one is
        marked clearly.
        ⚠️Both branches are run on this machine (by pinning `os.name` directly): a gate that only runs the branch
        for the current platform can never see a bug on the other side.
        13c review M4: one cell with Chinese, one with a single quote (never let "the fixture path happens to
        have no spaces" do the criterion's job)."""
        cases = {("Program Files", "codex.exe"): ("& 'C:/Program Files/codex.exe' login", '"C:/Program Files/codex.exe" login'),
                 ("用户 目录", "codex.exe"): ("& 'C:/用户 目录/codex.exe' login", '"C:/用户 目录/codex.exe" login'),
                 ("it's", "codex.exe"): ("& 'C:/it''s/codex.exe' login", '"C:/it' + "'" + 's/codex.exe" login')}
        for parts, (ps, other) in cases.items():
            with self.subTest(parts=parts), mock.patch.object(os, "name", "nt"):
                cmd = scv.login_cmd("codex", [chr(92).join(("C:",) + parts)])
                self.assertEqual(_shell_line(cmd, "PowerShell"), ps)
                self.assertEqual((_shell_line(cmd, "cmd"), _shell_line(cmd, "Git Bash")), (other, other))
        with mock.patch.object(os, "name", "posix"):
            self.assertEqual(scv.login_cmd("codex", ["/my dir/codex"]), "'/my dir/codex' login")
            self.assertEqual(scv.login_cmd("codex", ["/it's/codex"]), "'/it'" + '"' + "'" + '"' + "'s/codex' login")

    def test_it_says_credentials_not_usable(self):
        """⚠️`login status` only tells you "credentials exist locally", never "the credentials are still valid" ⇒
        the wording must never turn "credentials exist" into "usable"."""
        os.environ["FAKE_MODE"] = "auth"
        found, _c = _cat()
        self.assertIn("credentials", found["codex"]["blocked"])
        for overclaim in ("可用", "能用", "登录有效"):
            self.assertNotIn(overclaim, found["codex"]["blocked"])


class ProbesCarryThePlayersOwnHome(_StubEnv):
    """Q4: every probe `detect()` fires must use the home that will really be used (never probe one home and
    then actually run against another).
    ⭐Since Task 13c that home comes only from the player's own environment: use it as-is if he set it (an escape
    hatch, zero code), and add nothing at all if he did not."""

    def test_a_codex_home_the_player_set_reaches_every_probe(self):
        log = self.own_log()
        home = tempfile.mkdtemp(prefix="codex-home-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["CODEX_HOME"] = home
        _cat(scv.load_config())       # ⭐the production path: detect uses load_config()'s copy (before 13c it would override the one he set)
        rows = [r for r in helpers.read_fake_log(log) if r["family"] == "codex"]
        self.assertEqual([r["quick"] for r in rows], [["--version"], ["login", "status"]])
        self.assertEqual({r["codex_home"] for r in rows}, {home})

    def test_without_one_no_probe_is_handed_a_home_we_made_up(self):
        """Zero-input control: take away the knob under test (CODEX_HOME in his environment) and measure again —
        neither family's probe is ever allowed to get one out of thin air."""
        log = self.own_log()
        os.environ.pop("CODEX_HOME", None)
        _cat(scv.load_config())
        rows = helpers.read_fake_log(log)
        self.assertEqual(sorted({r["family"] for r in rows}), ["claude", "codex"])     # the ruler is not blind: both families were really asked
        self.assertEqual({r["codex_home"] for r in rows}, {""})


class ProbeFailuresDoNotLie(_StubEnv):
    """⭐Attribution must never lie: "could not tell" and "we asked and the answer is bad" are two different
    things, never merge them into one message.

    It used to be that `--help` threw ⇒ `helptext = ""` ⇒ `"--safe-mode" not in ""` was true ⇒ the user was told
    "this version does not recognize --safe-mode, please upgrade". His real problem might be a timeout /
    permissions / node crashed — sending him to upgrade a perfectly healthy CLI leaves him exactly where he
    started. The codex side has the same shape (cannot start ⇒ sent to login, which will not help either).
    A lying error is more expensive than a silent one."""

    GONE = [os.path.join(tempfile.gettempdir(), "scv-no-such-cli-9b31.exe")]

    def _detect_with(self, head, cfg=None):
        with mock.patch.object(scv, "cli_head", lambda family, c: head):
            return scv.detect(dict({"extra_models": {}}, **(cfg or {})))

    def test_claude_probe_that_cannot_run_says_so_and_does_not_blame_the_version(self):
        msg = self._detect_with(self.GONE)["claude"]["blocked"]
        self.assertIn("could not tell", msg)
        self.assertNotIn("please upgrade", msg)          # never send him to upgrade a perfectly healthy CLI
        self.assertRegex(msg, "WinError|FileNotFound|No such file|Errno")   # the OS's own words must be in there

    def test_claude_probe_that_answers_without_the_flag_does_blame_the_version(self):
        """Negative control: only when the answer really is "this version does not have it" should "please
        upgrade" appear. Without this half, the test above would also be all green on an implementation that
        always says it could not tell."""
        os.environ["FAKE_NO_SAFE_MODE"] = "1"
        msg = self._detect_with(helpers.fake_head("claude"))["claude"]["blocked"]
        self.assertIn("please upgrade", msg)
        self.assertNotIn("could not tell", msg)

    def test_codex_status_that_cannot_run_does_not_say_you_are_logged_out(self):
        msg = self._detect_with(self.GONE)["codex"]["blocked"]
        self.assertIn("could not tell", msg)
        self.assertNotIn("please run", msg)            # never send him to run a login that will not help
        self.assertRegex(msg, "WinError|FileNotFound|No such file|Errno")

    def test_codex_that_really_has_no_credentials_does_say_so(self):
        """The other half of the negative control: only when he really is not logged in should "no credentials +
        this command" appear."""
        os.environ["FAKE_MODE"] = "auth"
        msg = self._detect_with(helpers.fake_head("codex"))["codex"]["blocked"]
        self.assertIn("credentials", msg)
        self.assertIn("login", msg)
        self.assertNotIn("could not tell", msg)

    def test_every_failed_probe_leaves_a_line_in_the_log(self):
        """Global constraint: every failure path must leave one line in bridge.log. All three paths used to leave
        nothing at all."""
        with mock.patch.object(scv, "log") as spy:
            self._detect_with(self.GONE)
        said = " || ".join(c.args[0] for c in spy.call_args_list)
        for must in ("--version", "--help", "login-status"):
            self.assertIn(must, said)
        self.assertRegex(said, "WinError|FileNotFound|No such file|Errno")

    def test_a_probe_that_times_out_is_not_reported_as_an_old_cli_either(self):
        """⚠️The scene review pointed to was actually a timeout (Windows cold start plus antivirus scanning
        node's first run), not "the file isn't there". Both go through the same `except`, but verifying only the
        latter is the same as never having verified the former — and a timeout is exactly the kind most likely
        to really happen. `TimeoutExpired` is a subclass of `SubprocessError` ⇒ just construct it directly, no
        need to really wait 10 seconds (how long the wait takes is not what this test is about; stretching it
        out would only make the harness slower, not make the criterion more accurate)."""
        boom = subprocess.TimeoutExpired(["claude", "--help"], scv.PROBE_MAX_SECONDS)
        with mock.patch.object(scv, "run_cli", side_effect=boom):
            claude_msg = scv._claude_safe_mode_gate(["claude"])
            codex_msg = scv._codex_gate(["codex"])
        self.assertIn("could not tell", claude_msg)
        self.assertNotIn("please upgrade", claude_msg)
        self.assertIn("timed out", claude_msg)          # the original words carry through (B21), never a sentence we rewrote
        self.assertIn("could not tell", codex_msg)
        self.assertNotIn("please run", codex_msg)
        self.assertIn("timed out", codex_msg)

    def test_a_help_that_exits_nonzero_is_not_reported_as_an_old_cli(self):
        """🔴I-1's rc axis (the cell fix round 1 missed): `run_cli` has no `check=True` ⇒ when the CLI can start
        but rc≠0, it does not throw ⇒ execution never reaches the `except` that the previous round fixed,
        `--safe-mode` is not in the output ⇒ it still prints "please upgrade Claude Code".
        "throws an exception" and "rc≠0" are two cells of the same bug; the previous round only fixed the first
        one."""
        os.environ["FAKE_HELP_RC"] = "1"
        msg = self._detect_with(helpers.fake_head("claude"))["claude"]["blocked"]
        self.assertIn("could not tell", msg)
        self.assertNotIn("please upgrade", msg)          # never send him to upgrade a perfectly healthy CLI
        self.assertIn("rc=1", msg)               # the exit code must be spoken
        self.assertIn("throw err", msg)          # the CLI's own original words must be carried along too (B21)
        self.assertIn("Cannot find module 'yoga-wasm-web'", msg)   # ⭐the third line is where the real reason is
        # ⭐There is no legitimate multi-line content in this claude family's blocked (the kind a "pastable
        #   command" would produce) ⇒ it must be one complete sentence: if the CLI's three original lines are
        #   not folded, this sentence gets torn apart in the middle, and "do not report this family before we
        #   have an answer" ends up stranded alone on a fourth line. This is not fussiness — that is the text
        #   the user reads.
        self.assertEqual(msg.splitlines(), [msg], msg)

    def test_a_status_that_exits_nonzero_without_saying_so_is_not_called_logged_out(self):
        """The same cell for codex: rc≠0 is both how it expresses "not logged in" and what it looks like when it
        crashes ⇒ the criterion must never look at rc alone, it also has to check whether it really said that
        sentence. Never call a crash "you are not logged in" and then send him to run a login that will not
        help."""
        os.environ["FAKE_STATUS_BROKEN"] = "1"
        msg = self._detect_with(helpers.fake_head("codex"))["codex"]["blocked"]
        self.assertIn("could not tell", msg)
        self.assertNotIn("please run", msg)
        self.assertIn("config.toml", msg)                     # codex's own original words
        self.assertIn("expected '=' after key", msg)          # ⭐the second line must be there too (folded, never truncated)
        # This path has no legitimate multi-line content like a pastable command ⇒ likewise it must be one
        # complete sentence.
        self.assertEqual(msg.splitlines(), [msg], msg)

    def test_a_status_that_really_says_not_logged_in_still_gets_the_login_command(self):
        """The other half of the negative control: only when it really said "Not logged in" should "no
        credentials + this command" appear. Without this half, an implementation that always says it could not
        tell would also be all green."""
        os.environ["FAKE_MODE"] = "auth"
        msg = self._detect_with(helpers.fake_head("codex"))["codex"]["blocked"]
        self.assertIn("credentials", msg)
        self.assertIn("please run", msg)
        self.assertNotIn("could not tell", msg)

    def test_help_is_read_from_both_pipes(self):
        """Never read only stdout: on the codex side it has long been `stdout + stderr`. Some version prints help
        to stderr, and reading only one stream would trigger the same false refusal ("this version does not
        recognize --safe-mode")."""
        os.environ["FAKE_HELP_ON_STDERR"] = "1"
        found = self._detect_with(helpers.fake_head("claude"))
        self.assertEqual(found["claude"]["blocked"], "")


class ForeignTextIsFoldedAtTheBorder(_FreshHome):
    """⭐Foreign text (the CLI's stdout/stderr, an exception's `str(e)`) is folded into one line at the border
    where it enters, never truncated where it is output — truncation is exactly what cuts off the reason: the
    first line of a CLI's original words is often a useless stack header, and the sentence that actually says
    "how to fix it" comes later (in the stub it's the `Cannot find module` on line 3).
    "`bridge.log` has one record per line" and "the original words are kept completely, without changing a
    character (B21)" must both hold at the same time — never trade one for the other, and `splitlines()[0]` is
    exactly trading the latter for the former.

    ⚠️This test reads the real file, it does not look at what was passed into `mock.patch(scv.log)`: whether the
      formatting is broken is a property of `bridge.log` itself, while a spy can only see "what we intended to
      write", not "what it ended up looking like on disk"."""

    STACK = ("node:internal/modules/cjs/loader:1234", "throw err;", "Cannot find module 'yoga-wasm-web'")

    def _records(self):
        p = scv.spath("bridge.log")
        if not p.exists():
            return []
        return [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _assert_one_record_per_line(self, recs):
        self.assertTrue(recs, "the ruler is not blind: this path must really have written to the log")
        for x in recs:      # one record per line = every line starts with its own timestamp, never a continuation line
            self.assertRegex(x, "^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} ")

    def test_a_three_line_cli_message_stays_one_record_and_keeps_every_line(self):
        os.environ["FAKE_HELP_RC"] = "1"
        found, _c = _cat()
        recs = self._records()
        self._assert_one_record_per_line(recs)
        # ⚠️Pick the probe line specifically: `"--help" in x` is too loose, it would also catch the decision line
        #   ("--help exit code rc=1") ⇒ pick by "--help probe" here, so the count is a real anchor.
        probe = [x for x in recs if "--help probe" in x]
        self.assertEqual(len(probe), 1, recs)
        for must in self.STACK:                 # ⭐assert the correct value: not one of the three original lines may be lost
            self.assertIn(must, probe[0], probe[0])
            self.assertIn(must, found["claude"]["blocked"])

    def test_the_decision_record_keeps_the_reason_too(self):
        """The decision line used to go through `splitlines()[0]` ⇒ what it kept was the "could not tell..."
        opening, and what it dropped was what the CLI actually said. Once folded into one line, both are
        there."""
        os.environ["FAKE_HELP_RC"] = "1"
        _cat()
        decision = [x for x in self._records() if "refusing to report" in x]
        self.assertEqual(len(decision), 1, self._records())
        self.assertIn("claude", decision[0])
        self.assertIn("Cannot find module 'yoga-wasm-web'", decision[0])

    def test_the_decision_record_keeps_the_pastable_command_too(self):
        """codex's blocked is legitimately multi-line (the command starts its own line, he selects the whole line
        and pastes it) ⇒ if the decision line were truncated, `bridge.log` would only have "no credentials" left
        in it — with no way to see what we actually told him to run — and that is exactly what needs to be
        checked after the fact.

        ⚠️This test was forced into existence by NC-50: once folding happens at the border, `splitlines()[0]`
        becomes a no-op at that point (blocked was already down to one line), so the original test could no
        longer tell "folded" apart from "truncated" ⇒ a test that cannot tell the difference is not that gate.
        This one is.
        ⚠️Task 13c: the command no longer carries CODEX_HOME (⇒ when the path has no spaces there is only one
          line, and the old assertion of "two lines labeled cmd/PowerShell" is gone) ⇒ assert that the command
          itself is on the second line, and inside the decision line."""
        os.environ["FAKE_MODE"] = "auth"
        found, _c = _cat(scv.load_config())      # ⭐the production path (before 13c this was two shell-split lines with CODEX_HOME)
        want = scv.login_cmd("codex", helpers.fake_head("codex")).splitlines()     # 13c review M4: computed by the gate, never hand-assembled
        self.assertEqual(found["codex"]["blocked"].splitlines()[1:], want)   # precondition: it really is multi-line (otherwise folded and truncated can't be told apart)
        decision = [x for x in self._records() if "refusing to report" in x]
        self.assertEqual(len(decision), 1, self._records())
        self.assertIn("credentials", decision[0])
        for line in want:
            self.assertIn(line, decision[0])    # ⭐it must be possible to look up exactly which command we handed him

    def test_the_codex_cannot_tell_path_leaves_a_line_too(self):
        """The codex "could not tell" path used to have no fallback log line at all."""
        os.environ["FAKE_STATUS_BROKEN"] = "1"
        _cat()
        recs = self._records()
        self._assert_one_record_per_line(recs)
        self.assertTrue([x for x in recs if "config.toml" in x], recs)

    def test_log_itself_will_not_write_a_multi_line_record(self):
        """The fallback gate is built on the single exit point: `log()` is the only place in the whole file that
        writes to `bridge.log` ⇒ folding it here means whoever stuffs a multi-line piece of text in later cannot
        escape it. Never fold only at today's few entry points — the next call to `log()` is the next cell."""
        scv.log("第一行" + chr(10) + "第二行" + chr(13) + chr(10) + "第三行")
        recs = self._records()
        self.assertEqual(len(recs), 1, recs)
        for must in ("第一行", "第二行", "第三行"):
            self.assertIn(must, recs[0])

    def test_a_single_line_message_is_left_alone(self):
        """Zero-input control: something that was already one line must never be touched (otherwise "folding" and
        "mangling the original words" cannot be told apart)."""
        scv.log("本来就一行")
        self.assertEqual(self._records()[0].split(" ", 2)[2], "本来就一行")


class BlockingAFamilyIsWrittenDown(_StubEnv):
    """⭐These two tests are about the decision path, never the failure path: the whole family disappears from
    `/v1/models`, while not a single character lands in `bridge.log` ⇒ when the user comes asking "where did my
    claude go", there is nothing on disk to look up.
    The three `log()` calls the previous round added record "the probe died"; what's needed here is "and so the
    whole family is not reported" — two different things, and the latter is the one he needs to look up."""

    def _decisions(self, fn):
        with mock.patch.object(scv, "log") as spy:
            fn()
        return [c.args[0] for c in spy.call_args_list if "refusing to report" in c.args[0]]

    def test_the_old_cli_decision_lands_in_the_log(self):
        os.environ["FAKE_NO_SAFE_MODE"] = "1"
        said = self._decisions(_cat)
        self.assertEqual(len(said), 1, said)
        self.assertIn("claude", said[0])
        self.assertIn("--safe-mode", said[0])

    def test_the_no_credentials_decision_lands_in_the_log(self):
        os.environ["FAKE_MODE"] = "auth"
        said = self._decisions(lambda: _cat())
        self.assertEqual(len(said), 1, said)
        self.assertIn("codex", said[0])
        self.assertIn("credentials", said[0])

    def test_a_line_is_one_line(self):
        """A multi-line blocked must never be dumped into the log as-is: codex's carries two lines of a pastable
        command, and dumping it in would break "one record per line" (⇒ no one could read this file line by line
        anymore)."""
        os.environ["FAKE_MODE"] = "auth"
        said = self._decisions(lambda: _cat())
        self.assertEqual(said[0].splitlines(), [said[0]])

    def test_a_family_with_no_binary_on_this_machine_is_written_down_too(self):
        """🔴The third cell: `cli_head` returns None ⇒ this family never even makes it into `found`, so there is
        no blocked to speak of ⇒ the log built on "blocked is non-empty" is naturally blind to it.
        Yet what the user sees is identical to the other two cells: this family is missing from `/v1/models`,
        while there is nothing on disk to look up.

        ⭐I built the logging point at the `detect()` layer precisely so that "one more reason to refuse, added
        later, is automatically recorded too" — the fact that this cell was not recorded shows that this point
        missed this path (it `continue`s out further up in the `for` loop)."""
        with mock.patch.object(scv, "cli_head", lambda family, cfg: None):
            said = self._decisions(lambda: scv.detect({"extra_models": {}}))
        self.assertEqual(len(said), 2, said)                    # one for each family
        self.assertTrue(any("claude" in x for x in said), said)
        self.assertTrue(any("codex" in x for x in said), said)
        for x in said:
            self.assertIn("no executable for it", x)                          # spell out which kind of disappearance it is

    def test_a_family_that_is_not_blocked_says_nothing(self):
        """Zero-input control: take away the thing under test (refusal) and measure again.
        Without this line, an implementation that logs a line on every single detect() call would also be all
        green — and that kind of gate gets shouted into background noise by our own people."""
        self.assertEqual(self._decisions(lambda: _cat()), [])


class TheUsersCodexHomeIsNeverCreated(_StubEnv):
    """Before Task 13c this test was "wherever CODEX_HOME is, it must really be created": the home belonged to
    scv (`~/.scv/codex-home`, or whatever was configured in config.json), codex could not start against a home
    that did not exist, and it could not even run the login command we handed him ⇒ so the bridge created it for
    him.
    ⭐Since 13c the home is the player's own (the CODEX_HOME in his environment, or codex's own default
      `~/.codex`) ⇒ the nature of this is reversed: the bridge must never create it for him (that would be
      writing into his own space); when he sets it wrong, only codex's own original words (B21) may be carried
      out, never phrased as "you are not logged in".
    Where the nature of the three deleted tests ("it gets created", "if it cannot be created, say so",
    "if it cannot be created, log it") went: the first two are now pinned in reverse by this test; "log it" is
    handled by `ProbeFailuresDoNotLie` / `BlockingAFamilyIsWrittenDown` (codex cannot tell its login state ⇒ one
    line gets logged)."""

    def test_a_home_that_does_not_exist_is_reported_in_codexs_words_and_left_alone(self):
        root = tempfile.mkdtemp(prefix="codex-cfg-")
        self.addCleanup(shutil.rmtree, root, True)
        home = os.path.join(root, "not", "there", "yet")
        os.environ["CODEX_HOME"] = home
        found, cat = _cat(scv.load_config())      # ⭐the production path (before 13c load_config would stuff in scv's own home)
        msg = found["codex"]["blocked"]
        self.assertIn("CODEX_HOME points to", msg)             # codex's own original words (the stub matches the real codex's measured wording)
        self.assertIn("could not tell", msg)
        self.assertNotIn("please run", msg)                          # never send him off to run a login that will not help
        self.assertFalse(os.path.exists(os.path.join(root, "not")))   # ⭐not even one directory level was created for him
        self.assertEqual([m for m in cat if m.startswith("codex/")], [])

    def test_a_home_that_exists_is_used_as_is(self):
        """Negative control: same stub, same path, but when the home exists it must still be let through as
        normal — otherwise the test above would also be all green on an implementation that refuses the moment
        CODEX_HOME is set at all."""
        home = tempfile.mkdtemp(prefix="codex-cfg-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["CODEX_HOME"] = home
        found, _c = _cat(scv.load_config())
        self.assertEqual(found["codex"]["blocked"], "")


class AuthStatusLive(_StubEnv):
    """Zero-quota login status. ⚠️It only tells you "credentials exist locally", never "the credentials are still
    valid" — necessary but not sufficient."""

    def test_claude_reads_the_json_and_keeps_only_the_whitelisted_fields(self):
        """⭐`probe_error` is a cell that is always present, never "a key that only shows up when something goes
        wrong": the consumer `detect()` relies on it to tell "could not ask" apart from "we asked, and the
        answer is bad" — a key that is sometimes there and sometimes not can only be `.get()`'d, and what
        `.get()` reads for its absence looks identical to "we asked, and nothing went wrong"."""
        st = scv.auth_status("claude", helpers.fake_head("claude"))
        self.assertEqual(st, {"logged_in": True, "method": "claude.ai", "plan": "max", "probe_error": ""})

    def test_claude_not_logged_in(self):
        os.environ["FAKE_MODE"] = "auth"
        st = scv.auth_status("claude", helpers.fake_head("claude"))
        self.assertEqual(st["logged_in"], False)

    def test_codex_reads_the_text_line_and_the_exit_code(self):
        st = scv.auth_status("codex", helpers.fake_head("codex"))
        self.assertEqual(st, {"logged_in": True, "method": "Logged in using ChatGPT", "plan": "",
                              "probe_error": ""})

    def test_codex_not_logged_in(self):
        os.environ["FAKE_MODE"] = "auth"
        st = scv.auth_status("codex", helpers.fake_head("codex"))
        self.assertEqual(st, {"logged_in": False, "method": "Not logged in", "plan": "",
                              "probe_error": ""})       # ⭐we really did ask and get "not logged in" ⇒ this cell must be empty

    def test_codex_probe_carries_the_players_own_home(self):
        """When asking about login status, the home asked about must be the one really used for work (never let
        the probe ask about one home while the real work happens in another: that way the probe says "logged in"
        while the real path, in a different home, is not). Since Task 13c that home comes only from his own
        environment ⇒ whatever he set, the probe carries the exact same thing."""
        log = self.own_log()
        home = tempfile.mkdtemp(prefix="codex-home-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["CODEX_HOME"] = home
        scv.auth_status("codex", helpers.fake_head("codex"))
        rows = helpers.read_fake_log(log)
        self.assertEqual(len(rows), 1)                           # the ruler is not blind: the probe really did run
        self.assertEqual(rows[0]["codex_home"], home)

    def test_without_one_the_probe_is_given_none(self):
        """Zero-input control: take away the knob under test (CODEX_HOME in his environment) and measure again,
        otherwise the test above cannot tell "the stub recorded the one he set" from "the stub recorded something
        or other"."""
        log = self.own_log()
        os.environ.pop("CODEX_HOME", None)
        scv.auth_status("codex", helpers.fake_head("codex"))
        self.assertEqual(helpers.read_fake_log(log)[0]["codex_home"], "")

    def test_each_family_is_asked_in_its_own_dialect(self):
        """⭐The two families are asked differently (claude with a JSON subcommand, codex with a text line + exit
        code) ⇒ assert directly on the string that got sent out, never rely on "an error would be reported if we
        asked it wrong" — the real claude has no such failure mode at all (measured: an argv it does not
        recognize gets sent as a real turn, as if it were a prompt)."""
        log = self.own_log()
        scv.auth_status("claude", helpers.fake_head("claude"))
        scv.auth_status("codex", helpers.fake_head("codex"))
        rows = helpers.read_fake_log(log)
        self.assertEqual([(r["family"], r["quick"]) for r in rows],
                         [("claude", ["auth", "status", "--json"]), ("codex", ["login", "status"])])

    def test_the_status_line_is_picked_by_content_not_by_position(self):
        """M-8: it used to take `lines[-1]` and only filter out lines starting with `WARNING` ⇒ if some other line
        of noise came after the status line (codex really does print `WARNING: proceeding, …`, and any other
        prefix slips through), `method` would turn into an unrelated sentence.
        The criterion was changed to "take the line that contains Logged in / Not logged in", and if there is no
        such line, keep the whole thing."""
        self.assertEqual(scv._codex_auth_from_text("noise" + chr(10) + "Logged in using ChatGPT"
                                                   + chr(10) + "some trailing note", 0),
                         {"logged_in": True, "method": "Logged in using ChatGPT", "plan": ""})
        self.assertEqual(scv._codex_auth_from_text("Not logged in" + chr(10) + "tail", 1)["method"],
                         "Not logged in")

    def test_when_no_line_looks_like_a_status_we_keep_the_whole_thing(self):
        """Never throw away the truth for the sake of "picking a line": when not a single character is
        recognizable, keep the whole original text (B21: an error is the original words)."""
        st = scv._codex_auth_from_text("完全看不懂的一段" + chr(10) + "另一行", 1)
        self.assertIn("完全看不懂的一段", st["method"])
        self.assertIn("另一行", st["method"])

    def test_a_status_command_that_will_not_start_shouts_the_os_message(self):
        gone = [os.path.join(tempfile.gettempdir(), "scv-no-such-cli-9b31.exe")]
        st = scv.auth_status("claude", gone)
        self.assertEqual(st["logged_in"], False)
        self.assertIn("never ran", st["method"])
        # ⭐`logged_in=False` in this cell is false (we never actually got an answer) ⇒ there must be another
        #   cell that carries the truth out, otherwise the caller can only read "could not tell" as "he is not
        #   logged in", and then send him to run a login that will not help.
        self.assertRegex(st["probe_error"], "WinError|FileNotFound|No such file|Errno")


class AuthWhitelistShape(unittest.TestCase):
    """⭐Feed it the shape measured on a real CLI (2026-09-21, on this machine, `claude auth status --json`):
    four more fields than the brief's version (apiProvider/analyticsDisabled/projectsDirectory/configDirectory)
    — exactly the ones that would leak out if the whitelist missed even one."""

    REAL = ('{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", '
            '"analyticsDisabled": false, "projectsDirectory": "C:/Users/someone/.claude/projects", '
            '"configDirectory": "C:/Users/someone/.claude", "email": "someone@example.com", '
            '"orgId": "f913d065-7b93-46c1-b54b-64f325415fcb", '
            '"orgName": "someone@example.com Organization", "subscriptionType": "max"}')

    def test_only_three_keys_survive(self):
        st = scv._claude_auth_from_json(self.REAL)
        self.assertEqual(sorted(st), ["logged_in", "method", "plan"])

    def test_no_identifying_value_survives_anywhere(self):
        """Beyond asserting the correct value, there's another question to ask: "could it leak out through some
        other key?" Stringify the whole result and search it for those values."""
        blob = repr(scv._claude_auth_from_json(self.REAL))
        for leak in ("someone@example.com", "f913d065", "Users/someone", "firstParty"):
            self.assertNotIn(leak, blob)

    def test_leading_noise_before_the_brace_is_skipped(self):
        st = scv._claude_auth_from_json("Some npm warning line" + chr(10) + self.REAL)
        self.assertEqual(st["plan"], "max")

    def test_garbage_is_cannot_tell_not_logged_out(self):
        """🔴F-1 (Task 12 changed the contract): it used to return `logged_in=False` when it could not read the
        answer — that turns "could not tell" into "you are not logged in", and when claude does not recognize
        `auth status` what it returns is exactly an answer that cannot be read (it sent the argument as a real
        call, as if it were a prompt).
        ⇒ Now it returns `None`, which `auth_status` folds into `probe_error` and shouts out loud
        (tests/test_90_cli.py::ClaudeAuthProbe)."""
        for raw in ("", "totally not json", "{", "[]", '{"authMethod": "claude.ai"}'):
            self.assertIsNone(scv._claude_auth_from_json(raw), raw)

    def test_logged_out_json(self):
        st = scv._claude_auth_from_json('{"loggedIn": false}')
        self.assertEqual(st, {"logged_in": False, "method": "", "plan": ""})


class CodexArgvPairs(unittest.TestCase):
    """⭐`assertIn('model="x"', a)` can miss a real failure: even if every `-c` were deleted, or the order got
    scrambled, it would still be green. The real criterion is "the cell right before every setting must be
    -c"."""

    @staticmethod
    def _settings(a):
        return [a[i + 1] for i, x in enumerate(a[:-1]) if x == "-c"]

    def test_every_setting_is_introduced_by_dash_c(self):
        a = scv.codex_argv(["codex"], "gpt-6-luna", "high")
        settings = self._settings(a)
        self.assertIn('model="gpt-6-luna"', settings)
        self.assertIn('model_reasoning_effort="high"', settings)
        self.assertIn('web_search="disabled"', settings)
        # Task 13c: the entries layered on top of the player's own config.toml — the expected value is read only
        # from `PLAYER_OVERRIDES` (the criterion must live in exactly one place, 13c review M8)
        for want in PLAYER_OVERRIDES:
            self.assertIn(want, settings)
        # everything left is a feature being turned off, not one missing and not one extra
        self.assertEqual(sorted(x for x in settings if x.startswith("features.")),
                         sorted("features." + n + "=false" for n in scv.CODEX_OFF))
        # the first four cells are head+app-server+--listen+stdio://, and the rest must come in tidy pairs:
        # missing even one -c and this goes red
        self.assertEqual(len(settings) * 2, len(a) - 4)

    def test_shell_tool_is_off(self):
        """B11: this tool is always off. Pin it on its own, never rely on the CODEX_OFF list happening to still
        carry it."""
        self.assertIn("shell_tool", scv.CODEX_OFF)
        self.assertIn("features.shell_tool=false", self._settings(scv.codex_argv(["c"], "gpt-6-luna", "low")))

    def test_effort_defaults_to_low_and_is_closed(self):
        for empty in (None, ""):               # both kinds of "not said" fall through to the default tier
            self.assertIn('model_reasoning_effort="low"',
                          self._settings(scv.codex_argv(["c"], "gpt-6-luna", empty)))
        for bad in ("LOW", "low; rm -rf /", "ultra", "low medium"):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.codex_argv(["c"], "gpt-6-luna", bad)
            self.assertEqual(cm.exception.klass, "bad_request")

    def test_head_is_copied_not_aliased(self):
        head = ["codex"]
        scv.codex_argv(head, "gpt-6-luna", "low")
        scv.claude_argv(head, "haiku", "S", "I")
        self.assertEqual(head, ["codex"])      # the caller's own copy must never be modified in place


class ModelIsClosedAtTheAssemblyPoint(unittest.TestCase):
    """I-4: what B27 needs pinned is "not a single character from the remote side can get into the command
    line", and that guarantee has to live at the one and only assembly point.

    `effort` used to be guarded by `_closed()`, but `model` ran bare all the way to `--model <m>` and
    `-c model="<m>"` — closure depended entirely on the caller (`resolve_model`). If Task 5 gave even one path
    (a retry / a default fallback / doctor's self-check) that bypassed it and assembled argv directly, closure
    would be gone, and not a single test would go red. Defense in depth is built at the assembly point.
    ⚠️The criterion is `MODEL_RE`, never `CLAUDE_MODELS` — `extra_models` is a legitimate source."""

    BAD = ("haiku --dangerously-skip-permissions", "haiku; calc", 'so"nnet', "../x", "haiku model",
           "", "-haiku", "x" * 65, "haiku" + chr(10), None, 7)
    # ⚠️The `"haiku" + NL` cell is deliberate: `re.match`'s `$` matches "right before that trailing newline" ⇒
    #   `MODEL_RE.match("haiku" + NL)` really is true, only `fullmatch` closes it off. Same regex, two different
    #   readings — the only difference is whether one extra byte at the end can sneak into argv, and that is
    #   exactly the kind of crack B27 needs pinned.

    def test_claude_argv_refuses_anything_outside_the_shape(self):
        for bad in self.BAD:
            with self.assertRaises(scv.BridgeError, msg=repr(bad)) as cm:
                scv.claude_argv(HEAD, bad, "S", "I")
            self.assertEqual(cm.exception.klass, "bad_request")

    def test_codex_argv_refuses_anything_outside_the_shape(self):
        for bad in self.BAD:
            with self.assertRaises(scv.BridgeError, msg=repr(bad)) as cm:
                scv.codex_argv(["codex"], bad, "low")
            self.assertEqual(cm.exception.klass, "bad_request")

    def test_every_name_the_bridge_can_legitimately_advertise_still_passes(self):
        """Negative control: never fix the gate into "only recognize the three built-in ones" — anything in
        `extra_models` that passes `MODEL_RE` is a legitimate source."""
        for m in list(scv.CLAUDE_MODELS) + ["fable", "claude-opus-4-5-20260101", "a" * 64]:
            self.assertEqual(scv.claude_argv(HEAD, m, "S", "I")[3], m)
        for m in list(scv.CODEX_MODELS) + ["gpt-6", "o4-mini"]:
            self.assertIn('model="' + m + '"', scv.codex_argv(["codex"], m, "low"))

    def test_the_catalog_never_advertises_a_name_the_builders_would_refuse(self):
        """⭐Both ends of the closed set must be the same ruler: the reporting end (`catalog`) lets something
        through while the assembly end refuses it ⇒ a user sees a model in `/v1/models`, clicks it, and gets
        `bad_request`. The silent error sits on the reporting side.
        ⚠️This test only has teeth once the assembly point really does refuse (before that it is a hollow
        green) ⇒ see NC-31's negative control in the report."""
        cfg = {"extra_models": {"claude": ["haiku" + chr(10)], "codex": ["gpt-x" + chr(10)]}}
        found = {"claude": {"blocked": ""}, "codex": {"blocked": ""}}
        cat = scv.catalog(cfg, found)
        self.assertGreaterEqual(len(cat), 6)                  # the ruler is not blind: the built-in ones really were reported
        for mid in cat:
            family, model = scv.resolve_model(mid, cat)
            if family == "claude":
                scv.claude_argv(HEAD, model, "S", "I")
            else:
                scv.codex_argv(["codex"], model, "low")


class ClaudeArgvPairs(unittest.TestCase):
    """The brief's test only asks "is the flag present". ⭐Position and count are two other axes: if `--model`
    were followed by the wrong thing, or some flag got added twice, it would still be all green."""

    def test_the_head_stays_in_front_and_the_model_follows_its_flag(self):
        a = scv.claude_argv(["cmd", "/c", "claude.CMD"], "sonnet", "S", "I")
        self.assertEqual(a[:3], ["cmd", "/c", "claude.CMD"])
        self.assertEqual(a[a.index("--model") + 1], "sonnet")

    def test_no_flag_is_passed_twice(self):
        a = scv.claude_argv(HEAD, "haiku", "S", "I", effort="high")
        flags = [x for x in a if x.startswith("--")]
        self.assertEqual(sorted(flags), sorted(set(flags)))

    def test_path_objects_are_stringified(self):
        """In a real call, the prompt file / settings file are Path objects ⇒ never hand a `PosixPath('...')` to
        subprocess."""
        from pathlib import Path
        a = scv.claude_argv(HEAD, "haiku", Path("a") / "sys.txt", Path("b") / "settings.json")
        for i in (a.index("--system-prompt-file") + 1, a.index("--settings") + 1):
            self.assertIsInstance(a[i], str)
            self.assertNotIn("Path", a[i])

    def test_every_element_is_a_string(self):
        a = scv.claude_argv(HEAD, "haiku", "S", "I", effort="low")
        self.assertEqual([x for x in a if not isinstance(x, str)], [])


class ChildEnv(unittest.TestCase):
    """The child process's environment = our own environment with the session-bound families stripped out (15c):
    everything else — not one item added, not one item removed, not a single value changed — is identical between
    the two families.
    ⭐The player's own CODEX_HOME reaching the child process as-is is the escape hatch for "someone who wants
      isolation", zero code; when he has not set it we never invent one (the premise of no-login is that codex
      uses exactly the home he is normally already logged into). B22: never set ANTHROPIC_BASE_URL, never
      impersonate a client — whatever he set himself is inherited as-is, that's his business;
      🔴while the variables of the agent session that started the bridge (session id, message pipe, entrypoint,
      originator...) are never his setting, they are someone else's session's identity (15c).
    ⚠️Lessons from the 2026-09-23 census (the `assertLessEqual` family) are kept here: (1) the subtraction must
      never take the environment after the call and subtract from that (an implementation that "quietly writes
      into the process's own environment then returns it as-is" would make the difference set permanently empty)
      ⇒ here we assert the whole set is equal directly, and separately pin "the process's own environment was
      never touched"; (2) the baseline is the smallest clean environment (`clear=True`), never "the current
      environment minus some key": if an earlier test case had leaked a key into the process environment, the
      ruler would have already been blinded by whoever ran before it in the same process.
    ⭐The fixture is split into two halves: the fact (which names have shown up in real agent sessions) comes from
      `tests/agent_session_env.py` (exported from a real environment, never hand-copied); the decision (which
      ones should be stripped) is written in `SESSION` below (the maintainer's ruling, 15c item 7-1) — never work
      it out backward from `scv`: that way, if the rule gets broken, the ruler breaks right along with it."""

    BASE_KEYS = ("PATH", "SystemRoot", "SCV_HOME")
    # The names to strip out of what a real environment exports: the session identity both agent families set for
    # a child process they start (all the values are fake)
    SESSION = {"AI_AGENT", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
               "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
               "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_PID", "CODEX_CI", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
               "CODEX_VERSION"}
    # Families with only static grounds (the session-prefix family Claude Code itself defines for its eval sandbox
    # + what the package documents setting for a child process) — never seen on this machine, stripped per ruling
    STATIC = {"TRACEPARENT", "TRACESTATE", "CLAUDE_CODE_INVOKED_SKILLS", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CLAUDE_CODE_HOST_CREDS_FILE",
              "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE", "CLAUDE_CODE_SDK_HOST", "CLAUDE_CODE_RELAUNCH_X",
              "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_BG_ISOLATION", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
              "CODEX_NETWORK_PROXY_ACTIVE"}
    # The control arm: user-level configuration, billing shape, user settings that change behavior (class B) —
    # never strip these even when they share a prefix (this is exactly where a broad `CLAUDE_CODE_*` prefix trips up)
    USER = ("CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "CLAUDE_CODE_EFFORT_LEVEL",
            "CLAUDE_CODE_DISABLE_THINKING", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_TMPDIR")

    def _agent_env(self, *names):
        from tests import agent_session_env as real
        seen = set(real.CLAUDE_CODE_SESSION) | set(real.CODEX_SHELL_ADDED) | set(names)
        return self._base(**{n: "fake-" + n.lower() for n in seen}), seen

    def test_an_agent_sessions_own_variables_never_reach_the_child(self):
        """The two sets of names exported from a real environment (Claude Code session + what codex's shell tool
        adds): strip out the ones in `SESSION`, and everything else must be equal in full (including
        `CLAUDE_CODE_USE_POWERSHELL_TOOL`, which shares the prefix but is something the player configured
        himself). ⭐Pin the precondition first: the fixture really does contain every one of `SESSION` (never let
        this run empty)."""
        env, seen = self._agent_env(*self.USER)
        self.assertLessEqual(self.SESSION, seen, "the fixture is missing a name that needs testing ⇒ this test is running empty")
        with mock.patch.dict(os.environ, env, clear=True):
            before = dict(os.environ)
            got = scv.child_env()
            self.assertEqual(got, {k: v for k, v in before.items() if k not in self.SESSION})   # ⭐the correct value: everything outside the stripped list is equal in full
            self.assertEqual(dict(os.environ), before, "child_env modified the process's own environment")

    def test_the_static_only_families_are_stripped_too(self):
        """The families with only static grounds (never seen on this machine): strip them the same way the
        maintainer ruled; the user-level arm still comes through in full."""
        with mock.patch.dict(os.environ, self._base(**{n: "x" for n in self.STATIC | set(self.USER)}), clear=True):
            before = dict(os.environ)
            self.assertEqual(scv.child_env(), {k: v for k, v in before.items() if k not in self.STATIC})

    def test_user_level_settings_reach_the_child_exactly_as_he_set_them(self):
        """The control arm, pulled out on its own: user-level configuration / billing shape / class B (things
        that change behavior, but they're his own setting ⇒ keep them, let doctor disclose them) — not one
        missing, and not a single character of any value changed."""
        mine = {n: "his-own-" + n.lower() for n in self.USER}
        with mock.patch.dict(os.environ, self._base(**mine), clear=True):
            got = scv.child_env()
        self.assertEqual({k: got.get(k) for k in mine}, mine)

    def _base(self, **more):
        return dict({k: os.environ[k] for k in self.BASE_KEYS if k in os.environ}, **more)

    def test_the_players_codex_home_reaches_the_child_exactly_as_he_set_it(self):
        mine = self._base(CODEX_HOME="/players/own/codex-home", HTTPS_PROXY="http://proxy.example:3128")
        with mock.patch.dict(os.environ, mine, clear=True):
            before = dict(os.environ)       # ⚠️on win32 the keys of `os.environ` are uppercased (SystemRoot → SYSTEMROOT) ⇒ use it itself as the baseline
            self.assertEqual(before["CODEX_HOME"], "/players/own/codex-home")      # precondition: the one he set really is in the environment
            env = scv.child_env()
            self.assertEqual(env, before)                        # ⭐the correct value: the whole thing, as-is (including his CODEX_HOME, the proxy)
            self.assertEqual(env["CODEX_HOME"], "/players/own/codex-home")
            self.assertEqual(dict(os.environ), before, "child_env modified the process's own environment")
            env["SCV_PROBE_ONLY"] = "1"
            self.assertNotIn("SCV_PROBE_ONLY", os.environ)       # modifying the return value must never touch the process's own environment

    def test_without_one_no_codex_home_is_invented(self):
        """Zero-input control: take away the knob under test (the CODEX_HOME he set) and measure again — never
        allowed to conjure one out of thin air (before 13c this would stuff in scv's own dedicated home, exactly
        the source of "having to log in a second time")."""
        with mock.patch.dict(os.environ, self._base(), clear=True):
            before = dict(os.environ)
            self.assertNotIn("CODEX_HOME", before)
            self.assertEqual(scv.child_env(), before)


class CatalogEdges(unittest.TestCase):
    FOUND = {"claude": {"head": HEAD, "version": "x", "blocked": ""},
             "codex": {"head": ["codex"], "version": "x", "blocked": ""}}

    def test_both_families_full_table(self):
        cat = scv.catalog({"extra_models": {}}, self.FOUND)
        self.assertEqual(cat, ["claude/" + m for m in scv.CLAUDE_MODELS] +
                              ["codex/" + m for m in scv.CODEX_MODELS])

    def test_a_slash_can_never_enter_a_model_name(self):
        """⭐This is a real injection surface: a name in `extra_models` carrying a `/` would make resolve_model's
        partition split in the wrong place, conjuring up an extra "family" out of nowhere. MODEL_RE does not
        accept `/`; this pins that down."""
        cfg = {"extra_models": {"claude": ["codex/gpt-6-sol", "a/b", "..", "../../etc/passwd"]}}
        cat = scv.catalog(cfg, self.FOUND)
        self.assertEqual([m for m in cat if m.count("/") != 1], [])
        self.assertNotIn("claude/codex/gpt-6-sol", cat)

    def test_non_strings_are_dropped_not_crashed(self):
        cfg = {"extra_models": {"claude": [None, 7, {"a": 1}, ["x"], "fable"]}}
        self.assertEqual([m for m in scv.catalog(cfg, self.FOUND) if m.startswith("claude/")],
                         ["claude/" + m for m in scv.CLAUDE_MODELS] + ["claude/fable"])

    def test_name_length_boundary(self):
        ok, too_long = "a" * 64, "a" * 65
        cfg = {"extra_models": {"claude": [ok, too_long]}}
        cat = scv.catalog(cfg, self.FOUND)
        self.assertIn("claude/" + ok, cat)
        self.assertNotIn("claude/" + too_long, cat)

    def test_leading_punctuation_is_refused(self):
        cfg = {"extra_models": {"claude": [".hidden", "-dash", "_under"]}}
        self.assertEqual([m for m in scv.catalog(cfg, self.FOUND) if m.startswith("claude/.")
                          or m.startswith("claude/-") or m.startswith("claude/_")], [])

    def test_duplicates_collapse(self):
        cfg = {"extra_models": {"claude": ["haiku", "haiku"]}}
        cat = scv.catalog(cfg, self.FOUND)
        self.assertEqual(cat.count("claude/haiku"), 1)

    def test_missing_extra_models_key_is_fine(self):
        """Never use "compute both sides and check they're equal" as the assertion — if both sides are wrong the
        same way, it would still be green. Assert the correct value."""
        base = ["claude/" + m for m in scv.CLAUDE_MODELS] + ["codex/" + m for m in scv.CODEX_MODELS]
        for cfg in ({}, {"extra_models": None}, {"extra_models": {}}, {"extra_models": {"claude": None}}):
            self.assertEqual(scv.catalog(cfg, self.FOUND), base, cfg)


class ResolveRoundTrip(unittest.TestCase):
    def test_everything_the_bridge_advertises_resolves_back_to_itself(self):
        cat = scv.catalog({"extra_models": {"claude": ["fable"]}}, CatalogEdges.FOUND)
        for mid in cat:
            family, model = scv.resolve_model(mid, cat)
            self.assertEqual(family + "/" + model, mid)
            self.assertIn(family, ("claude", "codex"))

    def test_a_name_that_is_only_a_prefix_is_refused(self):
        cat = ["claude/haiku"]
        for bad in ("claude/haiku ", " claude/haiku", "claude/haik", "CLAUDE/HAIKU", "claude/haiku/x"):
            with self.assertRaises(scv.BridgeError) as cm:
                scv.resolve_model(bad, cat)
            self.assertEqual(cm.exception.klass, "bad_request")

    def test_the_refusal_says_where_to_look(self):
        with self.assertRaises(scv.BridgeError) as cm:
            scv.resolve_model("claude/opus", ["claude/haiku"])
        self.assertIn("/v1/models", cm.exception.raw)
        self.assertIn("claude/opus", cm.exception.raw)     # the CLI/caller's original words, not a character changed (B21)


class ProbesStayUnderTheProbeBudget(_StubEnv):
    """⭐Probes must never trigger Task 2's gate for "no family given, so it's about to wait a long time".

    That gate exists for "a real, live process that forgot to register"; a probe that spits out one ⚠️ line every
    time it runs turns it into wallpaper — a gate our own people have shouted into background noise is worse than
    no gate at all.
    Measured on a real CLI on this machine, 2026-09-21: all ≤0.43s (claude --version 0.07 / --help 0.36 /
    auth status --json 0.43; codex --version 0.05 / login status 0.05) ⇒ probes always use
    `timeout=PROBE_MAX_SECONDS`, which still leaves 23x headroom.
    Never take either of the other two routes: passing `family` would make a second-scale probe pay for a birth
    id plus two registry-table writes for nothing (a birth id, before switching to ctypes, took 0.8s on win32,
    now microseconds; on POSIX it's a `ps` call); raising `PROBE_MAX_SECONDS` is the same as widening this gate's
    blind spot for missed detections."""

    def _warnings_during(self, fn):
        with mock.patch.object(scv, "log") as spy:
            fn()
        return [c.args[0] for c in spy.call_args_list if "not in the registry" in c.args[0]]

    def test_detect_trips_it_zero_times(self):
        with mock.patch.object(scv, "cli_head", helpers.fake_head):
            self.assertEqual(self._warnings_during(lambda: scv.detect({"extra_models": {}})), [])

    def test_auth_status_trips_it_zero_times(self):
        for family in ("claude", "codex"):
            head = helpers.fake_head(family)
            self.assertEqual(self._warnings_during(lambda: scv.auth_status(family, head)), [], family)

    def test_the_scanner_is_not_blind(self):
        """Positive control: when this gate really does fire, do I catch it? Feed it a run_cli that deliberately
        goes over budget.
        Without this line, the two tests above ("no ⚠️") and "I'm simply not listening" look identical."""
        noisy = self._warnings_during(
            lambda: scv.run_cli(helpers.fake_head("claude") + ["--version"], timeout=scv.PROBE_MAX_SECONDS + 1))
        self.assertEqual(len(noisy), 1)

    def test_nothing_lands_in_bridge_log_either(self):
        """Another layer of the same thing: the tests above intercept the call to `scv.log`, this one looks at the
        bytes that really land on disk.
        ⭐The positive control is welded into the same test case: of course that line of text can't be found in an
        empty file ⇒ without first proving "it really does land there", this assertNotIn is a hollow statement."""
        p = scv.spath("bridge.log")

        def tail(since):
            return (p.read_text(encoding="utf-8") if p.exists() else "")[since:]

        before = len(tail(0))
        with mock.patch.object(scv, "cli_head", helpers.fake_head):
            scv.detect({"extra_models": {}})
        after = before + len(tail(before))
        self.assertNotIn("not in the registry", tail(before))          # detect wrote not a single line
        scv.run_cli(helpers.fake_head("claude") + ["--version"], timeout=scv.PROBE_MAX_SECONDS + 1)
        self.assertIn("not in the registry", tail(after))              # same file, same path: over budget really does land

    def test_every_probe_timeout_is_within_the_budget(self):
        """The other half, following the shape of the gate, not these two call sites we happen to have in hand:
        every `run_cli` in the whole file that does not pass `family` (which, by Task 2's definition, makes it a
        probe) must have `timeout` ≤ PROBE_MAX_SECONDS.
        ⚠️This used to scan only `("detect", "auth_status")` by a function-name whitelist, while the docstring
        claimed "a newly added probe that forgot to set the cap ⇒ this test calls it out by name" — a probe
        written into a third function was simply not covered at all, less coverage than it claimed for itself.
        The criterion was already at hand: not passing family = a probe."""
        import ast
        import io as _io
        with _io.open(scv.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        def within_budget(t):
            """⭐Both `timeout=PROBE_MAX_SECONDS` (the constant itself) and `timeout=<an integer within budget>`
            count. Never accept only integer literals: that would judge the style that should be most encouraged
            as a violation (which is exactly what my first version did)."""
            if isinstance(t, ast.Name) and t.id == "PROBE_MAX_SECONDS":
                return True
            return isinstance(t, ast.Constant) and isinstance(t.value, int) \
                and not isinstance(t.value, bool) and t.value <= scv.PROBE_MAX_SECONDS

        def where(call):
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef) and call in list(ast.walk(n)):
                    return n.name
            return "<module>"

        def is_probe(call):
            """⭐The criterion needs to line up with the gate at runtime: what that gate checks is
            `family is None`, so a call that explicitly writes `family=None` is also a probe. Never look only at
            "does this keyword exist" — that would let `run_cli(…, family=None)` make the two gates reach
            opposite conclusions about the same line of code: at runtime it's treated as a probe, in the AST it
            is not, and the side that misses it is this gate."""
            kw = {k.arg: k.value for k in call.keywords}
            if "family" not in kw:
                return True
            return isinstance(kw["family"], ast.Constant) and kw["family"].value is None

        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run_cli"]
        probes = [c for c in calls if is_probe(c)]
        bad = ["%s@%d" % (where(c), c.lineno) for c in probes
               if not within_budget({k.arg: k.value for k in c.keywords}.get("timeout"))]
        self.assertEqual(bad, [])
        # the ruler is not blind: the whole file really was scanned for run_cli calls that don't pass family
        # (never let an empty scan pass itself off as all green)
        self.assertGreaterEqual(len(probes), 4)

        # ── The criterion's own two-sided control. ⚠️"an integer literal within budget ⇒ True" originally had not
        #    a single cell covering it, and what the implementer's first version fell into was precisely the
        #    direction of "judging the correct way of writing it as a violation".
        def parsed(src):
            return ast.parse(src).body[0].value

        self.assertTrue(within_budget(parsed("PROBE_MAX_SECONDS")))
        self.assertTrue(within_budget(parsed("5")))                      # <- the cell that was added
        self.assertTrue(within_budget(parsed(str(scv.PROBE_MAX_SECONDS))))
        self.assertFalse(within_budget(parsed(str(scv.PROBE_MAX_SECONDS + 1))))
        self.assertFalse(within_budget(parsed("OTHER_CONST")))           # <- never let a different name slip through
        self.assertFalse(within_budget(parsed("PROBE_MAX_SECONDS * 3")))
        self.assertFalse(within_budget(parsed("True")))                  # bool is a subclass of int
        self.assertFalse(within_budget(None))                            # forgot to write a cap

        # ── `is_probe`'s own two-sided control: an explicit `family=None` must be treated as a probe, a named
        #    one must never be treated as a probe.
        #    (No one writes it this way in `scv.py` today ⇒ without this control it would be dead code that was
        #    never checked.)
        self.assertTrue(is_probe(ast.parse("run_cli(a, family=None, timeout=99)").body[0].value))
        self.assertFalse(is_probe(ast.parse("run_cli(a, family='claude', timeout=99)").body[0].value))
        self.assertTrue(is_probe(ast.parse("run_cli(a, timeout=99)").body[0].value))


class CodexDefaults(unittest.TestCase):
    """0.2.0 (the maintainer's decision, 2026-09-27): the Codex family's default models are the service's own
    subscription seats (gpt-6-luna / gpt-5.6-terra / gpt-6-sol).
    The first one is the call `doctor --live` and the service's check make, so it must be the cheapest (luna).
    The same day the maintainer asked for the bridge to find local models by itself; this table is a stopgap,
    so do not write it down anywhere else."""

    def test_codex_catalog_is_the_services_set(self):
        self.assertEqual(scv.catalog({}, {"codex": {}}),
                         ["codex/gpt-6-luna", "codex/gpt-5.6-terra", "codex/gpt-6-sol"])

    def test_the_old_names_are_still_reachable_through_extra_models(self):
        cat = scv.catalog({"extra_models": {"codex": ["gpt-5.6-luna"]}}, {"codex": {}})
        self.assertIn("codex/gpt-5.6-luna", cat)

    def test_version(self):
        self.assertEqual(scv.VERSION, "0.3.0")


if __name__ == "__main__":
    unittest.main()
