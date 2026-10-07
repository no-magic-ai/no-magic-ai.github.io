#!/usr/bin/env python3
"""Build the website content snapshot from one published no-magic cohort.

The snapshot joins three repositories' committed, published metadata:

  no-magic         docs/catalog.json (one record per algorithm script)
  no-magic-papers  data/papers.json (generated projection of every paper card)
  no-magic-viz     the scenes and GIF previews the cards declare

The cohort is fixed by a trusted schema-version-1 receipt that
no-magic-papers' scripts/validate_invariants.py emitted for a published
cohort. Every repository root must be a clean Git worktree whose origin,
HEAD commit and tree equal the receipt, and every file this script reads or
links must be a receipt input whose bytes, SHA-256 and committed blob match.
Nothing is fetched, no sibling code is imported or executed, and any failure
stops before an output file is touched.

Outputs, relative to this website repository:

  data/content.json  algorithms and papers, with source, card, lesson and
                     preview URLs pinned to the cohort commits
  data/cohort.json   the receipt, byte for byte

Usage:
    python scripts/build_content.py --core-root ../no-magic \\
        --papers-root ../no-magic-papers --viz-root ../no-magic-viz \\
        --receipt cohort-receipt.json [--check]

--check regenerates in memory and exits 1 unless both outputs exist with
exactly those bytes; it never writes.

Exit codes: 0 success, 1 invalid input or stale output, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SITE_ROOT = Path(__file__).resolve().parents[1]
CONTENT_PATH = "data/content.json"
COHORT_PATH = "data/cohort.json"
SCHEMA_VERSION = 1
ORG = "no-magic-ai"
CORE, PAPERS, VIZ = "no-magic", "no-magic-papers", "no-magic-viz"
REPOSITORIES = (CORE, PAPERS, VIZ)
REQUIRED_INVARIANTS = frozenset(
    {
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
    }
)
TIERS = ("01-foundations", "02-alignment", "03-systems", "04-agents")
TEACHING_KINDS = ("train_infer", "comparison", "forward_pass", "algorithm_demo")
DATA_SOURCES = ("names_download", "in_script")
THEMES = (
    "efficient-inference",
    "long-context",
    "alignment",
    "reasoning",
    "architecture",
    "training-dynamics",
    "parameter-efficient",
    "interpretability",
    "retrieval",
    "safety-robustness",
    "agents",
    "multimodal",
)
PAPER_STATUSES = (
    "triaged",
    "summarized",
    "backlog-implement",
    "implemented",
    "deprecated",
    "replaced",
    "archived",
    "reference-only",
)
LESSON_STATUSES = ("none", "planned", "drafted", "published")
CATALOG_KEYS = frozenset(
    {
        "tier",
        "name",
        "display",
        "thesis",
        "lines",
        "paper_slug",
        "teaching_kind",
        "data_source",
        "adaptation_note",
    }
)
FRONTMATTER_KEYS = frozenset(
    {
        "slug",
        "title",
        "authors",
        "venue",
        "year",
        "arxiv_id",
        "doi",
        "url",
        "discovered_via",
        "discovered_date",
        "status",
        "themes",
        "tags",
        "routing",
        "implementations",
        "lesson",
        "dependencies_on_other_papers",
    }
)
ROUTING_KEYS = frozenset(
    {
        "decision",
        "target_repo",
        "target_script_slug",
        "target_path",
        "target_tier",
        "batch_label",
        "review_date",
    }
)
IMPLEMENTATION_KEYS = frozenset(
    {
        "repo",
        "path",
        "script_slug",
        "commit",
        "release",
        "media_repo",
        "media_status",
        "scene_path",
        "preview_path",
        "media_note",
    }
)
OID = {"sha1": re.compile(r"^[0-9a-f]{40}$"), "sha256": re.compile(r"^[0-9a-f]{64}$")}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SCRIPT_SLUG = re.compile(r"^[a-z0-9_]+$")
YEAR = re.compile(r"^[0-9]{4}$")
DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
RELEASE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
LESSON_PREFIX = f"{PAPERS}/"

Json = object
JsonObject = dict[str, Json]


class BuildError(Exception):
    """An input is missing, unpinned, malformed or inconsistent with the receipt."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BuildError(message)


