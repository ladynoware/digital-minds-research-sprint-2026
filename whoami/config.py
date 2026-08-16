"""Config layer.

Two YAML files carry the entire experimental design:

* ``config/models.yaml``    — roster, tiers, pairings, condition rules, runtime
* ``config/questions.yaml`` — the instrument: verbatim prompts, flow, gates

Nothing in the code hardcodes a model, a family, a tier, a question, a prompt id
or a flow order. Both files are hashed on load so every run can be pinned to the
exact instrument that produced it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS_PATH = REPO_ROOT / "config" / "models.yaml"
DEFAULT_QUESTIONS_PATH = REPO_ROOT / "config" / "questions.yaml"

GATE_KINDS = {"consent", "detection", "fork", "preference"}
SIMPLE_ASK_IF = {"always", "swapped", "clean"}
ON_NO_VALUES = {"stop", "continue"}
ON_YES_VALUES = {"stop", "continue"}
ON_UNCLEAR_VALUES = {"pause", "record_null"}
UNCLEAR = "unclear"
SELECTION_RULES = {
    "none",
    # rev. 4 axes
    "same_tier_other_family",   # peer: capability tier held, family varies
    "same_family_other_tier",   # kin:  family held, tier varies
    "other_tier_other_family",  # far:  both vary
    # retained, still valid rules for future designs
    "same_family_other_model",
    "other_family_same_class",
    "other_class",
}


class ConfigError(ValueError):
    """Raised when a config file is malformed or internally inconsistent."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _label(value: Any) -> str:
    """Normalise a YAML scalar used as a gate label.

    YAML 1.1 turns bare ``yes``/``no`` into booleans, which would silently break
    every gate. Values are quoted in the config; this is the belt to that
    braces, so a forgotten pair of quotes cannot corrupt a measurement.
    """
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model: str
    family: str
    tier: str
    model_class: str
    display_name: str


@dataclass(frozen=True)
class Condition:
    name: str
    rule: str
    n_swaps: int | None = None  # None -> taken from swap_count_allocation


@dataclass
class RosterConfig:
    version: str
    models: list[ModelSpec]
    conditions: list[Condition]
    samples_per_cell: int
    swap_count_allocation: dict[int, int]
    pairings: dict[str, dict[str, str]]
    router: dict[str, Any]
    receipt: dict[str, Any]
    api: dict[str, Any]
    dry_run: dict[str, Any]
    source_path: Path
    sha256: str

    @property
    def by_key(self) -> dict[str, ModelSpec]:
        return {m.key: m for m in self.models}

    def get(self, key: str) -> ModelSpec:
        try:
            return self.by_key[key]
        except KeyError:
            raise ConfigError(f"unknown model key {key!r}") from None

    def condition(self, name: str) -> Condition:
        for c in self.conditions:
            if c.name == name:
                return c
        raise ConfigError(f"unknown condition {name!r}")

    def pairing_for(self, resident_key: str, condition: str) -> str | None:
        """Explicit understudy for a cell, or None if the table does not fix it."""
        entry = self.pairings.get(resident_key)
        if entry is None:
            return None
        return entry.get(condition)

    def has_pairings(self, resident_key: str) -> bool:
        return resident_key in self.pairings

    def with_dry_run_profile(self) -> "RosterConfig":
        """Copy whose roster/router come from the ``dry_run`` block.

        Condition rules, sampling and every other mechanism are untouched — the
        dry run exercises the same code path against free-tier models.
        """
        dr = self.dry_run or {}
        if not dr.get("roster"):
            raise ConfigError("models.yaml has no dry_run.roster block")
        models = [_parse_model(m, f"dry_run.roster[{i}]") for i, m in enumerate(dr["roster"])]
        return RosterConfig(
            version=f"{self.version}+dryrun",
            models=models,
            conditions=self.conditions,
            samples_per_cell=self.samples_per_cell,
            swap_count_allocation=self.swap_count_allocation,
            pairings={},  # pairings name real-roster keys; fall back to the rules
            router=dr.get("router", self.router),
            receipt=self.receipt,
            api=self.api,
            dry_run=dr,
            source_path=self.source_path,
            sha256=self.sha256,
        )


