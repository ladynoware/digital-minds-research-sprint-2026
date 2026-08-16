"""Codebooks are instruments, and are frozen like one.

A codebook is the measuring device for one question: 5–9 codes, each with a
name, a one-sentence definition and two verbatim examples drawn from the actual
replies. It is written by a human-in-the-loop pass over the whole corpus, then
**reviewed and approved by the researcher** before a single reply is tagged
against it.

Freezing works the same way ``questions.yaml`` does. ``approve`` computes a
SHA-256 over the codebook's canonical content and writes it back as
``approved_hash``; the tagger recomputes that hash and refuses to run when it
does not match. The hash covers ``approved: true`` itself, so reverting
approval, renaming a code, sharpening a definition or swapping an example all
invalidate it — which is the point. Editing after approval is not forbidden,
it is just not silent: re-approve and the manifest records that a second
version of the instrument existed and when.

``analysis/codebook_manifest.jsonl`` is the append-only record of every
approval, mirroring ``data/run_manifest.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEBOOK_DIR = Path(__file__).resolve().parent / "codebooks"
MANIFEST = Path(__file__).resolve().parent / "codebook_manifest.jsonl"

# Fields that carry the approval stamp itself, and so cannot be inside the hash
# it stamps.
_UNHASHED = ("approved_hash", "approved_at")

MIN_CODES = 5
MAX_CODES = 9
OTHER_CODE = "other"
# Above this share, `other` is not a residual category — it is the sign of a
# codebook that failed to see a real pattern in the data.
OTHER_CEILING = 0.10


class CodebookError(ValueError):
    pass


@dataclass(frozen=True)
class Code:
    name: str
    definition: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class Codebook:
    prompt_id: str
    title: str
    unit: str
    codes: tuple[Code, ...]
    rules: tuple[str, ...]
    approved: bool
    approved_hash: str | None
    approved_at: str | None
    path: Path
    raw: dict[str, Any]

    @property
    def code_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.codes)

    @property
    def content_hash(self) -> str:
        return content_hash(self.raw)


def content_hash(raw: dict[str, Any]) -> str:
    """SHA-256 over the codebook's content, excluding the stamp fields.

    Canonicalised through JSON with sorted keys so that reordering YAML keys or
    reflowing a block scalar does not read as a different instrument, while any
    change to a name, definition, example or rule does.
    """
    body = {k: v for k, v in raw.items() if k not in _UNHASHED}
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def path_for(prompt_id: str) -> Path:
    return CODEBOOK_DIR / f"{prompt_id}.yaml"


def parse(raw: dict[str, Any], path: Path) -> Codebook:
    def need(key: str) -> Any:
        if key not in raw:
            raise CodebookError(f"{path.name}: missing `{key}`")
        return raw[key]

    codes_raw = need("codes")
    if not isinstance(codes_raw, list) or not codes_raw:
        raise CodebookError(f"{path.name}: `codes` must be a non-empty list")

    codes: list[Code] = []
    seen: set[str] = set()
    for i, c in enumerate(codes_raw):
        where = f"{path.name}: codes[{i}]"
        if not isinstance(c, dict):
            raise CodebookError(f"{where} is not a mapping")
        for key in ("name", "definition", "examples"):
            if not c.get(key):
                raise CodebookError(f"{where} has no `{key}`")
        name = str(c["name"]).strip()
        if name != name.lower().replace(" ", "-").replace("_", "-"):
            raise CodebookError(f"{where}: `{name}` should be lower-case-kebab")
        if name in seen:
            raise CodebookError(f"{where}: duplicate code `{name}`")
        seen.add(name)
        examples = [str(e).strip() for e in c["examples"] if str(e).strip()]
        if len(examples) < 2:
            raise CodebookError(f"{where}: needs 2 verbatim examples, has {len(examples)}")
        codes.append(Code(name, str(c["definition"]).strip(), tuple(examples)))

    substantive = [c for c in codes if c.name != OTHER_CODE]
    if not MIN_CODES <= len(substantive) <= MAX_CODES:
        raise CodebookError(
            f"{path.name}: {len(substantive)} substantive codes; the brief asks for "
            f"{MIN_CODES}–{MAX_CODES} (`{OTHER_CODE}` is not counted)"
        )
    if OTHER_CODE not in seen:
        raise CodebookError(f"{path.name}: no `{OTHER_CODE}` code — every codebook needs one")

    return Codebook(
        prompt_id=str(need("prompt_id")),
        title=str(need("title")),
        unit=str(raw.get("unit", "one reply")),
        codes=tuple(codes),
        rules=tuple(str(r).strip() for r in need("rules")),
        approved=bool(raw.get("approved", False)),
        approved_hash=raw.get("approved_hash"),
        approved_at=raw.get("approved_at"),
        path=path,
        raw=raw,
    )


def load(prompt_id: str) -> Codebook:
    path = path_for(prompt_id)
    if not path.exists():
        raise CodebookError(f"no codebook at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CodebookError(f"{path.name}: not a YAML mapping")
    book = parse(raw, path)
    if book.prompt_id != prompt_id:
        raise CodebookError(f"{path.name}: declares prompt_id `{book.prompt_id}`")
    return book


def load_frozen(prompt_id: str) -> Codebook:
    """Load a codebook that is cleared for tagging, or explain why it is not."""
    book = load(prompt_id)
    if not book.approved:
        raise CodebookError(
            f"{book.path.name} is not approved. Nothing is tagged against an "
            "unreviewed codebook — the researcher sets `approved: true`, then\n"
            f"    python -m analysis approve {prompt_id}"
        )
    if not book.approved_hash:
        raise CodebookError(
            f"{book.path.name} is approved but not stamped. Run:\n"
            f"    python -m analysis approve {prompt_id}"
        )
    if book.approved_hash != book.content_hash:
        raise CodebookError(
            f"{book.path.name} was edited after approval.\n"
            f"  stamped: {book.approved_hash}\n"
            f"  now:     {book.content_hash}\n"
            "Tagging against a changed instrument would make the existing rows "
            "uncomparable. Review the change, then re-approve to record the new "
            "version:\n"
            f"    python -m analysis approve {prompt_id}"
        )
    return book


def approve(prompt_id: str) -> Codebook:
    """Stamp an approved codebook and record the approval in the manifest."""
    book = load(prompt_id)
    if not book.approved:
        raise CodebookError(
            f"{book.path.name} still says `approved: false`. The researcher sets "
            "that after review; this command only stamps what is already approved."
        )
    stamp = book.content_hash
    text = book.path.read_text(encoding="utf-8")
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [ln for ln in text.splitlines() if not ln.startswith(("approved_hash:", "approved_at:"))]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += [f"approved_hash: {stamp}", f"approved_at: {when}"]
    book.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "prompt_id": prompt_id,
                    "codebook_hash": stamp,
                    "codes": list(book.code_names),
                    "approved_at": when,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return load(prompt_id)


def response_schema(book: Codebook) -> dict:
    """Structured-output schema for one codebook.

    Built per codebook rather than fixed in code, for the same reason the gate
    router builds its schema per gate: the valid label set is a property of the
    instrument, not of the software.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "reply_coding",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "codes": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "enum": list(book.code_names)},
                    },
                    "flagged_quote": {"type": "string"},
                    "notable": {"type": "boolean"},
                },
                "required": ["codes", "flagged_quote", "notable"],
                "additionalProperties": False,
            },
        },
    }