def obj(value: Json, where: str, keys: frozenset[str] | None = None) -> JsonObject:
    require(isinstance(value, dict), f"{where} must be an object")
    assert isinstance(value, dict)
    if keys is not None:
        found = frozenset(value)
        require(
            found == keys,
            f"{where} keys must be exactly {sorted(keys)}; got {sorted(found)}",
        )
    return value


def text(value: Json, where: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    require(
        isinstance(value, str) and bool(value) and value == value.strip(),
        f"{where} must be a non-empty trimmed string{' or null' if nullable else ''}",
    )
    assert isinstance(value, str)
    require(
        not any(ord(ch) < 32 or ord(ch) == 127 for ch in value),
        f"{where} contains a control character",
    )
    return value


def string(value: Json, where: str) -> str:
    result = text(value, where)
    assert result is not None
    return result


def strings(value: Json, where: str, *, non_empty: bool) -> list[str]:
    require(isinstance(value, list), f"{where} must be a list")
    assert isinstance(value, list)
    require(bool(value) or not non_empty, f"{where} must not be empty")
    return [string(item, f"{where}[{index}]") for index, item in enumerate(value)]


def member(value: Json, allowed: tuple[str, ...], where: str) -> str:
    result = string(value, where)
    require(result in allowed, f"{where} {result!r} is not one of {list(allowed)}")
    return result


def matching(value: Json, pattern: re.Pattern[str], where: str) -> str:
    result = string(value, where)
    require(bool(pattern.match(result)), f"{where} {result!r} is malformed")
    return result


def safe_relative_path(value: Json, where: str) -> str:
    path = string(value, where)
    parts = path.split("/")
    require(
        "\\" not in path
        and ":" not in path
        and not path.startswith("/")
        and all(part not in {"", ".", ".."} for part in parts),
        f"{where} {path!r} must be a plain repository-relative POSIX path",
    )
    return path


def http_url(value: Json, where: str) -> str:
    """Accept only an absolute http(s) URL with a host and no credentials."""
    url = string(value, where)
    parts = urlsplit(url)
    require(
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not any(ch.isspace() for ch in url),
        f"{where} {url!r} must be an absolute http(s) URL without credentials",
    )
    return url


def git(root: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise BuildError(f"{root}: git {' '.join(args)} failed: {detail}")
    return proc.stdout


def blob_oid(data: bytes, object_format: str) -> str:
    digest = hashlib.sha256() if object_format == "sha256" else hashlib.sha1()
    digest.update(f"blob {len(data)}\0".encode() + data)
    return digest.hexdigest()


def normalized_remote(url: str) -> str:
    value = url.strip()
    for prefix in ("https://github.com/", "ssh://git@github.com/", "git@github.com:"):
        if value.lower().startswith(prefix):
            return value[len(prefix) :].rstrip("/").removesuffix(".git").lower()
    return ""


@dataclass
class Source:
    """One receipt-pinned repository: identity, commit and the inputs it may serve."""

    name: str
    root: Path
    commit: str
    tree: str
    object_format: str
    inputs: dict[str, tuple[str, str]]
    committed: dict[str, tuple[str, str]] = field(default_factory=dict)

    def open(self) -> None:
        require(self.root.is_dir(), f"{self.name} root {self.root} is not a directory")
        top = Path(os.fsdecode(git(self.root, "rev-parse", "--show-toplevel").strip()))
        require(
            top.resolve() == self.root.resolve(),
            f"{self.name} root {self.root} is not its Git worktree top level ({top})",
        )
        origin = git(self.root, "config", "--get", "remote.origin.url").decode()
        require(
            normalized_remote(origin) == f"{ORG}/{self.name}",
            f"{self.name} root origin {origin.strip()!r} is not {ORG}/{self.name}",
        )
        actual = git(self.root, "rev-parse", "HEAD^{commit}", "HEAD^{tree}").split()
        require(
            [oid.decode() for oid in actual] == [self.commit, self.tree],
            f"{self.name} HEAD is not the receipt commit {self.commit} / tree {self.tree}",
        )
        fmt = git(self.root, "rev-parse", "--show-object-format").decode().strip()
        require(fmt == self.object_format, f"{self.name} object format is {fmt}")
        dirty = git(self.root, "status", "--porcelain=v1", "--untracked-files=no")
        require(not dirty, f"{self.name} root {self.root} has uncommitted changes")
        listing = git(self.root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
        for record in listing.split(b"\0"):
            if record:
                meta, _, path = record.partition(b"\t")
                mode, kind, oid = meta.decode().split(" ")
                self.committed[os.fsdecode(path)] = (mode, f"{kind}:{oid}")

    def read(self, relative: str) -> bytes:
        """Return the bytes of a receipt input after binding them to the commit."""
        pinned = self.inputs.get(relative)
        require(
            pinned is not None,
            f"{self.name}:{relative} is not an input of the receipt",
        )
        assert pinned is not None
        current = self.root
        for part in relative.split("/"):
            current = current / part
            require(
                not current.is_symlink(),
                f"{self.name}:{relative} has a symlinked component {part!r}",
            )
        require(current.is_file(), f"{self.name}:{relative} is not a regular file")
        entry = self.committed.get(relative)
        digest, blob = pinned
        require(
            entry is not None
            and entry[0] in {"100644", "100755"}
            and entry[1] == f"blob:{blob}",
            f"{self.name}:{relative} is not the receipt blob {blob} at {self.commit}",
        )
        data = current.read_bytes()
        require(
            hashlib.sha256(data).hexdigest() == digest
            and blob_oid(data, self.object_format) == blob,
            f"{self.name}:{relative} bytes differ from the receipt",
        )
        return data

    def blob_url(self, relative: str) -> str:
        return f"https://github.com/{ORG}/{self.name}/blob/{self.commit}/{relative}"

    def raw_url(self, relative: str) -> str:
        return f"https://raw.githubusercontent.com/{ORG}/{self.name}/{self.commit}/{relative}"


def load_receipt(raw: bytes, roots: dict[str, Path]) -> dict[str, Source]:
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"receipt is not UTF-8 JSON: {exc}") from exc
    receipt = obj(receipt, "receipt")
    version = receipt.get("schema_version")
    require(
        type(version) is int and version == 1,
        "receipt schema_version must be 1",
    )
    require(receipt.get("cohort") == "published", "receipt cohort must be published")
    invariants = set(
        strings(receipt.get("invariants"), "receipt invariants", non_empty=True)
    )
    missing = sorted(REQUIRED_INVARIANTS - invariants)
    require(not missing, f"receipt lacks required invariants {missing}")
    repositories = obj(
        receipt.get("repositories"), "receipt repositories", frozenset(REPOSITORIES)
    )
    return {
        name: receipt_source(name, repositories[name], roots[name])
        for name in REPOSITORIES
    }


def receipt_source(name: str, value: Json, root: Path) -> Source:
    where = f"receipt repositories.{name}"
    entry = obj(value, where)
    require(entry.get("identity") == f"{ORG}/{name}", f"{where}.identity is wrong")
    fmt = member(entry.get("object_format"), tuple(OID), f"{where}.object_format")
    commit = matching(entry.get("commit"), OID[fmt], f"{where}.commit")
    tree = matching(entry.get("tree"), OID[fmt], f"{where}.tree")
    publication = obj(entry.get("publication"), f"{where}.publication")
    require(
        publication.get("status") == "published"
        and publication.get("ref") == "refs/heads/main"
        and publication.get("provider") == f"https://github.com/{ORG}/{name}.git",
        f"{where}.publication must be published from refs/heads/main of {ORG}/{name}",
    )
    matching(
        publication.get("main_commit"), OID[fmt], f"{where}.publication.main_commit"
    )
    require(isinstance(entry.get("inputs"), list), f"{where}.inputs must be a list")
    records = entry["inputs"]
    assert isinstance(records, list)
    inputs: dict[str, tuple[str, str]] = {}
    keys = frozenset({"path", "sha256", "blob"})
    for index, item in enumerate(records):
        record = obj(item, f"{where}.inputs[{index}]", keys)
        path = safe_relative_path(record["path"], f"{where}.inputs[{index}].path")
        require(path not in inputs, f"{where} lists input {path} twice")
        inputs[path] = (
            matching(record["sha256"], SHA256, f"{where}.inputs[{index}].sha256"),
            matching(record["blob"], OID[fmt], f"{where}.inputs[{index}].blob"),
        )
    return Source(name, root, commit, tree, fmt, inputs)


def parse_json(data: bytes, where: str) -> Json:
    try:
        parsed: Json = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"{where} is not UTF-8 JSON: {exc}") from exc
    return parsed


def load_catalog(core: Source) -> list[JsonObject]:
    raw = parse_json(core.read("docs/catalog.json"), "no-magic docs/catalog.json")
    require(
        isinstance(raw, list) and bool(raw), "catalog.json must be a non-empty list"
    )
    assert isinstance(raw, list)
    entries: list[JsonObject] = []
    for index, item in enumerate(raw):
        where = f"catalog.json[{index}]"
        entry = obj(item, where, CATALOG_KEYS)
        lines = entry["lines"]
        require(
            type(lines) is int and lines > 0,
            f"{where}.lines must be a positive integer",
        )
        entries.append(
            {
                "tier": member(entry["tier"], TIERS, f"{where}.tier"),
                "name": matching(entry["name"], SCRIPT_SLUG, f"{where}.name"),
                "display": string(entry["display"], f"{where}.display"),
                "thesis": string(entry["thesis"], f"{where}.thesis"),
                "lines": lines,
                "paper_slug": matching(
                    entry["paper_slug"], SLUG, f"{where}.paper_slug"
                ),
                "teaching_kind": member(
                    entry["teaching_kind"], TEACHING_KINDS, f"{where}.teaching_kind"
                ),
                "data_source": member(
                    entry["data_source"], DATA_SOURCES, f"{where}.data_source"
                ),
                "adaptation_note": text(
                    entry["adaptation_note"], f"{where}.adaptation_note", nullable=True
                ),
            }
        )
    names = [entry["name"] for entry in entries]
    require(len(set(names)) == len(names), "catalog.json repeats a script name")
    return entries


def load_cards(papers: Source) -> list[JsonObject]:
    raw = obj(
        parse_json(papers.read("data/papers.json"), "no-magic-papers data/papers.json"),
        "papers.json",
        frozenset({"schema_version", "papers"}),
    )
    version = raw["schema_version"]
    require(
        type(version) is int and version == 1, "papers.json schema_version must be 1"
    )
    require(isinstance(raw["papers"], list), "papers.json papers must be a list")
    records = raw["papers"]
    assert isinstance(records, list)
    cards = []
    for index, item in enumerate(records):
        record = obj(
            item,
            f"papers.json papers[{index}]",
            frozenset({"card_path", "frontmatter"}),
        )
        cards.append(card_fields(record, index))
    slugs = [card["slug"] for card in cards]
    require(len(set(slugs)) == len(slugs), "papers.json repeats a paper slug")
    return cards


def card_fields(record: JsonObject, index: int) -> JsonObject:
    fields = obj(
        record["frontmatter"], f"papers[{index}].frontmatter", FRONTMATTER_KEYS
    )
    slug = matching(fields["slug"], SLUG, f"papers[{index}].slug")
    where = f"paper {slug}"
    require(
        record["card_path"] == f"papers/{slug}.md",
        f"{where} card_path must be papers/{slug}.md",
    )
    themes = obj(
        fields["themes"], f"{where} themes", frozenset({"primary", "secondary"})
    )
    secondary = strings(
        themes["secondary"], f"{where} themes.secondary", non_empty=False
    )
    require(
        len(secondary) <= 2 and all(theme in THEMES for theme in secondary),
        f"{where} themes.secondary must hold zero to two known themes",
    )
    routing = obj(fields["routing"], f"{where} routing", ROUTING_KEYS)
    lesson = obj(fields["lesson"], f"{where} lesson", frozenset({"path", "status"}))
    dependencies = fields["dependencies_on_other_papers"]
    require(isinstance(dependencies, list), f"{where} dependencies must be a list")
    assert isinstance(dependencies, list)
    implementations = fields["implementations"]
    require(
        isinstance(implementations, list), f"{where} implementations must be a list"
    )
    assert isinstance(implementations, list)
    return {
        "slug": slug,
        "title": string(fields["title"], f"{where} title"),
        "authors": strings(fields["authors"], f"{where} authors", non_empty=True),
        "venue": string(fields["venue"], f"{where} venue"),
        "year": matching(fields["year"], YEAR, f"{where} year"),
        "arxiv_id": text(fields["arxiv_id"], f"{where} arxiv_id", nullable=True),
        "doi": text(fields["doi"], f"{where} doi", nullable=True),
        "url": http_url(fields["url"], f"{where} url"),
        "discovered_via": string(fields["discovered_via"], f"{where} discovered_via"),
        "discovered_date": matching(
            fields["discovered_date"], DATE, f"{where} discovered_date"
        ),
        "status": member(fields["status"], PAPER_STATUSES, f"{where} status"),
        "themes": {
            "primary": member(themes["primary"], THEMES, f"{where} themes.primary"),
            "secondary": secondary,
        },
        "tags": strings(fields["tags"], f"{where} tags", non_empty=False),
        "routing": {
            key: text(routing[key], f"{where} routing.{key}", nullable=True)
            for key in sorted(ROUTING_KEYS)
        },
        "lesson": {
            "status": None
            if lesson["status"] is None
            else member(lesson["status"], LESSON_STATUSES, f"{where} lesson.status"),
            "path": text(lesson["path"], f"{where} lesson.path", nullable=True),
        },
        "dependencies": [
            matching(
                obj(dep, f"{where} dependency", frozenset({"slug"}))["slug"],
                SLUG,
                f"{where} dependency slug",
            )
            for dep in dependencies
        ],
        "implementations": [
            implementation(item, f"{where} implementations[{position}]")
            for position, item in enumerate(implementations)
        ],
    }


def implementation(value: Json, where: str) -> JsonObject:
    record = obj(value, where, IMPLEMENTATION_KEYS)
    require(record["repo"] == CORE, f"{where}.repo must be {CORE}")
    status = member(
        record["media_status"], ("linked", "omitted"), f"{where}.media_status"
    )
    commit = record["commit"]
    release = record["release"]
    return {
        "script_slug": matching(
            record["script_slug"], SCRIPT_SLUG, f"{where}.script_slug"
        ),
        "path": safe_relative_path(record["path"], f"{where}.path"),
        "commit": None
        if commit is None
        else matching(commit, OID["sha1"], f"{where}.commit"),
        "release": None
        if release is None
        else matching(release, RELEASE, f"{where}.release"),
        "media_status": status,
        "media_repo": text(record["media_repo"], f"{where}.media_repo", nullable=True),
        "scene_path": text(record["scene_path"], f"{where}.scene_path", nullable=True),
        "preview_path": text(
            record["preview_path"], f"{where}.preview_path", nullable=True
        ),
        "media_note": text(record["media_note"], f"{where}.media_note", nullable=True),
    }


def media(record: JsonObject, viz: Source, where: str) -> JsonObject:
    note = record["media_note"]
    if record["media_status"] == "omitted":
        require(
            record["media_repo"] is None
            and record["scene_path"] is None
            and record["preview_path"] is None
            and note is not None,
            f"{where} omitted media must declare only a note",
        )
        return {
            "status": "omitted",
            "note": note,
            "preview_url": None,
            "scene_url": None,
        }
    name = record["script_slug"]
    scene = f"scenes/scene_{name}.py"
    preview = f"previews/{name}.gif"
    require(
        record["media_repo"] == VIZ
        and record["scene_path"] == scene
        and record["preview_path"] == preview,
        f"{where} linked media must name {VIZ} {scene} and {preview}",
    )
    viz.read(scene)
    require(
        viz.read(preview)[:6] in {b"GIF87a", b"GIF89a"},
        f"{viz.name}:{preview} is not a GIF",
    )
    return {
        "status": "linked",
        "note": note,
        "preview_url": viz.raw_url(preview),
        "scene_url": viz.blob_url(scene),
    }


def build(sources: dict[str, Source], receipt: bytes) -> bytes:
    core, papers, viz = sources[CORE], sources[PAPERS], sources[VIZ]
    catalog = load_catalog(core)
    cards = load_cards(papers)
    by_slug = {card["slug"]: card for card in cards}
    owners: dict[str, tuple[JsonObject, JsonObject]] = {}
    for card in cards:
        for record in as_records(card["implementations"]):
            name = str(record["script_slug"])
            require(
                name not in owners,
                f"script {name} has more than one implementation record",
            )
            owners[name] = (card, record)
        for dependency in as_strings(card["dependencies"]):
            require(
                dependency in by_slug,
                f"paper {card['slug']} depends on unknown {dependency}",
            )
    names = {str(entry["name"]) for entry in catalog}
    unknown = sorted(set(owners) - names)
    require(
        not unknown,
        f"implementation records name scripts missing from catalog.json: {unknown}",
    )
    algorithms = [algorithm(entry, owners, sources) for entry in catalog]
    document = {
        "schema_version": SCHEMA_VERSION,
        "cohort": {
            "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
            "repositories": {
                source.name: {"commit": source.commit, "tree": source.tree}
                for source in (core, papers, viz)
            },
            "inputs": {
                CORE: input_binding(core, "docs/catalog.json"),
                PAPERS: input_binding(papers, "data/papers.json"),
            },
        },
        "algorithms": sorted(
            algorithms, key=lambda item: (str(item["tier"]), str(item["name"]))
        ),
        "papers": [
            paper(card, papers) for card in sorted(cards, key=lambda c: str(c["slug"]))
        ],
    }
    rendered = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    return f"{rendered}\n".encode()


def as_records(value: Json) -> list[JsonObject]:
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, dict)]


