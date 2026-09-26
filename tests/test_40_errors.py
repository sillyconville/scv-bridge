# -*- coding: utf-8 -*-
"""The classifier. ⭐The fixtures are original text produced on a real CLI on 09-21 (CLAUDE_CONFIG_DIR/CODEX_HOME
pointed at an empty directory), never an error I imagined."""
import re
import unittest

from tests import helpers

import scv  # noqa: E402


def setUpModule():
    helpers.fresh_home("errors", unittest.addModuleCleanup)

REAL_CLAUDE_NOAUTH = "Not logged in · Please run /login"
REAL_CODEX_NOAUTH = ("ERROR: unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
                     "url: https://api.openai.com/v1/responses, cf-ray: a3e813305d9f3d8c-SIN, request id: req_7f3e")


class Classify(unittest.TestCase):
    def test_real_auth_fixtures(self):
        self.assertEqual(scv.classify(REAL_CLAUDE_NOAUTH), "auth_required")
        self.assertEqual(scv.classify(REAL_CODEX_NOAUTH), "auth_required")

    def test_quota(self):
        self.assertEqual(scv.classify("You've hit your session limit · resets 3pm"), "quota")
        self.assertEqual(scv.classify("Rate limit exceeded, please retry shortly"), "quota")

    def test_unknown_stays_unknown(self):
        """Not recognizable means unknown — never lean toward whichever category looks closest."""
        self.assertEqual(scv.classify("segmentation fault"), "unknown")
        self.assertEqual(scv.classify("segmentation fault", "crashed"), "crashed")
        self.assertEqual(scv.classify(""), "unknown")

    def test_body_keeps_the_raw_text_verbatim(self):
        """The last sentence, codex's `fix_hint`, is a contract that goes back and forth across batches, and the
        reason has been the same sentence both times: an error message must never lie.
        (1) Before Task 12 it was a bare `codex login`; (2) Task 13 changed it to scv's own codex-login
          subcommand — back then scv always gave codex a dedicated CODEX_HOME (`~/.scv/codex-home`), a bare
          `codex login` would log into `~/.codex`, and running it would still leave you with a 401 (Task 12's
          spot check on a real CLI hit exactly this), and `fix_hint` goes into the API error body, where there's
          no way to compute a command that both carries a path and can be pasted into any shell ⇒ the only option
          was to point at a subcommand that does not depend on the shell;
        (3) Task 13c changed it back to `codex login`: the maintainer ruled on 09-24, "don't make the user log in,
          asking them to log in raises suspicion" ⇒ codex went back to the player's own CODEX_HOME, that
          subcommand was removed, and `codex login` logs into exactly the home the bridge uses ⇒ this sentence
          became true again.
        ⚠️It still does not know where the executable is (a codex bundled with a desktop app is mostly not on
          PATH): only doctor can compute the one that carries a path (`login_cmd`).
        ⭐That the scv subcommand named in `fix_hint`'s return value genuinely exists is pinned by
          tests/test_70_local_api.py::PromisedCommands (there are zero of them today)."""
        body = scv.error_body(scv.BridgeError("auth_required", REAL_CLAUDE_NOAUTH, "claude"))["error"]
        self.assertEqual(body["message"], REAL_CLAUDE_NOAUTH)        # B21: not a character changed
        self.assertEqual((body["type"], body["code"], body["family"]), ("auth_required", "auth_required", "claude"))
        self.assertEqual(body["fix_hint"], "claude auth login")
        self.assertEqual(body["retryable"], False)
        self.assertEqual(scv.fix_hint("auth_required", "codex"), "codex login")
        self.assertEqual(scv.error_body(scv.BridgeError("timeout", "x", "codex"))["error"]["retryable"], True)

    def test_every_class_has_a_status(self):
        for k in ("auth_required", "quota", "timeout", "crashed", "cancelled", "bad_request", "local_rate_limit", "unknown"):
            self.assertIn(k, scv.HTTP_STATUS)
        self.assertEqual((scv.HTTP_STATUS["auth_required"], scv.HTTP_STATUS["quota"]), (401, 429))