def _parse_model(raw: dict, where: str) -> ModelSpec:
    missing = {"key", "model", "family", "tier", "model_class"} - set(raw)
    if missing:
        raise ConfigError(f"{where}: missing keys {sorted(missing)}")
    if raw["model_class"] not in {"frontier", "open"}:
        raise ConfigError(
            f"{where}: model_class must be 'frontier' or 'open', got {raw['model_class']!r}"
        )
    return ModelSpec(
        key=raw["key"],
        model=raw["model"],
        family=raw["family"],
        tier=str(raw["tier"]),
        model_class=raw["model_class"],
        display_name=raw.get("display_name", raw["key"]),
    )


def load_roster(path: Path | str = DEFAULT_MODELS_PATH) -> RosterConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    models = [_parse_model(m, f"roster[{i}]") for i, m in enumerate(raw.get("roster", []))]
    if not models:
        raise ConfigError("roster is empty")
    keys = [m.key for m in models]
    if len(set(keys)) != len(keys):
        raise ConfigError("duplicate model keys in roster")

    conditions: list[Condition] = []
    for i, c in enumerate(raw.get("conditions", [])):
        if "name" not in c or "rule" not in c:
            raise ConfigError(f"conditions[{i}]: needs 'name' and 'rule'")
        if c["rule"] not in SELECTION_RULES:
            raise ConfigError(
                f"conditions[{i}]: unknown rule {c['rule']!r}; known: {sorted(SELECTION_RULES)}"
            )
        conditions.append(
            Condition(name=c["name"], rule=c["rule"], n_swaps=c.get("n_swaps"))
        )
    if not conditions:
        raise ConfigError("no conditions defined")
    cond_names = {c.name for c in conditions}

    pairings: dict[str, dict[str, str]] = {}
    for resident, entry in (raw.get("pairings") or {}).items():
        if resident not in keys:
            raise ConfigError(f"pairings: unknown resident {resident!r}")
        if not isinstance(entry, dict):
            raise ConfigError(f"pairings[{resident}]: expected a mapping of condition -> model")
        for condition, understudy in entry.items():
            if condition not in cond_names:
                raise ConfigError(f"pairings[{resident}]: unknown condition {condition!r}")
            if understudy not in keys:
                raise ConfigError(
                    f"pairings[{resident}][{condition}]: unknown model {understudy!r}"
                )
            if understudy == resident:
                raise ConfigError(
                    f"pairings[{resident}][{condition}]: a model cannot stand in for itself"
                )
        pairings[resident] = dict(entry)

    samples_per_cell = int(raw.get("samples_per_cell", 1))
    allocation = {
        int(k): int(v)
        for k, v in (raw.get("swap_count_allocation") or {1: samples_per_cell}).items()
    }
    if any(v < 0 for v in allocation.values()):
        raise ConfigError("swap_count_allocation counts must be non-negative")
    if sum(allocation.values()) != samples_per_cell:
        raise ConfigError(
            f"swap_count_allocation sums to {sum(allocation.values())} but samples_per_cell "
            f"is {samples_per_cell}; every sample in a cell must be allocated"
        )

    return RosterConfig(
        version=raw.get("version", "unversioned"),
        models=models,
        conditions=conditions,
        samples_per_cell=samples_per_cell,
        swap_count_allocation=allocation,
        pairings=pairings,
        router=raw.get("router") or {},
        receipt=raw.get("receipt") or {"mode": "prefix", "aliases": {}},
        api=raw.get("api") or {},
        dry_run=raw.get("dry_run") or {},
        source_path=path,
        sha256=_sha256(path),
    )


# ---------------------------------------------------------------------------
# ask_if predicate
# ---------------------------------------------------------------------------