def as_strings(value: Json) -> list[str]:
    assert isinstance(value, list)
    return [item for item in value if isinstance(item, str)]


def input_binding(source: Source, relative: str) -> JsonObject:
    digest, blob = source.inputs[relative]
    return {"path": relative, "sha256": digest, "blob": blob}


def algorithm(
    entry: JsonObject,
    owners: dict[str, tuple[JsonObject, JsonObject]],
    sources: dict[str, Source],
) -> JsonObject:
    name, tier = str(entry["name"]), str(entry["tier"])
    owner = owners.get(name)
    require(owner is not None, f"catalog script {name} has no implementation record")
    assert owner is not None
    card, record = owner
    where = f"paper {card['slug']} implementation {name}"
    require(
        card["slug"] == entry["paper_slug"],
        f"catalog script {name} names paper {entry['paper_slug']} but {card['slug']} owns it",
    )
    path = f"{tier}/{name}.py"
    require(record["path"] == path, f"{where} path must be {path}")
    sources[CORE].read(path)
    return {
        **entry,
        "source_path": path,
        "source_url": sources[CORE].blob_url(path),
        "declared_commit": record["commit"],
        "declared_release": record["release"],
        "media": media(record, sources[VIZ], where),
    }


def paper(card: JsonObject, papers: Source) -> JsonObject:
    slug = str(card["slug"])
    card_path = f"papers/{slug}.md"
    papers.read(card_path)
    lesson = card["lesson"]
    assert isinstance(lesson, dict)
    lesson_url = None
    if lesson["status"] in {"drafted", "published"}:
        expected = f"{LESSON_PREFIX}lessons/{slug}.md"
        require(
            lesson["path"] == expected, f"paper {slug} lesson.path must be {expected}"
        )
        relative = expected.removeprefix(LESSON_PREFIX)
        papers.read(relative)
        lesson_url = papers.blob_url(relative)
    return {
        **{key: value for key, value in card.items() if key != "implementations"},
        "lesson": {**lesson, "url": lesson_url},
        "card_path": card_path,
        "card_url": papers.blob_url(card_path),
        "implementations": sorted(
            str(record["script_slug"]) for record in as_records(card["implementations"])
        ),
    }


