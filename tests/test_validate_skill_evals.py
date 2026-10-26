"""Tests for the partner-managed tier in .github/scripts/validate_skill_evals.py.

The tier tests read SKILL.md files on disk; the evals tests build a throwaway Git
repository with a skill in it, the way the workflow sees a pull request.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".github" / "scripts"))

import validate_skill_evals  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def skill_md(name: str, metadata_extra: str = "") -> str:
    return (
        f"---\nname: {name}\ndescription: Use this skill when testing {name}.\n"
        f'metadata:\n  author: octocat\n  version: "1.0.0"\n{metadata_extra}'
        f"---\n\n# {name}\n"
    )


class SkillTierTests(unittest.TestCase):
    def tier(self, text: str):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        path = directory / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        return validate_skill_evals.skill_tier(path)

    def test_reads_the_tier_in_each_scalar_style(self):
        for value in ("partner-managed", '"partner-managed"', "'partner-managed'", "partner-managed  # who maintains it"):
            with self.subTest(value=value):
                self.assertEqual(self.tier(skill_md("s", f"  tier: {value}\n")), "partner-managed")

    def test_a_hash_without_a_space_is_part_of_the_value(self):
        self.assertEqual(self.tier(skill_md("s", "  tier: partner-managed#x\n")), "partner-managed#x")

    def test_reads_aws_managed(self):
        self.assertEqual(self.tier(skill_md("s", '  tier: "aws-managed"\n')), "aws-managed")

    def test_no_tier(self):
        self.assertIsNone(self.tier(skill_md("s")))

    def test_ignores_a_tier_that_is_not_directly_under_metadata(self):
        cases = {
            "top level": '---\nname: s\ntier: partner-managed\nmetadata:\n  version: "1.0.0"\n---\n',
            "nested deeper": '---\nname: s\nmetadata:\n  version: "1.0.0"\n  extra:\n    tier: partner-managed\n---\n',
            "after metadata ends": '---\nname: s\nmetadata:\n  version: "1.0.0"\nother:\n  tier: partner-managed\n---\n',
            "in the body": '---\nname: s\nmetadata:\n  version: "1.0.0"\n---\n\nmetadata:\n  tier: partner-managed\n',
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.tier(text))

    def test_unreadable_frontmatter_reads_as_no_tier(self):
        cases = {
            "flow mapping": "---\nname: s\nmetadata: {tier: partner-managed}\n---\n",
            "no frontmatter": "# s\n\nmetadata:\n  tier: partner-managed\n",
            "unterminated": "---\nname: s\nmetadata:\n  tier: partner-managed\n",
            "not UTF-8": None,
        }
        for name, text in cases.items():
            with self.subTest(name):
                if text is None:
                    directory = Path(tempfile.mkdtemp())
                    self.addCleanup(shutil.rmtree, directory)
                    path = directory / "SKILL.md"
                    path.write_bytes(b"---\nmetadata:\n  tier: partner-managed\xff\n---\n")
                    self.assertIsNone(validate_skill_evals.skill_tier(path))
                else:
                    self.assertIsNone(self.tier(text))

    def test_missing_file_reads_as_no_tier(self):
        self.assertIsNone(validate_skill_evals.skill_tier(Path(tempfile.gettempdir()) / "no-such-dir" / "SKILL.md"))


STANDARD_REASON = "Partner-managed skill: skill evaluation tool results aren't produced for partner-managed skills."
EXEMPT_ALL = json.dumps({t: {"reason": STANDARD_REASON} for t in ("structure", "best-practices", "functional")})


class PartnerManagedEvalsTests(unittest.TestCase):
    """A pull request adding or changing a skill, run through build_reports and main()."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "base\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, env={**os.environ, **GIT_ENV}
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def add_skill(self, name: str, tier: str | None, exemptions: str | None = EXEMPT_ALL, evals_json: bool = False) -> None:
        self.write(f"skills/{name}/SKILL.md", skill_md(name, f"  tier: {tier}\n" if tier else ""))
        if exemptions is not None:
            self.write(f"skills/{name}/evals/exemptions.json", exemptions)
        if evals_json:
            self.write(f"skills/{name}/evals/evals.json", "[]\n")
        self.commit()

    def report(self, name: str, base: str | None = None, **kwargs) -> validate_skill_evals.SkillReport:
        (report,) = validate_skill_evals.build_reports(self.repo, [name], base or self.base, **kwargs)
        return report

    def label(self) -> dict:
        return {"force_reason": "enforce-evals label", "withdraw_exemptions": "the `enforce-evals` label"}

    def test_a_partner_managed_skill_with_the_standard_exemption_needs_no_evals_json(self):
        self.add_skill("theirs", "partner-managed")
        report = self.report("theirs")
        self.assertTrue(report.enforced)
        self.assertTrue(report.ok, report.violations)
        self.assertTrue(any("`evals.json` not required" in e for e in report.exemptions))

    def test_a_partner_managed_skill_still_needs_its_results_exempted(self):
        self.add_skill("theirs", "partner-managed", exemptions=None)
        self.write("skills/theirs/evals/.keep", "")
        self.commit()
        report = self.report("theirs")
        self.assertFalse(report.ok)
        self.assertFalse(any("evals.json" in v for v in report.violations))
        self.assertTrue(any("structure/" in v for v in report.violations))

    def test_the_label_requires_evals_json_and_the_results_again(self):
        self.add_skill("theirs", "partner-managed")
        report = self.report("theirs", **self.label())
        self.assertFalse(report.ok)
        self.assertTrue(any("evals.json" in v for v in report.violations))
        self.assertTrue(any("structure/" in v for v in report.violations))

    def test_other_tiers_still_need_evals_json(self):
        for name, tier in (("ours", "aws-managed"), ("plain", None)):
            with self.subTest(tier=tier):
                self.add_skill(name, tier)
                report = self.report(name)
                self.assertEqual(report.violations, [f"missing file `skills/{name}/evals/evals.json`"])

    def test_a_malformed_exemptions_file_still_fails_a_partner_managed_skill(self):
        self.add_skill("theirs", "partner-managed", exemptions='{"functional": {"reason": ""}}')
        report = self.report("theirs")
        self.assertTrue(report.enforced)
        self.assertFalse(report.ok)
        self.assertTrue(any("non-empty `reason`" in v for v in report.violations))

    def test_committed_results_of_a_partner_managed_skill_cannot_be_deleted(self):
        self.add_skill("theirs", "partner-managed", exemptions=None, evals_json=True)
        self.write("skills/theirs/evals/structure/structure-tests-results-v1.json", "{}\n")
        base = self.commit()
        shutil.rmtree(self.repo / "skills/theirs/evals")
        self.write("skills/theirs/SKILL.md", skill_md("theirs", "  tier: partner-managed\n") + "more\n")
        self.commit()
        report = self.report("theirs", base=base)
        self.assertTrue(report.enforced)
        self.assertFalse(report.ok)

    def run_main(self, *argv: str) -> int:
        with mock.patch.object(validate_skill_evals, "_repo_root", return_value=self.repo), mock.patch.dict(
            os.environ, {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}, clear=True
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return validate_skill_evals.main(list(argv))

    def test_main_passes_a_partner_managed_skill_and_fails_it_under_the_label(self):
        self.add_skill("theirs", "partner-managed")
        self.assertEqual(self.run_main("--base-ref", self.base, "--head-ref", "HEAD"), 0)
        self.assertEqual(self.run_main("--base-ref", self.base, "--head-ref", "HEAD", "--labels", '["enforce-evals"]'), 1)

    def test_main_skill_option_applies_the_same_rule(self):
        self.add_skill("theirs", "partner-managed")
        self.add_skill("ours", "aws-managed")
        self.assertEqual(self.run_main("--skill", "theirs"), 0)
        self.assertEqual(self.run_main("--skill", "ours"), 1)


if __name__ == "__main__":
    unittest.main()