def _validate_ask_if(node: Any, where: str) -> None:
    if isinstance(node, str):
        if node not in SIMPLE_ASK_IF:
            raise ConfigError(f"{where}: ask_if must be one of {sorted(SIMPLE_ASK_IF)}")
        return
    if not isinstance(node, dict):
        raise ConfigError(f"{where}: ask_if must be a string or a mapping")
    if "any" in node or "all" in node:
        for key in ("any", "all"):
            if key in node:
                if not isinstance(node[key], list) or not node[key]:
                    raise ConfigError(f"{where}: ask_if.{key} must be a non-empty list")
                for i, clause in enumerate(node[key]):
                    _validate_ask_if(clause, f"{where}.{key}[{i}]")
        return
    if "not" in node:
        _validate_ask_if(node["not"], f"{where}.not")
        return
    if "column" in node:
        if not ({"in", "not_in"} & set(node)):
            raise ConfigError(f"{where}: a column clause needs 'in' or 'not_in'")
        for key in ("in", "not_in"):
            if key in node and not isinstance(node[key], list):
                raise ConfigError(f"{where}.{key} must be a list")
        return
    raise ConfigError(f"{where}: unrecognised ask_if clause {node!r}")


def evaluate_ask_if(node: Any, thread: dict[str, Any]) -> bool:
    """Should this prompt be asked, given the thread's state so far?

    Clauses: ``always`` / ``swapped`` / ``clean``; ``{column, in|not_in}``;
    combined with ``any`` / ``all`` / ``not``. A NULL column never matches an
    ``in`` list, so a prompt gated on an unrecorded answer is asked rather than
    silently skipped.
    """
    if isinstance(node, str):
        if node == "always":
            return True
        swapped = int(thread.get("n_swaps") or 0) > 0
        return swapped if node == "swapped" else not swapped
    if "any" in node:
        return any(evaluate_ask_if(c, thread) for c in node["any"])
    if "all" in node:
        return all(evaluate_ask_if(c, thread) for c in node["all"])
    if "not" in node:
        return not evaluate_ask_if(node["not"], thread)
    value = thread.get(node["column"])
    value = None if value is None else _label(value)
    if "in" in node:
        return value is not None and value in [_label(v) for v in node["in"]]
    return value is None or value not in [_label(v) for v in node["not_in"]]


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str | None = None
    variants: dict[str, Any] | None = None
    swappable: bool = False
    blind: bool = False
    gate: str | None = None
    answers: tuple[str, ...] = ("yes", "no")
    records: str | None = None
    ask_if: Any = "always"
    on_no: str = "continue"
    on_yes: str = "continue"
    on_unclear: str = "pause"

    @property
    def is_gate(self) -> bool:
        """The three branching gates. ``preference`` records but never branches."""
        return self.gate in {"consent", "detection", "fork"}

    @property
    def allowed_labels(self) -> tuple[str, ...]:
        return (*self.answers, UNCLEAR)

    def render_for(self, thread: dict[str, Any]) -> str:
        """The text template for this thread, before interpolation."""
        if self.variants:
            key = _label(thread.get(self.variants["select_by"]))
            cases = self.variants["cases"]
            if key not in cases:
                raise ConfigError(
                    f"{self.id}: no variant for "
                    f"{self.variants['select_by']}={key!r}; have {sorted(cases)}"
                )
            return cases[key]
        return self.text or ""


@dataclass
class Instrument:
    version: str
    locked: bool
    flow: list[Prompt]
    system_prompt_default: str
    system_prompts_per_model: dict[str, str]
    router_prompts: dict[str, str]
    derivations: dict[str, str]
    dry_run_ambiguity_probe: str
    source_path: Path
    sha256: str

    @property
    def by_id(self) -> dict[str, Prompt]:
        return {p.id: p for p in self.flow}

    def prompt(self, prompt_id: str) -> Prompt:
        try:
            return self.by_id[prompt_id]
        except KeyError:
            raise ConfigError(f"unknown prompt_id {prompt_id!r}") from None

    @property
    def swap_pool(self) -> list[Prompt]:
        return [p for p in self.flow if p.swappable]

    @property
    def blind_prompt_ids(self) -> list[str]:
        return [p.id for p in self.flow if p.blind]

    def system_prompt_for(self, model: ModelSpec) -> str:
        raw = self.system_prompts_per_model.get(model.key, self.system_prompt_default)
        return raw.format(display_name=model.display_name, model=model.model).strip()