# ━━ The four tests below were added beyond the brief. Each one was asked first "could this false-alarm / could
#   this miss a detection", and the reasoning is written in its own docstring.

class OrderIsPinned(unittest.TestCase):
    """The order "recognize a rate limit before recognizing a login problem" inside `classify` — is that just a
    comment, or a real constraint?"""

    def test_quota_wording_that_also_says_login_is_still_quota(self):
        """Rate-limit wording can carry the word login in it (switch accounts / log in again), but never the other
        way around ⇒ swap the order and a single rate limit would get called "you are not logged in", sending the
        user to run a login that will not help even after running it (the Task 4 sickness).

        Never write `assertNotEqual(..., "auth_required")`: that does not pin down why it went red. Assert the
        correct value, "quota".
        Could this miss a detection: an implementation that judges everything as quota could fool this test alone
        — but `test_unknown_stays_unknown` blocks exactly that path, and the two only mean something together."""
        raw = "You've hit your usage limit · please run /login to switch accounts"
        self.assertEqual(scv.classify(raw), "quota")


class BodyIsVerbatim(unittest.TestCase):
    """The real shape of B21: the original text can be multi-line, and the half where the error actually is often
    is not on the first line."""

    def test_a_multi_line_raw_survives_word_for_word(self):
        """The outbound version of the same fall Task 4 took: `splitlines()[0]` throws away the reason (node's
        crash has its stack header as the first two lines, and `Error: Cannot find module …` comes after),
        folding it into a ` ⏎ ` here is also changing the original text.
        A JSON body can hold a newline just fine ⇒ the correct action here is to not move a single character.

        Could this false-alarm: no, assertEqual only goes red when someone has touched the original text.
        Could this miss a detection: no, both folding and truncating would make assertEqual go red, and the line
        below pins down that the newline is still there."""
        raw = ("node:internal/modules/cjs/loader:1215" + scv.NL
               + "  throw err;" + scv.NL
               + "Error: Cannot find module '@anthropic-ai/claude-code/cli.js'")
        body = scv.error_body(scv.BridgeError("crashed", raw, "claude"))["error"]
        self.assertEqual(body["message"], raw)
        self.assertIn(scv.NL, body["message"])
        self.assertIn("Cannot find module", body["message"])   # this is exactly the sentence that would have been thrown away


