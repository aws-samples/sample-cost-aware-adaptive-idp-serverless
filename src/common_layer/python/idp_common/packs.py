# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Industry pack loader — the only thing you "define".

A pack is a declarative YAML file describing, per industry: document types and
their region kinds, the keys used to link records across a project/asset/entity
lifecycle, compliance limits, and per-field value-at-risk weights. The engine
is industry-agnostic; packs give the spatially-learned fields *meaning*.

Packs are bundled alongside this module (``idp_common/packs/*.yaml``) so they
ship with the Lambda layer. ``PACK_DIR`` can override the location.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_BUNDLED_DIR = Path(__file__).resolve().parent / "pack_defs"

# Identifiers in a compliance expr that are not pure numbers are field names.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class ComplianceRule:
    """A single declarative compliance check."""

    id: str
    description: str
    expr: str

    def field_names(self) -> List[str]:
        return _IDENT_RE.findall(self.expr)


@dataclass
class DocumentType:
    """One document type within an industry, with its own fields and rules.

    ``fields`` are the field names to extract for this type (so a pile test
    report and a delivery note ask for different things). ``field_descriptions``
    are optional pack-author hints (a description or synonyms) for individual
    fields, keyed by field name — used to sharpen both the LLM prompt and the
    BDA blueprint's per-field instruction. ``compliance_rules`` are the checks
    that apply only to this type.
    """

    name: str
    region_kinds: List[str] = field(default_factory=list)
    fields: List[str] = field(default_factory=list)
    field_descriptions: Dict[str, str] = field(default_factory=dict)
    compliance_rules: List[ComplianceRule] = field(default_factory=list)


def _dedup(names) -> List[str]:
    """Ordered, case-insensitive de-duplication of field names."""
    ordered: List[str] = []
    seen = set()
    for name in names:
        key = (name or "").strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            ordered.append(key)
    return ordered


@dataclass
class IndustryPack:
    """A loaded industry pack.

    Fields and compliance rules are declared **per document type**. Linking keys
    (which reconcile records across a project/asset/entity lifecycle) and
    value-at-risk weights stay at the pack level, plus optional pack-wide
    ``global_compliance_rules`` that apply to every type.
    """

    industry: str
    document_types: List[DocumentType] = field(default_factory=list)
    linking_keys: List[str] = field(default_factory=list)
    global_compliance_rules: List[ComplianceRule] = field(default_factory=list)
    value_at_risk_weights: Dict[str, float] = field(default_factory=dict)
    # Fingerprint of the definition this pack was built from (see
    # :func:`pack_fingerprint`); stamped on every compliance result.
    content_hash: Optional[str] = None

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def document_type(self, name: Optional[str]) -> Optional[DocumentType]:
        if not name:
            return None
        for dt in self.document_types:
            if dt.name == name:
                return dt
        return None

    @property
    def compliance_rules(self) -> List[ComplianceRule]:
        """Flattened view: pack-wide rules plus every type's rules.

        Kept for callers/analytics that want all rules; per-document evaluation
        should use :meth:`compliance_rules_for`.
        """
        rules = list(self.global_compliance_rules)
        for dt in self.document_types:
            rules.extend(dt.compliance_rules)
        return rules

    def compliance_rules_for(self, doc_type: Optional[str] = None) -> List[ComplianceRule]:
        """Rules that apply to a document of ``doc_type``.

        Pack-wide rules always apply; the matched type's rules are added. An
        unknown/absent type gets only the pack-wide rules.
        """
        rules = list(self.global_compliance_rules)
        dt = self.document_type(doc_type)
        if dt is not None:
            rules.extend(dt.compliance_rules)
        return rules

    # ------------------------------------------------------------------
    # Field derivation (drives the LLM prompt + BDA blueprint schema)
    # ------------------------------------------------------------------
    def fields_for(self, doc_type: Optional[str] = None) -> List[str]:
        """Ordered, de-duplicated field names to extract.

        For a known ``doc_type``: that type's fields + its rule fields + the
        pack-wide rule fields + linking keys. Otherwise: the union across all
        types + pack-wide rule fields + value-at-risk fields + linking keys
        (used for the pack-wide fallback blueprint and back-compat).
        """
        names: List[str] = list(self.linking_keys)
        for rule in self.global_compliance_rules:
            names.extend(rule.field_names())
        dt = self.document_type(doc_type)
        if dt is not None:
            names.extend(dt.fields)
            for rule in dt.compliance_rules:
                names.extend(rule.field_names())
        else:
            for t in self.document_types:
                names.extend(t.fields)
                for rule in t.compliance_rules:
                    names.extend(rule.field_names())
            names.extend(self.value_at_risk_weights.keys())
        return _dedup(names)

    def extraction_fields(self, doc_type: Optional[str] = None) -> List[str]:
        """Back-compatible alias for :meth:`fields_for`."""
        return self.fields_for(doc_type)

    def type_field_map(self) -> Dict[str, List[str]]:
        """Map of document-type name -> its extraction fields (+ its rule
        fields), for building a single classify-and-extract LLM prompt."""
        out: Dict[str, List[str]] = {}
        for dt in self.document_types:
            names = list(dt.fields)
            for rule in dt.compliance_rules:
                names.extend(rule.field_names())
            out[dt.name] = _dedup(names)
        return out

    def descriptions_for(self, doc_type: Optional[str] = None) -> Dict[str, str]:
        """Field-name -> author-supplied description/synonym hints.

        For a known ``doc_type``, that type's own hints. Without one, the union
        across every type (used for the pack-wide fallback blueprint).
        """
        dt = self.document_type(doc_type)
        if dt is not None:
            return dict(dt.field_descriptions)
        merged: Dict[str, str] = {}
        for t in self.document_types:
            merged.update(t.field_descriptions)
        return merged

    @classmethod
    def from_dict(cls, data: Dict) -> "IndustryPack":
        def _rules(items) -> List[ComplianceRule]:
            return [
                ComplianceRule(
                    id=r["id"], description=r.get("description", ""), expr=r["expr"]
                )
                for r in (items or [])
            ]

        def _fields(items) -> "tuple[List[str], Dict[str, str]]":
            """Parse a ``fields:`` list of bare names and/or ``{name,
            description}`` objects. Backward-compatible: a plain list of
            strings works exactly as before, with no descriptions."""
            names: List[str] = []
            descriptions: Dict[str, str] = {}
            for item in items or []:
                if isinstance(item, dict):
                    fname = str(item.get("name", "")).strip()
                    if not fname:
                        continue
                    names.append(fname)
                    desc = item.get("description")
                    if desc:
                        descriptions[fname] = str(desc)
                else:
                    fname = str(item).strip()
                    if fname:
                        names.append(fname)
            return names, descriptions

        document_types = []
        for dt in data.get("document_types", []):
            names, descriptions = _fields(dt.get("fields"))
            document_types.append(
                DocumentType(
                    name=dt["name"],
                    region_kinds=list(dt.get("region_kinds", [])),
                    fields=names,
                    field_descriptions=descriptions,
                    compliance_rules=_rules(dt.get("compliance_rules")),
                )
            )

        return cls(
            industry=data["industry"],
            document_types=document_types,
            linking_keys=list(data.get("linking_keys", [])),
            # Top-level compliance_rules are treated as pack-wide (back-compat
            # with the earlier single-list shape).
            global_compliance_rules=_rules(data.get("compliance_rules")),
            value_at_risk_weights={
                str(k): float(v)
                for k, v in (data.get("value_at_risk_weights", {}) or {}).items()
            },
            content_hash=pack_fingerprint(data),
        )


def pack_fingerprint(data: Dict) -> str:
    """Short, stable content hash of a raw pack definition.

    Canonical JSON (sorted keys, compact separators) means the bundled YAML and
    the same content stored as JSON in DynamoDB hash identically. Recorded with
    each compliance result so a record can be traced to the exact rule set that
    judged it, and compared in pack-change alerts.
    """
    canonical = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _pack_dir(pack_dir: Optional[str]) -> Path:
    return Path(pack_dir) if pack_dir else Path(os.environ.get("PACK_DIR") or _BUNDLED_DIR)


def load_pack_data(name: str, pack_dir: Optional[str] = None) -> Dict:
    """Load the raw pack dict from a bundled YAML file.

    This is the single source used both to build an :class:`IndustryPack` and to
    seed the packs table (the same YAML shape is stored as JSON in DynamoDB).
    """
    import yaml  # type: ignore  # lazy import; provided by the deps layer

    path = _pack_dir(pack_dir) / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Industry pack not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_pack(name: str, pack_dir: Optional[str] = None) -> IndustryPack:
    """Load and parse a bundled industry pack by name (without extension)."""
    return IndustryPack.from_dict(load_pack_data(name, pack_dir))


def pack_from_item(item: Dict) -> IndustryPack:
    """Build an :class:`IndustryPack` from a packs-table item.

    Item shape: ``{"pack_name": <str>, "definition": <dict|json-string>}`` where
    ``definition`` is the same structure as a pack YAML file.
    """
    definition = item.get("definition")
    if isinstance(definition, str):
        definition = json.loads(definition)
    if not isinstance(definition, dict):
        raise ValueError("packs item is missing a valid 'definition'")
    return IndustryPack.from_dict(definition)


def load_pack_dynamodb(name: str, table_name: str, client=None) -> IndustryPack:
    """Load a pack from the DynamoDB packs table (runtime config source)."""
    import boto3  # lazy import; provided by the Lambda runtime/deps layer

    ddb = client or boto3.client("dynamodb")
    resp = ddb.get_item(TableName=table_name, Key={"pack_name": {"S": name}})
    item = resp.get("Item")
    if not item or "definition" not in item:
        raise KeyError(f"pack '{name}' not found in table {table_name}")
    return pack_from_item({"pack_name": name, "definition": item["definition"]["S"]})


def resolve_pack(
    name: str,
    table_name: Optional[str] = None,
    client=None,
    pack_dir: Optional[str] = None,
) -> IndustryPack:
    """Resolve a pack from DynamoDB when configured, else the bundled YAML.

    DynamoDB is the source of truth at runtime (so field/doc-type edits need no
    deploy). If the table is not configured, or the item is missing/unavailable,
    fall back to the bundled seed so the pipeline never breaks on config lookup.
    """
    if table_name:
        try:
            return load_pack_dynamodb(name, table_name, client)
        except Exception as exc:  # noqa: BLE001 - degrade to the bundled seed
            logger.warning(
                "Pack %r lookup from DynamoDB table %r failed (%s); "
                "falling back to the bundled seed.",
                name,
                table_name,
                exc,
            )
    return load_pack(name, pack_dir)


def list_packs(pack_dir: Optional[str] = None) -> List[str]:
    """List available pack names (without extension)."""
    directory = _pack_dir(pack_dir)
    return sorted(p.stem for p in directory.glob("*.yaml"))
