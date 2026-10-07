"""build_content.py builds only from a receipt-pinned published cohort and never writes on failure.

Each test builds three real Git repositories and a receipt describing their
committed bytes, then runs a copy of scripts/build_content.py inside a
temporary website directory so the checkout's own data/ is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_content.py"
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}
GIF = b"GIF89a" + b"\x04\x00\x03\x00" + b"\x00" * 16
LESSON_PATH = "no-magic-papers/lessons/alpha.md"


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def implementation(name: str, tier: str, *, linked: bool) -> dict[str, str | None]:
    return {
        "repo": "no-magic",
        "path": f"{tier}/{name}.py",
        "script_slug": name,
        "commit": None,
        "release": None,
        "media_repo": "no-magic-viz" if linked else None,
        "media_status": "linked" if linked else "omitted",
        "scene_path": f"scenes/scene_{name}.py" if linked else None,
        "preview_path": f"previews/{name}.gif" if linked else None,
        "media_note": None if linked else "Comparison script without a preview.",
    }


def card(slug: str, implementations: list[dict[str, str | None]]) -> dict[str, object]:
    drafted = slug == "alpha"
    return {
        "slug": slug,
        "title": f"{slug.title()}: A Fixture Paper",
        "authors": ["Christopher Ré"],
        "venue": "arXiv",
        "year": "2024",
        "arxiv_id": "2401.00001",
        "doi": None,
        "url": "https://arxiv.org/abs/2401.00001",
        "discovered_via": "maintainer",
        "discovered_date": "2026-01-01",
        "status": "implemented",
        "themes": {"primary": "architecture", "secondary": []},
        "tags": ["fixture"],
        "routing": {
            "decision": "backlog-implement",
            "target_repo": "no-magic",
            "target_script_slug": None,
            "target_path": None,
            "target_tier": None,
            "batch_label": "fixture",
            "review_date": None,
        },
        "implementations": implementations,
        "lesson": {
            "path": LESSON_PATH if drafted else None,
            "status": "drafted" if drafted else "none",
        },
        "dependencies_on_other_papers": [] if drafted else [{"slug": "alpha"}],
    }


class Cohort:
    """A valid published cohort: one linked algorithm and one omitted comparison."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.core = base / "no-magic"
        self.papers = base / "no-magic-papers"
        self.viz = base / "no-magic-viz"
        self.site = base / "site"
        self.receipt = base / "receipt.json"
        self.catalog = [
            {
                "tier": "01-foundations",
                "name": "microalpha",
                "display": "Alpha",
                "thesis": "Alpha thesis.",
                "lines": 10,
                "paper_slug": "alpha",
                "teaching_kind": "train_infer",
                "data_source": "in_script",
                "adaptation_note": None,
            },
            {
                "tier": "02-alignment",
                "name": "beta_vs_gamma",
                "display": "Beta vs Gamma",
                "thesis": "Beta thesis.",
                "lines": 20,
                "paper_slug": "beta",
                "teaching_kind": "comparison",
                "data_source": "names_download",
                "adaptation_note": "Compares two arms.",
            },
        ]
        self.cards = [
            card(
                "alpha", [implementation("microalpha", "01-foundations", linked=True)]
            ),
            card(
                "beta", [implementation("beta_vs_gamma", "02-alignment", linked=False)]
            ),
        ]

    def papers_json(self) -> bytes:
        document = {
            "schema_version": 1,
            "papers": [
                {"card_path": f"papers/{c['slug']}.md", "frontmatter": c}
                for c in self.cards
            ],
        }
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()

    def files(self) -> dict[Path, dict[str, bytes]]:
        return {
            self.core: {
                "docs/catalog.json": json.dumps(self.catalog).encode(),
                "01-foundations/microalpha.py": b"print('alpha')\n",
                "02-alignment/beta_vs_gamma.py": b"print('beta')\n",
            },
            self.papers: {
                "data/papers.json": self.papers_json(),
                "papers/alpha.md": b"---\nslug: alpha\n---\n",
                "papers/beta.md": b"---\nslug: beta\n---\n",
                "lessons/alpha.md": b"# Alpha lesson\n",
            },
            self.viz: {
                "scenes/scene_microalpha.py": b"class AlphaScene:\n    pass\n",
                "previews/microalpha.gif": GIF,
            },
        }

    def build(self) -> Cohort:
        (self.site / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.site / "scripts" / "build_content.py")
        for root, files in self.files().items():
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            git(
                root,
                "remote",
                "add",
                "origin",
                f"https://github.com/no-magic-ai/{root.name}.git",
            )
            for relative, data in files.items():
                self.write(root, relative, data)
            self.commit(root)
        self.write_receipt()
        return self

    def write(self, root: Path, relative: str, data: bytes) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def commit(self, root: Path) -> None:
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", "fixture")

    def write_receipt(self, *, status: str = "published") -> None:
        repositories = {}
        for root in (self.core, self.papers, self.viz):
            commit = git(root, "rev-parse", "HEAD")
            inputs = [
                {
                    "path": path,
                    "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
                    "blob": git(root, "rev-parse", f"HEAD:{path}"),
                }
                for path in sorted(git(root, "ls-files").splitlines())
            ]
            repositories[root.name] = {
                "identity": f"no-magic-ai/{root.name}",
                "commit": commit,
                "tree": git(root, "rev-parse", "HEAD^{tree}"),
                "object_format": "sha1",
                "publication": {
                    "status": status,
                    "main_commit": commit,
                    "provider": f"https://github.com/no-magic-ai/{root.name}.git",
                    "ref": "refs/heads/main",
                    "evidence": "fixture",
                },
                "inputs": inputs,
            }
        receipt = {
            "schema_version": 1,
            "cohort": "published",
            "repositories": repositories,
            "invariants": [
                "committed-input-binding",
                "frontmatter-schema",
                "lesson-lifecycle",
                "catalog-shape",
                "invariant-1-catalog-script-owned",
                "invariant-2-implementation-resolves",
                "invariant-3-paper-slug-backref",
                "media-declaration",
                "media-assets",
                "metadata-json-fresh",
                "publication-ancestry",
            ],
        }
        self.receipt.write_text(json.dumps(receipt, indent=2) + "\n")

    def run(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(self.site / "scripts" / "build_content.py"),
                "--core-root",
                str(self.core),
                "--papers-root",
                str(self.papers),
                "--viz-root",
                str(self.viz),
                "--receipt",
                str(self.receipt),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def outputs(self) -> dict[str, bytes | None]:
        return {
            name: path.read_bytes() if path.exists() else None
            for name in ("content.json", "cohort.json")
            for path in [self.site / "data" / name]
        }


class BuildContentTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="site-content-"))
        self.addCleanup(shutil.rmtree, tmp)
        self.cohort = Cohort(tmp).build()

    def assert_fails_without_writing(self, fragment: str) -> None:
        before = self.cohort.outputs()

        result = self.cohort.run()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(fragment, result.stderr)
        self.assertEqual(self.cohort.outputs(), before)

    def test_build_projects_pinned_urls_and_copies_receipt_exactly(self) -> None:
        result = self.cohort.run()

        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = self.cohort.outputs()
        self.assertEqual(outputs["cohort.json"], self.cohort.receipt.read_bytes())
        content = json.loads(outputs["content.json"] or b"")
        core = git(self.cohort.core, "rev-parse", "HEAD")
        viz = git(self.cohort.viz, "rev-parse", "HEAD")
        alpha, beta = content["algorithms"]
        self.assertEqual(
            alpha["source_url"],
            f"https://github.com/no-magic-ai/no-magic/blob/{core}/01-foundations/microalpha.py",
        )
        self.assertEqual(
            alpha["media"]["preview_url"],
            f"https://raw.githubusercontent.com/no-magic-ai/no-magic-viz/{viz}/previews/microalpha.gif",
        )
        self.assertEqual(beta["media"]["status"], "omitted")
        self.assertIsNone(beta["media"]["preview_url"])
        self.assertEqual(content["papers"][0]["authors"], ["Christopher Ré"])

    def test_check_is_nonmutating_and_rejects_stale_output(self) -> None:
        self.assertEqual(self.cohort.run().returncode, 0)
        content = self.cohort.site / "data" / "content.json"
        stamp = content.stat().st_mtime_ns

        current = self.cohort.run("--check")
        unchanged = content.stat().st_mtime_ns
        content.write_bytes(content.read_bytes().replace(b"Alpha", b"Omega"))
        stale = self.cohort.run("--check")

        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertEqual(unchanged, stamp)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("stale: data/content.json", stale.stderr)
        self.assertIn(b"Omega", content.read_bytes())

    def test_check_without_outputs_fails_and_creates_nothing(self) -> None:
        result = self.cohort.run("--check")

        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.cohort.site / "data").exists())

    def test_root_ahead_of_receipt_is_rejected(self) -> None:
        self.cohort.write(
            self.cohort.core, "02-alignment/beta_vs_gamma.py", b"print(2)\n"
        )
        self.cohort.commit(self.cohort.core)

        self.assert_fails_without_writing("no-magic HEAD is not the receipt commit")

    def test_uncommitted_change_in_root_is_rejected(self) -> None:
        self.cohort.write(self.cohort.papers, "papers/beta.md", b"edited\n")

        self.assert_fails_without_writing("has uncommitted changes")

    def test_receipt_digest_that_does_not_match_committed_bytes_is_rejected(
        self,
    ) -> None:
        receipt = json.loads(self.cohort.receipt.read_text())
        for record in receipt["repositories"]["no-magic"]["inputs"]:
            if record["path"] == "docs/catalog.json":
                record["sha256"] = "0" * 64
        self.cohort.receipt.write_text(json.dumps(receipt))

        self.assert_fails_without_writing("no-magic:docs/catalog.json bytes differ")

    def test_unpublished_receipt_is_rejected(self) -> None:
        self.cohort.write_receipt(status="not-asserted")

        self.assert_fails_without_writing("publication must be published")

    def test_non_http_paper_url_is_rejected(self) -> None:
        self.cohort.cards[1]["url"] = "javascript://example.com/%0Aalert(1)"
        self.cohort.write(
            self.cohort.papers, "data/papers.json", self.cohort.papers_json()
        )
        self.cohort.commit(self.cohort.papers)
        self.cohort.write_receipt()

        self.assert_fails_without_writing("must be an absolute http(s) URL")

    def test_symlinked_linked_input_is_rejected(self) -> None:
        preview = self.cohort.viz / "previews" / "microalpha.gif"
        outside = self.cohort.base / "outside.gif"
        outside.write_bytes(GIF)
        preview.unlink()
        preview.symlink_to(outside)
        self.cohort.commit(self.cohort.viz)
        self.cohort.write_receipt()

        self.assert_fails_without_writing("has a symlinked component")

    def test_catalog_script_without_implementation_record_is_rejected(self) -> None:
        self.cohort.cards[1]["implementations"] = []
        self.cohort.write(
            self.cohort.papers, "data/papers.json", self.cohort.papers_json()
        )
        self.cohort.commit(self.cohort.papers)
        self.cohort.write_receipt()

        self.assert_fails_without_writing("beta_vs_gamma has no implementation record")

    def test_invalid_input_leaves_previous_outputs_untouched(self) -> None:
        self.assertEqual(self.cohort.run().returncode, 0)
        self.cohort.catalog[1]["teaching_kind"] = "benchmark"
        self.cohort.write(
            self.cohort.core,
            "docs/catalog.json",
            json.dumps(self.cohort.catalog).encode(),
        )
        self.cohort.commit(self.cohort.core)
        self.cohort.write_receipt()

        self.assert_fails_without_writing("teaching_kind 'benchmark' is not one of")


if __name__ == "__main__":
    unittest.main()
