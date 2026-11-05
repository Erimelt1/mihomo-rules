from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from build_games_direct import BuildError, build


SOURCE_REF = "0" * 40


class GamesDirectBuilderTests(unittest.TestCase):
    def make_tree(self, root: Path) -> tuple[Path, Path]:
        source = root / "source"
        source.mkdir()
        (source / "category-games.list").write_text(
            "\n".join(
                (
                    "+.direct.example",
                    "+.itch.io",
                    "+.itch.zone",
                    "+.supercell.com",
                    "+.clashofclans.com",
                    "+.wbgames.com",
                    "+.warnerbrosgames.com",
                    "+.modrinth.com",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (source / "itchio.list").write_text("+.itch.io\n+.itch.zone\n", encoding="utf-8")
        (source / "supercell.list").write_text(
            "+.supercell.com\n+.clashofclans.com\n", encoding="utf-8"
        )
        (source / "wbgames.list").write_text(
            "+.wbgames.com\n+.warnerbrosgames.com\n", encoding="utf-8"
        )
        policy = root / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_repository": "MetaCubeX/meta-rules-dat",
                    "base_file": "category-games.list",
                    "exclude_files": ["itchio.list", "supercell.list", "wbgames.list"],
                    "guards": {
                        "base_min": 1,
                        "base_max": 20,
                        "output_min": 1,
                        "output_max": 20,
                        "max_added": 2,
                        "max_removed": 2,
                    },
                }
            ),
            encoding="utf-8",
        )
        return source, policy

    def run_build(self, root: Path, **overrides):
        source, policy = self.make_tree(root)
        arguments = {
            "policy_path": policy,
            "source_dir": source,
            "source_ref": SOURCE_REF,
            "output_list": root / "output" / "games-direct.list",
            "report_path": root / "output" / "report.json",
        }
        arguments.update(overrides)
        return build(**arguments), arguments

    def test_subtracts_only_reviewed_categories_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, arguments = self.run_build(root)
            rules = arguments["output_list"].read_text(encoding="utf-8").splitlines()
            self.assertEqual(rules, ["+.direct.example", "+.modrinth.com"])
            self.assertEqual(report["base_count"], 8)
            self.assertEqual(report["excluded_count"], 6)
            self.assertEqual(report["output_count"], 2)
            self.assertEqual(
                set(report["source_sha256"]),
                {"category-games.list", "itchio.list", "supercell.list", "wbgames.list"},
            )
            self.assertTrue(
                all(len(value) == 64 for value in report["source_sha256"].values())
            )

    def test_missing_exclusion_entry_fails_without_replacing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, policy = self.make_tree(root)
            (source / "supercell.list").write_text("+.not-in-base.example\n", encoding="utf-8")
            output = root / "output.list"
            report = root / "report.json"
            output.write_text("sentinel\n", encoding="utf-8")
            report.write_text("sentinel\n", encoding="utf-8")
            with self.assertRaises(BuildError):
                build(
                    policy_path=policy,
                    source_dir=source,
                    source_ref=SOURCE_REF,
                    output_list=output,
                    report_path=report,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")
            self.assertEqual(report.read_text(encoding="utf-8"), "sentinel\n")

    def test_duplicate_in_base_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, policy = self.make_tree(root)
            with (source / "category-games.list").open("a", encoding="utf-8") as stream:
                stream.write("+.itch.io\n")
            with self.assertRaises(BuildError):
                build(
                    policy_path=policy,
                    source_dir=source,
                    source_ref=SOURCE_REF,
                    output_list=root / "output.list",
                    report_path=root / "report.json",
                )

    def test_large_delta_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, policy = self.make_tree(root)
            previous = root / "previous.list"
            previous.write_text("+.old-1.example\n+.old-2.example\n+.old-3.example\n", encoding="utf-8")
            with self.assertRaises(BuildError):
                build(
                    policy_path=policy,
                    source_dir=source,
                    source_ref=SOURCE_REF,
                    output_list=root / "output.list",
                    report_path=root / "report.json",
                    previous_list=previous,
                )

    def test_requires_full_commit_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, policy = self.make_tree(root)
            with self.assertRaises(BuildError):
                build(
                    policy_path=policy,
                    source_dir=source,
                    source_ref="meta",
                    output_list=root / "output.list",
                    report_path=root / "report.json",
                )


if __name__ == "__main__":
    unittest.main()
