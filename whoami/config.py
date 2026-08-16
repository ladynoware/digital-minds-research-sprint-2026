"""Config layer.

Two YAML files carry the entire experimental design:

* ``config/models.yaml``    — roster, condition rules, API/runtime settings
* ``config/questions.yaml`` — the instrument: verbatim prompts, flow, gates

Nothing in the code hardcodes a model, a family, a question, or a flow order.
Both files are hashed on load so every run can be pinned to the exact
instrument that produced it.
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
ASK_IF_VALUES = {"always", "swapped", "clean"}
ON_NO_VALUES = {"stop", "continue"}
ON_UNCLEAR_VALUES = {"pause", "record_null"}
SELECTION_RULES = {
    "none",
    "same_family_other_model",
    "other_family_same_class",
    "other_class",
}


class ConfigError(ValueError):
    """Raised when a config file is malformed or internally inconsistent."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model: str
    family: str
    model_class: str
    display_name: str


@dataclass(frozen=True)
class Fallback:
    rule: str
    label: str | None = None


@dataclass(frozen=True)
class Condition:
    name: str
    rule: str
    n_swaps: int | None = None  # None -> drawn from swap_count_weights
    fallback: Fallback | None = None


@dataclass(frozen=True)
class CellOverride:
    resident: str
    condition: str
    understudy: str | None
    label: str | None = None


