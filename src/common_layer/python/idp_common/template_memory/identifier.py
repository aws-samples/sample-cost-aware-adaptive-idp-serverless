# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Template identification: which stored template best matches a document.

Combined score = 0.6 * structural (Jaccard of field names)
               + 0.4 * spatial (mean spatial score over shared fields).
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from idp_common.template_memory.matcher import SpatialMatcher
from idp_common.template_memory.models import FieldLocationMap

logger = logging.getLogger(__name__)


@dataclass
class TemplateMatch:
    """Result of identifying a document against stored templates."""

    template_id: str
    similarity_score: float
    field_overlap_ratio: float
    spatial_similarity: float


class TemplateIdentifier:
    """Identifies the best-matching stored template for a document."""

    def __init__(self, similarity_threshold: float = 0.7):
        self.similarity_threshold = similarity_threshold
        self._matcher = SpatialMatcher()

    def identify(
        self, document: Any, stored_templates: List[FieldLocationMap]
    ) -> Optional[TemplateMatch]:
        if not stored_templates:
            return None
        doc_field_names = self._field_names(document)
        best: Optional[TemplateMatch] = None
        best_score = -1.0
        best_structural = -1.0
        for template in stored_templates:
            try:
                structural = self.structural_similarity(doc_field_names, template)
                spatial = self.spatial_similarity(document, template)
                combined = 0.6 * structural + 0.4 * spatial
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Skipping template %s: %s", template.template_id, exc)
                continue
            if combined > best_score or (
                combined == best_score and structural > best_structural
            ):
                best_score = combined
                best_structural = structural
                best = TemplateMatch(
                    template_id=template.template_id,
                    similarity_score=combined,
                    field_overlap_ratio=structural,
                    spatial_similarity=spatial,
                )
        if best and best.similarity_score >= self.similarity_threshold:
            return best
        return None

    @staticmethod
    def structural_similarity(
        doc_field_names: Set[str], template: FieldLocationMap
    ) -> float:
        doc_set = {n.lower().strip() for n in doc_field_names}
        tmpl_set = {n.lower().strip() for n in template.fields}
        if not doc_set and not tmpl_set:
            return 0.0
        return len(doc_set & tmpl_set) / len(doc_set | tmpl_set)

    def spatial_similarity(self, document: Any, template: FieldLocationMap) -> float:
        doc_fields: Dict[str, Dict[str, float]] = {}
        for page in document.pages:
            for kv in page.key_values:
                name = " ".join(w.text for w in kv.key).strip().lower()
                if name and name not in doc_fields:
                    doc_fields[name] = {
                        "x": kv.bbox.x,
                        "y": kv.bbox.y,
                        "width": kv.bbox.width,
                        "height": kv.bbox.height,
                    }
        tmpl_fields: Dict[str, Dict[str, float]] = {}
        for name, regions in template.fields.items():
            lname = name.lower().strip()
            if regions and lname not in tmpl_fields:
                tmpl_fields[lname] = max(
                    regions, key=lambda r: r.occurrence_count
                ).bbox
        shared = set(doc_fields) & set(tmpl_fields)
        if not shared:
            return 0.0
        scores = [
            self._matcher.compute_spatial_score(doc_fields[n], tmpl_fields[n])
            for n in shared
        ]
        return max(0.0, min(1.0, sum(scores) / len(scores)))

    def compute_signature(self, document: Any) -> str:
        """Stable, human-readable template_id derived from field names."""
        names = sorted(self._field_names(document))
        digest = hashlib.md5(
            "|".join(names).encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:8]
        slug_parts = []
        for name in names[:3]:
            words = name.split()
            if words:
                slug = "".join(c for c in words[0] if c.isalnum()).lower()
                if slug:
                    slug_parts.append(slug)
        return f"{'-'.join(slug_parts)}-{digest}" if slug_parts else f"template-{digest}"

    @staticmethod
    def _field_names(document: Any) -> Set[str]:
        names: Set[str] = set()
        for page in document.pages:
            for kv in page.key_values:
                name = " ".join(w.text for w in kv.key).strip().lower()
                if name:
                    names.add(name)
        return names