def output_file(relative: str) -> Path:
    current = SITE_ROOT
    for part in relative.split("/"):
        current = current / part
        require(not current.is_symlink(), f"{relative}: {part!r} is a symlink")
    require(
        not current.exists() or current.is_file(), f"{relative} is not a regular file"
    )
    return current


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build data/content.json and data/cohort.json from a published cohort."
    )
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--papers-root", type=Path, required=True)
    parser.add_argument("--viz-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    roots = {CORE: args.core_root, PAPERS: args.papers_root, VIZ: args.viz_root}
    try:
        require(args.receipt.is_file(), f"receipt {args.receipt} is not a file")
        receipt = args.receipt.read_bytes()
        sources = load_receipt(receipt, roots)
        for source in sources.values():
            source.open()
        outputs = {CONTENT_PATH: build(sources, receipt), COHORT_PATH: receipt}
        targets = {relative: output_file(relative) for relative in outputs}
    except BuildError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    if args.check:
        stale = [
            relative
            for relative, data in outputs.items()
            if not targets[relative].is_file() or targets[relative].read_bytes() != data
        ]
        if stale:
            print(
                f"stale: {', '.join(stale)}; rerun scripts/build_content.py without --check",
                file=sys.stderr,
            )
            return 1
        return 0
    (SITE_ROOT / "data").mkdir(exist_ok=True)
    for relative, data in outputs.items():
        targets[relative].write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
