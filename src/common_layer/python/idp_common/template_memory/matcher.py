# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Spatial + name matching of candidate fields against expected regions.

Combines spatial proximity (IoU blended with center distance) and name
similarity (normalized Levenshtein) into a single ranked score.
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class FieldMatch:
    """A scored candidate for a requested field."""

    key_value: Any
    spatial_score: float
    name_score: float
    combined_score: float
    within_expected_region: bool


def _levenshtein(s: str, t: str) -> int:
    if len(s) < len(t):
        s, t = t, s
    if not t:
        return len(s)
    previous = list(range(len(t) + 1))
    for i, cs in enumerate(s):
        current = [i + 1]
        for j, ct in enumerate(t):
            current.append(
                min(
                    previous[j + 1] + 1,
                    current[j] + 1,
                    previous[j] + (0 if cs == ct else 1),
                )
            )
        previous = current
    return previous[-1]


def name_similarity(a: str, b: str) -> float:
    """1.0 = identical, 0.0 = completely different (length-normalized)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


class SpatialMatcher:
    """Scores candidate key/values against stored field regions."""

    def __init__(
        self,
        spatial_tolerance: float = 0.05,
        spatial_weight: float = 0.4,
        name_weight: float = 0.6,
    ):
        self.spatial_tolerance = spatial_tolerance
        self.spatial_weight = spatial_weight
        self.name_weight = name_weight

    @staticmethod
    def expand_region(bbox: Dict[str, float], tolerance: float) -> Dict[str, float]:
        if tolerance < 0:
            raise ValueError("tolerance must be non-negative")
        if tolerance == 0:
            return dict(bbox)
        x = max(0.0, bbox["x"] - tolerance)
        y = max(0.0, bbox["y"] - tolerance)
        return {
            "x": x,
            "y": y,
            "width": min(1.0 - x, bbox["width"] + 2 * tolerance),
            "height": min(1.0 - y, bbox["height"] + 2 * tolerance),
        }

    @staticmethod
    def compute_spatial_score(
        candidate: Dict[str, float], expected: Dict[str, float]
    ) -> float:
        """IoU (weighted 0.7) blended with center-distance score (0.3)."""
        c_area = candidate["width"] * candidate["height"]
        e_area = expected["width"] * expected["height"]
        if c_area <= 0 or e_area <= 0:
            return 0.0
        ix1 = max(candidate["x"], expected["x"])
        iy1 = max(candidate["y"], expected["y"])
        ix2 = min(candidate["x"] + candidate["width"], expected["x"] + expected["width"])
        iy2 = min(candidate["y"] + candidate["height"], expected["y"] + expected["height"])
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        union = c_area + e_area - inter
        iou = inter / union if union > 0 else 0.0

        c_cx = candidate["x"] + candidate["width"] / 2.0
        c_cy = candidate["y"] + candidate["height"] / 2.0
        e_cx = expected["x"] + expected["width"] / 2.0
        e_cy = expected["y"] + expected["height"] / 2.0
        distance = math.hypot(c_cx - e_cx, c_cy - e_cy)
        distance_score = max(0.0, 1.0 - distance / math.sqrt(2))
        return 0.7 * iou + 0.3 * distance_score if iou > 0 else distance_score

    def find_field(
        self,
        field_name: str,
        document: Any,
        field_location_map: Any,
        page: Optional[int] = None,
    ) -> List[FieldMatch]:
        expected = field_location_map.get_field_region(field_name, page)
        pages = [page] if page is not None else range(1, len(document.pages) + 1)
        candidates = [
            kv for p in pages for kv in document.pages[p - 1].key_values
        ]
        return self.rank_candidates(candidates, field_name, expected)

    def rank_candidates(
        self, candidates: List[Any], field_name: str, expected_region: Optional[Any]
    ) -> List[FieldMatch]:
        matches: List[FieldMatch] = []
        for candidate in candidates:
            key_text = " ".join(w.text for w in candidate.key).strip()
            n_score = name_similarity(field_name.lower(), key_text.lower())
            if expected_region is not None:
                c_bbox = {
                    "x": candidate.bbox.x,
                    "y": candidate.bbox.y,
                    "width": candidate.bbox.width,
                    "height": candidate.bbox.height,
                }
                s_score = self.compute_spatial_score(c_bbox, expected_region.bbox)
                expanded = self.expand_region(expected_region.bbox, self.spatial_tolerance)
                cx = candidate.bbox.x + candidate.bbox.width / 2.0
                cy = candidate.bbox.y + candidate.bbox.height / 2.0
                within = (
                    expanded["x"] <= cx <= expanded["x"] + expanded["width"]
                    and expanded["y"] <= cy <= expanded["y"] + expanded["height"]
                )
            else:
                s_score = 0.0
                within = False
            matches.append(
                FieldMatch(
                    key_value=candidate,
                    spatial_score=s_score,
                    name_score=n_score,
                    combined_score=self.spatial_weight * s_score
                    + self.name_weight * n_score,
                    within_expected_region=within,
                )
            )
        matches.sort(key=lambda m: m.combined_score, reverse=True)
        return matches