class HintsDoNotGuess(unittest.TestCase):
    """A fix instruction is only ever given when we genuinely know what to run. Leave it empty when it cannot be
    recognized, never send the user off to fix something that has nothing wrong with it."""

    def test_an_unknown_family_gets_no_command(self):
        """When the family name is an empty string (BridgeError's default) or unrecognized, we cannot say which
        login to run ⇒ give an empty string. Never fall back to "guess the most common one" like
        "claude auth login" — that would be writing "we don't know" as "you did something wrong"."""
        self.assertEqual(scv.fix_hint("auth_required", ""), "")
        self.assertEqual(scv.fix_hint("auth_required", "nosuch"), "")
        self.assertEqual(scv.error_body(scv.BridgeError("auth_required", "x"))["error"]["fix_hint"], "")

    def test_classes_with_nothing_to_do_get_no_hint(self):
        """Crashed / timeout / could not be recognized: there is not a single action on the user's end that could
        fix it ⇒ an empty string, never make up a sentence to fill it.

        ⚠️`local_rate_limit` is not on this list (pinned separately in the next test): it is exactly the category
        where we know completely well what to do."""
        for klass in ("unknown", "crashed", "timeout", "cancelled", "bad_request"):
            self.assertEqual(scv.fix_hint(klass, "claude"), "", klass)

    def test_the_local_rate_limit_hint_says_whose_limit_it_is_and_how_long_the_window_is(self):
        """🔴`retryable=True` can never come without something to go with it: when this category returns 429 with
        no information at all about how long to wait, the client will only retry immediately, hit 429 again, and
        retry again — while this gate's window is measured by the hour
        (`DEFAULT_CONFIG["remote_jobs_per_hour"]`), never seconds. Hammering it for a whole hour is worse than the
        original self-contradiction.

        ⇒ This sentence has to spell out three things at once: (1) this is the bridge's own rate limit, never an
          upstream quota; (2) the real scale of the window; (3) what to do about it. Assert the whole sentence
          (never a loose assertion like `"hour" in hint`); it's the same sentence for both families, since it has
          nothing to do with which CLI it is.
        ⏳The machine-readable HTTP `Retry-After` header is still owned by the wiring batch — that one is read by
        machines, this sentence is read by people, and the two never substitute for each other.

        🔴2026-09-22, Task 11 changed this sentence: that batch added a second source for this `klass` ("the
          in-flight count has hit its cap"), while the original sentence only wrote the hourly one ⇒ for the new
          source it is half-true: what should actually be waited out is a few seconds, and the knob to turn is
          `max_concurrent`, yet it tells the person to "wait for this hour's window to pass". ⭐A piece of advice
          that's wrong for half the cases is worse than no advice at all — `fix_hint`'s entire job is "let the
          person know what to do".
        ⭐The reader of this sentence is the dispatcher's implementer/operator (production hits for this category
          on the local leg are 0: in the whole file only `RemoteLeg` can produce a `local_rate_limit`, and that
          leg sends no HTTP headers) ⇒ give the knob's name directly, no need to cater to a user on the SDK side.
        ⭐The closing sentence, "use a new job_id", is never a courtesy: the refused id has already been recorded
          in `_seen`, and resending the same id will only get back a `dup` ack (see `RemoteLeg._take`)."""
        expect = ("this is the bridge's own rate limit, not an upstream quota; which one you hit is in the original "
                  "words — the hourly job count is config.json's remote_jobs_per_hour (window by the hour), the "
                  "in-flight count is max_concurrent (wait for a few to finish) ⇒ do not retry immediately, retry "
                  "with a new job_id")
        self.assertEqual(scv.fix_hint("local_rate_limit", "claude"), expect)
        self.assertEqual(scv.fix_hint("local_rate_limit", "codex"), expect)
        self.assertEqual(scv.error_body(scv.BridgeError("local_rate_limit", "x", "codex"))["error"]["fix_hint"], expect)

    def test_the_quota_hint_is_the_same_on_both_families_and_says_nothing_about_logging_in(self):
        """Running out of quota has nothing to do with logging in ⇒ the same sentence for both families, and there
        is no "go log in" action anywhere in it (assert the whole sentence, never `login not in`)."""
        expect = "wait for the quota to reset (see the original words for how long), or switch to a model from the other family on this bridge"
        self.assertEqual(scv.fix_hint("quota", "claude"), expect)
        self.assertEqual(scv.fix_hint("quota", "codex"), expect)


class TheTableIsTheContract(unittest.TestCase):
    """`retryable`/`type`/`code` are three fields a client genuinely acts on, pinned down class by class."""

    ALL = ("auth_required", "quota", "timeout", "crashed", "cancelled", "bad_request", "local_rate_limit", "unknown")
    # ⭐Hard-coded on this test's side: referencing scv.RETRYABLE would just be a tautology.
    # 🔴`local_rate_limit` = a plan-mandated deviation the controller ruled on (the brief says not retryable): 429
    #   + retryable=False is a self-contradiction — that one is refused by the bridge's own rate limit, it is
    #   bound to succeed once the window passes, and returning "do not retry" is the same as calling a request
    #   that will definitely succeed later a permanent failure. ⚠️But the window is measured by the hour
    #   (`remote_jobs_per_hour`), never seconds ⇒ giving True with nothing about "how long to wait" would have the
    #   client hammering it for a whole hour ⇒ `fix_hint`'s sentence of human language is what goes with it, see
    #   the `HintsDoNotGuess` test; the machine-readable `Retry-After` header belongs to the wiring batch.
    RETRY = {"timeout", "crashed", "quota", "local_rate_limit"}

    def test_retryable_is_pinned_class_by_class(self):
        """Retrying an auth_required once will only produce the same error again; retrying quota/timeout/crashed
        is the one that actually means something."""
        for klass in self.ALL:
            body = scv.error_body(scv.BridgeError(klass, "x", "claude"))["error"]
            self.assertEqual(body["retryable"], klass in self.RETRY, klass)

    def test_type_and_code_are_always_the_same_class(self):
        """Some OpenAI-compatible clients read type, some read code ⇒ the two fields must never diverge."""
        for klass in self.ALL:
            body = scv.error_body(scv.BridgeError(klass, "x", "codex"))["error"]
            self.assertEqual((body["type"], body["code"]), (klass, klass), klass)

    def test_classify_only_ever_returns_something_the_table_knows(self):
        """Whatever class the classifier produces must be convertible into an HTTP status code — otherwise
        `HTTP_STATUS[klass]` downstream would be a KeyError."""
        for raw in (REAL_CLAUDE_NOAUTH, REAL_CODEX_NOAUTH, "You've hit your session limit", "segmentation fault", ""):
            self.assertIn(scv.classify(raw), scv.HTTP_STATUS, raw)


