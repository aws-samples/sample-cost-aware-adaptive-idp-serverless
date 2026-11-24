# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""TemplateMemory: the main entry point for spatial field memory.

Records field locations from documents (self-learning, no predefined schema)
and uses the accumulated templates to identify document types and locate
fields — all with free, deterministic compute.

Trust (threat T2, template poisoning): templates are created only from
AI-checked documents (:meth:`TemplateMemory.learn_from_ai`) and start
``provisional``; they become ``trusted`` once enough AI-checked documents agree
on the layout and document type. The free spatial path may only refine an
existing trusted template (:meth:`TemplateMemory.refine_from_spatial`), and a
failed spot-check demotes a template back to provisional
(:meth:`TemplateMemory.demote`).
"""

import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from idp_common.template_memory.identifier import TemplateIdentifier, TemplateMatch
from idp_common.template_memory.matcher import FieldMatch, SpatialMatcher
from idp_common.template_memory.models import (
    TRUST_PROVISIONAL,
    TRUST_TRUSTED,
    FieldLocationMap,
    FieldRegion,
)
from idp_common.template_memory.store import FileTemplateStore, TemplateStore


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class TemplateMemory:
    """Record and query spatial field templates.

    Args:
        store: Persistence backend. Defaults to a :class:`FileTemplateStore`
            under ``~/.template_memory/templates`` when none is supplied.
        spatial_tolerance: Position tolerance for merging/matching (5% default).
        similarity_threshold: Minimum score to match an existing template.
        spatial_weight / name_weight: Field-matching score blend.
        decay_factor: Weight decay applied to existing observations on merge.
    """

    def __init__(
        self,
        store: Optional[TemplateStore] = None,
        spatial_tolerance: float = 0.05,
        similarity_threshold: float = 0.7,
        spatial_weight: float = 0.4,
        name_weight: float = 0.6,
        decay_factor: float = 0.95,
    ):
        if store is None:
            store = FileTemplateStore(Path.home() / ".template_memory" / "templates")
        self.store = store
        self.identifier = TemplateIdentifier(similarity_threshold)
        self.matcher = SpatialMatcher(spatial_tolerance, spatial_weight, name_weight)
        self.spatial_tolerance = spatial_tolerance
        self.decay_factor = decay_factor

    def record(
        self,
        document: Any,
        template_id: Optional[str] = None,
        doc_type: Optional[str] = None,
    ) -> str:
        """Learn field locations from a document; returns the template_id used.

        When ``template_id`` is omitted, the document is matched against stored
        templates (or a new signature is minted if nothing matches). When
        ``doc_type`` is given (the LLM/BDA tier's classification), it is stamped
        on the template so the free spatial path can report it for repeats.
        """
        observed = self._observe(document)
        if not observed.fields:
            raise ValueError("No key-value fields found in document")

        if template_id is None:
            match = self.identifier.identify(document, self.store.load_all())
            template_id = (
                match.template_id
                if match is not None
                else self.identifier.compute_signature(document)
            )

        observed.template_id = template_id
        observed.doc_type = doc_type
        existing = self.store.load(template_id)
        if existing is None:
            self.store.save(template_id, observed)
        else:
            existing.merge(observed, self.spatial_tolerance, self.decay_factor)
            # Only overwrite doc_type when a fresh classification is supplied,
            # so a later free spatial re-observation doesn't clear it.
            if doc_type:
                existing.doc_type = doc_type
            self.store.save(template_id, existing)
        return template_id

    def locate(
        self, document: Any, field_name: str, page: Optional[int] = None
    ) -> List[FieldMatch]:
        """Locate a field using the best-matching stored template."""
        match = self.identifier.identify(document, self.store.load_all())
        if match is None:
            return []
        flm = self.store.load(match.template_id)
        if flm is None:
            return []
        return self.matcher.find_field(field_name, document, flm, page)

    def identify_template(
        self, document: Any, trusted_only: bool = False
    ) -> Optional[TemplateMatch]:
        """Best-matching stored template; ``trusted_only`` ignores provisional ones."""
        templates = self.store.load_all()
        if trusted_only:
            templates = [t for t in templates if t.trust_state == TRUST_TRUSTED]
        return self.identifier.identify(document, templates)

    def learn_from_ai(
        self,
        document: Any,
        doc_type: Optional[str] = None,
        confirmations_required: int = 2,
        verified: bool = True,
    ) -> Tuple[str, str]:
        """Learn a layout from an AI-checked document; returns ``(template_id, trust_state)``.

        ``verified`` says whether the AI output was verified (every kept value
        grounded in the OCR text, or given a BDA bounding box). Only verified
        documents count toward trust:

        * No matching template: a new provisional template with one
          confirmation (trusted straight away only if ``confirmations_required``
          is 1 or less).
        * Match with the same ``doc_type`` (``None`` equals ``None``): merge the
          regions and add a confirmation; trusted once confirmations reach
          ``confirmations_required``.
        * Match with a different ``doc_type``: merge, take the new AI type, and
          reset to provisional with one confirmation.

        An unverified document never adds a confirmation, never changes a
        template's ``doc_type`` and never touches a trusted template. It may
        create a provisional template with zero confirmations, or merge its
        regions into a provisional template of the same ``doc_type``, so a
        layout still starts to be learned while it waits for verified documents.
        """
        observed = self._observe(document)
        if not observed.fields:
            raise ValueError("No key-value fields found in document")
        required = max(1, int(confirmations_required))
        match = self.identifier.identify(document, self.store.load_all())
        existing = self.store.load(match.template_id) if match is not None else None
        if existing is None:
            template_id = (
                match.template_id
                if match is not None
                else self.identifier.compute_signature(document)
            )
            observed.template_id = template_id
            observed.doc_type = doc_type
            if verified:
                observed.confirmations = 1
                observed.trust_state = (
                    TRUST_TRUSTED if required <= 1 else TRUST_PROVISIONAL
                )
            else:
                observed.confirmations = 0
                observed.trust_state = TRUST_PROVISIONAL
            self.store.save(template_id, observed)
            return template_id, observed.trust_state

        template_id = existing.template_id
        observed.template_id = template_id
        if not verified:
            if existing.trust_state != TRUST_TRUSTED and existing.doc_type == doc_type:
                existing.merge(observed, self.spatial_tolerance, self.decay_factor)
                self.store.save(template_id, existing)
            return template_id, existing.trust_state
        existing.merge(observed, self.spatial_tolerance, self.decay_factor)
        if existing.doc_type == doc_type:
            existing.confirmations += 1
            if existing.confirmations >= required:
                existing.trust_state = TRUST_TRUSTED
        else:
            existing.doc_type = doc_type
            existing.trust_state = TRUST_PROVISIONAL
            existing.confirmations = 1
        self.store.save(template_id, existing)
        return template_id, existing.trust_state

    def refine_from_spatial(self, document: Any, template_id: str) -> bool:
        """Refine an existing *trusted* template's regions from a free-path document.

        Never creates a template and never changes its ``doc_type``, trust
        state or confirmations. Returns True when the template was refined.
        """
        existing = self.store.load(template_id)
        if existing is None or existing.trust_state != TRUST_TRUSTED:
            return False
        observed = self._observe(document)
        if not observed.fields:
            return False
        observed.template_id = template_id
        existing.merge(observed, self.spatial_tolerance, self.decay_factor)
        self.store.save(template_id, existing)
        return True

    def demote(self, template_id: str) -> bool:
        """Send a template back to provisional with zero confirmations."""
        existing = self.store.load(template_id)
        if existing is None:
            return False
        existing.trust_state = TRUST_PROVISIONAL
        existing.confirmations = 0
        self.store.save(template_id, existing)
        return True

    def get_template(self, template_id: str) -> Optional[FieldLocationMap]:
        return self.store.load(template_id)

    def list_templates(self) -> List[str]:
        return self.store.list_templates()

    def delete_template(self, template_id: str) -> None:
        self.store.delete(template_id)

    # ------------------------------------------------------------------
    def _observe(self, document: Any) -> FieldLocationMap:
        """Build a single-sample FieldLocationMap from a document's fields."""
        flm = FieldLocationMap(
            template_id="placeholder",
            page_count=len(document.pages),
            created_at=_utc_now(),
        )
        for page in document.pages:
            for kv in page.key_values:
                key_text = " ".join(w.text for w in kv.key).strip()
                if not key_text:
                    continue
                flm.add_field(
                    key_text,
                    FieldRegion(
                        field_name=key_text,
                        page=kv.page,
                        bbox={
                            "x": kv.bbox.x,
                            "y": kv.bbox.y,
                            "width": kv.bbox.width,
                            "height": kv.bbox.height,
                        },
                        confidence=getattr(kv, "confidence", 1.0),
                    ),
                )
        return flm