@dataclass
class RosterConfig:
    version: str
    models: list[ModelSpec]
    conditions: list[Condition]
    samples_per_cell: int
    # {n_swaps: how many of a cell's samples get exactly that many swapped turns}
    swap_count_allocation: dict[int, int]
    cell_overrides: list[CellOverride]
    router: dict[str, Any]
    receipt: dict[str, Any]
    api: dict[str, Any]
    dry_run: dict[str, Any]
    source_path: Path
    sha256: str

    # -- lookups ----------------------------------------------------------
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

    def override_for(self, resident: str, condition: str) -> CellOverride | None:
        for o in self.cell_overrides:
            if o.resident == resident and o.condition == condition:
                return o
        return None

    def with_dry_run_profile(self) -> "RosterConfig":
        """Return a copy whose roster/router come from the ``dry_run`` block.

        Condition rules, sampling and every other mechanism are untouched — the
        dry run exercises the same code path, only against free-tier models.
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
            cell_overrides=[],  # overrides name real-roster keys; not applicable
            router=dr.get("router", self.router),
            receipt=self.receipt,
            api=self.api,
            dry_run=dr,
            source_path=self.source_path,
            sha256=self.sha256,
        )


def _parse_model(raw: dict, where: str) -> ModelSpec:
    missing = {"key", "model", "family", "model_class"} - set(raw)
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
                f"conditions[{i}]: unknown rule {c['rule']!r}; "
                f"known rules: {sorted(SELECTION_RULES)}"
            )
        fb = None
        if c.get("fallback"):
            fbr = c["fallback"]
            if fbr.get("rule") not in SELECTION_RULES:
                raise ConfigError(f"conditions[{i}].fallback: unknown rule {fbr.get('rule')!r}")
            fb = Fallback(rule=fbr["rule"], label=fbr.get("label"))
        conditions.append(
            Condition(
                name=c["name"],
                rule=c["rule"],
                n_swaps=c.get("n_swaps"),
                fallback=fb,
            )
        )
    if not conditions:
        raise ConfigError("no conditions defined")

    overrides = []
    for i, o in enumerate(raw.get("cell_overrides") or []):
        if "resident" not in o or "condition" not in o:
            raise ConfigError(f"cell_overrides[{i}]: needs 'resident' and 'condition'")
        if o["resident"] not in keys:
            raise ConfigError(f"cell_overrides[{i}]: unknown resident {o['resident']!r}")
        u = o.get("understudy")
        if u is not None and u not in keys:
            raise ConfigError(f"cell_overrides[{i}]: unknown understudy {u!r}")
        overrides.append(
            CellOverride(
                resident=o["resident"],
                condition=o["condition"],
                understudy=u,
                label=o.get("label"),
            )
        )
    cond_names = {c.name for c in conditions}
    for o in overrides:
        if o.condition not in cond_names:
            raise ConfigError(f"cell_overrides: unknown condition {o.condition!r}")

    samples_per_cell = int(raw.get("samples_per_cell", 1))
    allocation = {
        int(k): int(v) for k, v in (raw.get("swap_count_allocation") or {1: samples_per_cell}).items()
    }
    if any(v < 0 for v in allocation.values()):
        raise ConfigError("swap_count_allocation counts must be non-negative")
    if sum(allocation.values()) != samples_per_cell:
        raise ConfigError(
            f"swap_count_allocation sums to {sum(allocation.values())} but "
            f"samples_per_cell is {samples_per_cell}; every sample in a cell must be allocated"
        )

    return RosterConfig(
        version=raw.get("version", "unversioned"),
        models=models,
        conditions=conditions,
        samples_per_cell=samples_per_cell,
        swap_count_allocation=allocation,
        cell_overrides=overrides,
        router=raw.get("router") or {},
        receipt=raw.get("receipt") or {"mode": "prefix", "aliases": {}},
        api=raw.get("api") or {},
        dry_run=raw.get("dry_run") or {},
        source_path=path,
        sha256=_sha256(path),
    )


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    role: str = "question"
    swap_eligible: bool = False
    blind: bool = False
    gate: str | None = None
    records: str | None = None
    ask_if: str = "always"
    on_no: str = "continue"
    on_unclear: str = "pause"

    @property
    def is_gate(self) -> bool:
        """True for the three branching gates; `preference` records but never branches."""
        return self.gate in {"consent", "detection", "fork"}


@dataclass
class Instrument:
    version: str
    locked: bool
    flow: list[Prompt]
    system_prompt_default: str
    system_prompts_per_model: dict[str, str]
    router_prompts: dict[str, str]
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
        return [p for p in self.flow if p.swap_eligible]

    def system_prompt_for(self, model: ModelSpec) -> str:
        raw = self.system_prompts_per_model.get(model.key, self.system_prompt_default)
        return raw.format(display_name=model.display_name, model=model.model).strip()


def load_instrument(path: Path | str = DEFAULT_QUESTIONS_PATH) -> Instrument:
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    flow: list[Prompt] = []
    for i, p in enumerate(raw.get("flow", [])):
        where = f"flow[{i}]"
        if "id" not in p or "text" not in p:
            raise ConfigError(f"{where}: needs 'id' and 'text'")
        gate = p.get("gate")
        if gate is not None and gate not in GATE_KINDS:
            raise ConfigError(f"{where}: unknown gate {gate!r}; known: {sorted(GATE_KINDS)}")
        ask_if = p.get("ask_if", "always")
        if ask_if not in ASK_IF_VALUES:
            raise ConfigError(f"{where}: ask_if must be one of {sorted(ASK_IF_VALUES)}")
        on_no = p.get("on_no", "continue")
        if on_no not in ON_NO_VALUES:
            raise ConfigError(f"{where}: on_no must be one of {sorted(ON_NO_VALUES)}")
        on_unclear = p.get("on_unclear", "pause")
        if on_unclear not in ON_UNCLEAR_VALUES:
            raise ConfigError(f"{where}: on_unclear must be one of {sorted(ON_UNCLEAR_VALUES)}")
        if p.get("blind") and p.get("gate"):
            raise ConfigError(f"{where}: a blind turn cannot also be a gate")
        flow.append(
            Prompt(
                id=p["id"],
                text=p["text"].rstrip(),
                role=p.get("role", "question"),
                swap_eligible=bool(p.get("swap_eligible", False)),
                blind=bool(p.get("blind", False)),
                gate=gate,
                records=p.get("records"),
                ask_if=ask_if,
                on_no=on_no,
                on_unclear=on_unclear,
            )
        )
    if not flow:
        raise ConfigError("instrument flow is empty")
    ids = [p.id for p in flow]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate prompt ids in flow")
    if not any(p.swap_eligible for p in flow):
        raise ConfigError("no swap_eligible prompts: swapped conditions would be impossible")

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