class QuotaWordsAreWordsNotFragments(unittest.TestCase):
    """The single-word entries in the rate-limit group (overloaded/resets/quota) must be recognized as words,
    never as bare substrings.

    ⚠️This test's original text is something I constructed, never a real CLI's original words (unlike the two
      REAL_* tests above): `presets` contains `resets`, `quotation` contains `quota`, `overloadedFn` contains
      `overloaded`, and all three are crash/syntax-error wording, the same shape you'd run into on a real CLI.
    🔴The cost of a false positive: judged as quota ⇒ the status code is wrong, 429, and `fix_hint` lies — it
      tells the user to wait for a quota reset that does not exist at all, and no matter how long he waits it
      will never get better.
      ⚠️"the client will keep retrying" is never this cell's crime (I got this sentence wrong before, fixed in
        both places): `crashed` was already in `RETRYABLE` to begin with ⇒ judging it correctly still leaves the
        client retrying that broken CLI."""

    NOT_A_LIMIT = ("Error: unknown preset; available presets: fast, deep",
                   "SyntaxError: Unterminated quotation mark near line 3",
                   "TypeError: overloadedFn is not a function")
    # 🔴The true-positive fixtures are split into three categories by source, never just to pad out the count: the
    #   previous round's three true positives were all "a bare word inside a natural sentence", the exact same
    #   shape ⇒ the entire category "a machine-readable error code" had not a single entry in the fixtures ⇒ the
    #   missed detection I introduced myself was structurally invisible to the test bench (it was found by a
    #   person reading it, never caught by the test). ⭐The diversity of the fixtures is itself part of the
    #   criterion: each category has different characters glued to both sides of the word (whitespace/`_`/`"`/CJK),
    #   and what needs guarding against this time is exactly "what is glued to it".
    # (1) A CLI's natural sentence (prose). ⚠️Constructed by me, never a real CLI's original words; if real
    #   original words turn up someday they can replace this, and replacing it should record the date too.
    LIMIT_PROSE = ("The upstream model is overloaded, please try again later",
                   "You've used up this 5h window - resets 14:32",
                   "Error: quota exceeded for this workspace")
    # (2) A machine-readable error code: this is really how the upstream (OpenAI/Anthropic) replies, and codex's
    #    error line carries the body out as-is (see the shape of REAL_CODEX_NOAUTH at the top of this file) ⇒ what
    #    is glued to both sides here is `_` and `"`.
    LIMIT_CODES = ('{"error":{"code":"insufficient_quota","type":"insufficient_quota"}}',
                   '{"type":"quota_exceeded"}',
                   'ERROR: unexpected status 429: {"type":"overloaded_error"}')
    # (3) A Chinese-language context: CJK counts as a word-internal character too ⇒ a Chinese character glued
    #    right up against it likewise stops a word boundary from forming (the same sickness as `_`, wearing a
    #    different face).
    LIMIT_CJK = ("限流quota已用尽", "额度overloaded，稍后再试")
    ALL_REAL_LIMITS = LIMIT_PROSE + LIMIT_CODES + LIMIT_CJK
    # A known residual: an identifier inside quotes is itself literally that word ⇒ the word-boundary trick cannot
    # fix it (the two tests below pin it down side by side).
    RESIDUAL = "TypeError: Cannot read properties of undefined (reading 'resets')"

    def test_a_word_fragment_is_not_a_limit(self):
        """One end pinned down: things that should never match fall back to unknown (assert the correct value,
        never `!= "quota"`)."""
        for raw in self.NOT_A_LIMIT:
            with self.subTest(raw=raw):   # never let the first red block the other two: each of the three is a different way of embedding, and all three must be reported
                self.assertEqual(scv.classify(raw), "unknown")

    def test_the_word_still_hits_when_it_really_is_one(self):
        """The other end pinned down: never eliminate the true positives along with the false ones just to be
        rid of them.

        These three are recognized only by the word-boundary branch (not one of the phrase entries matches them)
        ⇒ if anyone takes the shortcut and deletes these three words from the table entirely, this test goes red
        immediately. Could this miss a detection: no — the test above pins down the opposite direction at the
        same time."""
        for raw in self.LIMIT_PROSE:
            with self.subTest(raw=raw):
                self.assertEqual(scv.classify(raw), "quota")

    def test_a_chinese_context_is_still_a_limit(self):
        """The third source category: a Chinese character glued right up against the word in the original Chinese
        text also counts as a word-internal character ⇒ the same sickness as `_`, wearing a different face."""
        for raw in self.LIMIT_CJK:
            with self.subTest(raw=raw):
                self.assertEqual(scv.classify(raw), "quota")

    def test_a_machine_readable_code_is_still_a_limit(self):
        """🔴The third head (this one is a missed detection introduced by tightening the boundary itself, never a
        gap in coverage that was there from the start).

        `\\b` treats `\\w` as a word-internal character, and `\\w` includes `_` and CJK ⇒ `insufficient_quota` /
        `quota_exceeded` / `overloaded_error` / the `LIMIT_CJK` fixture fail to form a word boundary on either side ⇒ a
        real rate limit silently falls into unknown: 502 + `retryable=False` + no fix_hint, and the client throws
        away a request that would have succeeded in 60 seconds as a permanent failure.
        ⭐The true-positive fixtures above are structurally blind to this category: all three of them are the same
        shape, "a bare word plus whitespace" ⇒ a gate has to be built to the shape of the bug (what characters are
        glued to it), never to the incident that happens to be on hand."""
        for raw in self.LIMIT_CODES:
            with self.subTest(raw=raw):
                self.assertEqual(scv.classify(raw), "quota")

    def test_the_boundary_does_not_lean_on_the_caller_lowercasing_first(self):
        """⭐The criterion itself has to stand on its own: the letter glued to the word could be uppercase
        (`overloadedFn`'s `F`, `Presets`'s `P`).

        Today `classify()` calls `.lower()` first (that line in `scv.py`, `low = (raw or "").lower()`) ⇒ either way
        of writing it passes all the tests above. But a version that only blocks lowercase letters has its
        correctness parked on a `.lower()` call inside a different function: whoever moves that line away, or
        whoever scans the original text directly with `QUOTA_WORD`, the false positive comes right back, while
        every test above is still green.
        ⇒ This test feeds original text that has not been lowered directly into that expression, pinning "does
        not depend on the caller" as a contract.
        ⚠️This test is already green today (this round changed no implementation), it was never won by first going
          red — its non-vacuousness is proven by the negative control in the report ("switch to the
          `(?<![a-z0-9])…(?![a-z])` version ⇒ this test goes red immediately")."""
        for raw in ("TypeError: overloadedFn is not a function", "Error: unknown preset; available Presets: fast"):
            with self.subTest(raw=raw):
                self.assertIsNone(scv.QUOTA_WORD.search(raw))

    def test_the_residual_is_exactly_quota_today(self):
        """⭐Add a criterion, never swap it out: the `expectedFailure` test below only says "today it does not
        equal the correct value", it cannot say what it actually equals today ⇒ the day it drifts from quota to
        auth_required/crashed, that test is still "an expected failure", still green, with no one watching it.
        This test pins down today's actual verdict; only with the two side by side is the correct value recorded
        and any drift made visible.
        (In the previous round I swapped it straight from "assert quota" to expectedFailure ⇒ the criterion was
        swapped out — this is exactly what re-review caught.)
        ⚠️The day this residual is genuinely fixed: this test has to change together with the one below (this one
        changes to unknown, the one below gets deleted)."""
        self.assertEqual(scv.classify(self.RESIDUAL), "quota")

    @unittest.expectedFailure   # ⭐the correct value is on record, today it just cannot be reached ⇒ whoever fixes it gets an unexpectedSuccess (still counted as a failure), forcing them to come delete this test
    def test_a_quoted_identifier_that_really_is_the_word_is_a_known_residual(self):
        """⚠️What this test pins down is the residual — it asserts the correct value, `unknown`, while today's
        implementation gives `quota`. Please delete this test together with the fix when it's done.

        The `resets` inside `(reading 'resets')` really is that exact word (a quote is not a letter ⇒ the boundary
        still holds) ⇒ looking at the text alone cannot tell "an identifier inside quotes" apart from "that word
        in a real rate-limit's original text".
        Fixing it requires structure (the child process's rc / exit shape / the CLI's own error type), never
        piling more tricks onto the pattern strings —
        ⚠️The reason is never "the word in a real rate limit is always bare" (that claim does not hold: two real
        `resets` entries in this test bench are both followed by a number, and a rule of "must be followed by a
        time" could tell the two apart); the real reason is that kind of tightening would also kill true
        positives on its own: `error: 'quota' exceeded` is exactly a real rate limit wrapped in quotes ⇒ it would
        just be swapping which mole gets whacked.
        I originally wrote this in as a third false-positive fixture, and only found out by actually running it
        that the boundary trick cannot fix it ⇒ changed it into an explicit piece of documented evidence."""
        self.assertEqual(scv.classify(self.RESIDUAL), "unknown")

    # This is a historical snapshot: the 8 bare substrings that criterion used before commit 0c57cab. What it
    # records is "how the code judged it back then", a fact that will never change again ⇒ a literal is the right
    # thing here. Never read this as "a copy of the production table" (that one does change, see the test below).
    OLD_PLAIN_SUBSTRING_JUDGE = ("usage limit", "session limit", "hit your", "rate limit", "overloaded",
                                 "resets", "quota", "exceeded your")

    def test_the_false_positives_would_all_have_hit_the_old_judge(self):
        """One half of the ruler's self-check (a positive control): the three false positives really do all match
        under the old bare-substring criterion — otherwise this test would be equivalent to testing nothing."""
        for raw in self.NOT_A_LIMIT:
            with self.subTest(raw=raw):
                self.assertTrue(any(p in raw.lower() for p in self.OLD_PLAIN_SUBSTRING_JUDGE))

    def test_the_true_positives_are_carried_only_by_the_word_branch(self):
        """The other half of the ruler's self-check: the true positives really are recognized by the word-boundary
        branch specifically, never picked up incidentally by some other branch.

        ⭐This test compares against no wording literal at all (the old way took a subset of phrases from the
          historical snapshot ⇒ the day someone adds a phrase to `QUOTA_PAT` that happens to cover the
          true-positive fixtures, that control would still be all green, while the true-positive test would no
          longer be pinning down the word boundary at all — the ruler goes blind on its own without making a
          sound). Changed to verify by shape instead: turn that branch off and run it again, and the answer must
          collapse back to unknown.
        ⭐This also fills in something the old way could never prove: the old way only proved "the other branches
          don't handle these three", which stops being enough the day one more branch is added.
        ⚠️What gets touched is the measurement side's module-level global, and `finally` puts it back exactly as
          it was; not a single production knob is turned."""
        real = scv.QUOTA_WORD
        scv.QUOTA_WORD = re.compile("(?!x)x")        # an expression that never matches = turns this branch off
        try:
            for raw in self.ALL_REAL_LIMITS:
                with self.subTest(raw=raw):
                    self.assertEqual(scv.classify(raw), "unknown")   # assert the correct value: once it's off, no one recognizes them anymore
        finally:
            scv.QUOTA_WORD = real
        for raw in self.ALL_REAL_LIMITS:                             # once it's put back, the original verdict must be restored (never leave dirty state behind)
            self.assertEqual(scv.classify(raw), "quota", raw)


if __name__ == "__main__":
    unittest.main()