def load_instrument(path: Path | str = DEFAULT_QUESTIONS_PATH) -> Instrument:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    flow: list[Prompt] = []
    for i, p in enumerate(raw.get("flow", [])):
        where = f"flow[{i}]"
        if "id" not in p:
            raise ConfigError(f"{where}: needs 'id'")
        has_text, has_variants = "text" in p, "variants" in p
        if has_text == has_variants:
            raise ConfigError(f"{where}: needs exactly one of 'text' or 'variants'")
        variants = None
        if has_variants:
            v = p["variants"]
            if not isinstance(v, dict) or "select_by" not in v or "cases" not in v:
                raise ConfigError(f"{where}.variants: needs 'select_by' and 'cases'")
            if not v["cases"]:
                raise ConfigError(f"{where}.variants.cases is empty")
            variants = {
                "select_by": v["select_by"],
                "cases": {_label(k): str(t).rstrip() for k, t in v["cases"].items()},
            }

        gate = p.get("gate")
        if gate is not None and gate not in GATE_KINDS:
            raise ConfigError(f"{where}: unknown gate {gate!r}; known: {sorted(GATE_KINDS)}")
        answers = tuple(_label(a) for a in p.get("answers", ["yes", "no"]))
        if gate and not answers:
            raise ConfigError(f"{where}: a gate needs at least one valid answer")
        if UNCLEAR in answers:
            raise ConfigError(
                f"{where}: '{UNCLEAR}' is the parse-failure route, not a valid answer"
            )
        if not gate and "answers" in p:
            raise ConfigError(f"{where}: 'answers' is meaningless without 'gate'")

        _validate_ask_if(p.get("ask_if", "always"), where)
        for key, allowed in (
            ("on_no", ON_NO_VALUES),
            ("on_yes", ON_YES_VALUES),
            ("on_unclear", ON_UNCLEAR_VALUES),
        ):
            if p.get(key, next(iter(allowed))) not in allowed:
                raise ConfigError(f"{where}: {key} must be one of {sorted(allowed)}")
        if p.get("blind") and gate:
            raise ConfigError(f"{where}: a blind turn cannot also be a gate")
        if p.get("blind") and p.get("swappable"):
            raise ConfigError(f"{where}: a blind turn cannot be swappable")

        flow.append(
            Prompt(
                id=p["id"],
                text=p["text"].rstrip() if has_text else None,
                variants=variants,
                swappable=bool(p.get("swappable", False)),
                blind=bool(p.get("blind", False)),
                gate=gate,
                answers=answers,
                records=p.get("records"),
                ask_if=p.get("ask_if", "always"),
                on_no=p.get("on_no", "continue"),
                on_yes=p.get("on_yes", "continue"),
                on_unclear=p.get("on_unclear", "pause"),
            )
        )
    if not flow:
        raise ConfigError("instrument flow is empty")
    ids = [p.id for p in flow]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate prompt ids in flow")
    if not any(p.swappable for p in flow):
        raise ConfigError("no swappable prompts: swapped conditions would be impossible")

    sp = raw.get("system_prompts") or {}
    rp = raw.get("router_prompts") or {}
    for k in ("system", "user"):
        if k not in rp:
            raise ConfigError(f"router_prompts.{k} is required")

    return Instrument(
        version=raw.get("version", "unversioned"),
        locked=bool(raw.get("locked", False)),
        flow=flow,
        system_prompt_default=sp.get("default", "You are {display_name}."),
        system_prompts_per_model=sp.get("per_model") or {},
        router_prompts=rp,
        derivations=raw.get("derivations") or {},
        dry_run_ambiguity_probe=raw.get("dry_run_ambiguity_probe", ""),
        source_path=path,
        sha256=_sha256(path),
    )


@dataclass
class Config:
    roster: RosterConfig
    instrument: Instrument

    @property
    def fingerprint(self) -> dict[str, str]:
        """Everything needed to pin a run to the exact instrument that made it."""
        return {
            "roster_version": self.roster.version,
            "roster_sha256": self.roster.sha256,
            "instrument_version": self.instrument.version,
            "instrument_sha256": self.instrument.sha256,
            "instrument_locked": str(self.instrument.locked),
        }


def load(
    models_path: Path | str = DEFAULT_MODELS_PATH,
    questions_path: Path | str = DEFAULT_QUESTIONS_PATH,
    dry_run: bool = False,
) -> Config:
    roster = load_roster(models_path)
    if dry_run:
        roster = roster.with_dry_run_profile()
    return Config(roster=roster, instrument=load_instrument(questions_path))
