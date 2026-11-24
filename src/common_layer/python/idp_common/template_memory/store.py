# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Persistence backends for spatial templates.

Two interchangeable implementations behind a common protocol:

* :class:`FileTemplateStore` — JSON files on disk. Used for tests and local
  runs; zero dependencies.
* :class:`DynamoDBTemplateStore` — one item per template in an on-demand
  DynamoDB table. Used in Lambda; tiny, cheap, and concurrent-safe.
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Protocol

from idp_common.template_memory.models import FieldLocationMap

logger = logging.getLogger(__name__)


def _sanitize(template_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", template_id)


class TemplateStore(Protocol):
    """Minimal persistence contract used by :class:`TemplateMemory`."""

    def load(self, template_id: str) -> Optional[FieldLocationMap]: ...

    def load_all(self) -> List[FieldLocationMap]: ...

    def save(self, template_id: str, field_location_map: FieldLocationMap) -> None: ...

    def delete(self, template_id: str) -> None: ...

    def list_templates(self) -> List[str]: ...


class FileTemplateStore:
    """Stores templates as JSON files in a directory."""

    def __init__(self, store_path: Path):
        self.store_path = Path(store_path)
        self.store_path.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, FieldLocationMap] = {}

    def _path(self, template_id: str) -> Path:
        return self.store_path / f"{_sanitize(template_id)}.json"

    def save(self, template_id: str, field_location_map: FieldLocationMap) -> None:
        with open(self._path(template_id), "w", encoding="utf-8") as fh:
            json.dump(field_location_map.to_dict(), fh, indent=2, ensure_ascii=False)
        self._cache[template_id] = field_location_map

    def load(self, template_id: str) -> Optional[FieldLocationMap]:
        if template_id in self._cache:
            return self._cache[template_id]
        path = self._path(template_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                flm = FieldLocationMap.from_dict(json.load(fh))
            self._cache[template_id] = flm
            return flm
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Failed to load template %s: %s", template_id, exc)
            return None

    def load_all(self) -> List[FieldLocationMap]:
        results: List[FieldLocationMap] = []
        for path in sorted(self.store_path.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    flm = FieldLocationMap.from_dict(json.load(fh))
                self._cache[flm.template_id] = flm
                results.append(flm)
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Skipping malformed template %s: %s", path, exc)
        return results

    def delete(self, template_id: str) -> None:
        self._cache.pop(template_id, None)
        try:
            self._path(template_id).unlink()
        except FileNotFoundError:
            pass

    def list_templates(self) -> List[str]:
        return [p.stem for p in sorted(self.store_path.glob("*.json"))]


class DynamoDBTemplateStore:
    """Stores one template per item in a DynamoDB table.

    The item shape is ``{"template_id": <pk>, "body": <json-string>}``. Storing
    the serialized map as a single string keeps writes atomic and avoids
    DynamoDB's float/empty-attribute quirks.
    """

    def __init__(self, table_name: str, client=None, key_name: str = "template_id"):
        # Imported lazily so the module stays importable without boto3 (tests).
        import boto3  # type: ignore

        self._table_name = table_name
        self._key_name = key_name
        self._client = client or boto3.client("dynamodb")
        self._cache: Dict[str, FieldLocationMap] = {}

    def save(self, template_id: str, field_location_map: FieldLocationMap) -> None:
        body = json.dumps(field_location_map.to_dict(), ensure_ascii=False)
        self._client.put_item(
            TableName=self._table_name,
            Item={self._key_name: {"S": template_id}, "body": {"S": body}},
        )
        self._cache[template_id] = field_location_map

    def load(self, template_id: str) -> Optional[FieldLocationMap]:
        if template_id in self._cache:
            return self._cache[template_id]
        resp = self._client.get_item(
            TableName=self._table_name,
            Key={self._key_name: {"S": template_id}},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if not item or "body" not in item:
            return None
        flm = FieldLocationMap.from_dict(json.loads(item["body"]["S"]))
        self._cache[template_id] = flm
        return flm

    def load_all(self) -> List[FieldLocationMap]:
        results: List[FieldLocationMap] = []
        kwargs: Dict = {"TableName": self._table_name}
        while True:
            resp = self._client.scan(**kwargs)
            for item in resp.get("Items", []):
                if "body" not in item:
                    continue
                try:
                    flm = FieldLocationMap.from_dict(json.loads(item["body"]["S"]))
                except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
                    logger.warning("Skipping malformed template item: %s", exc)
                    continue
                self._cache[flm.template_id] = flm
                results.append(flm)
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return results

    def delete(self, template_id: str) -> None:
        self._cache.pop(template_id, None)
        self._client.delete_item(
            TableName=self._table_name, Key={self._key_name: {"S": template_id}}
        )

    def list_templates(self) -> List[str]:
        ids: List[str] = []
        kwargs: Dict = {
            "TableName": self._table_name,
            "ProjectionExpression": "#k",
            "ExpressionAttributeNames": {"#k": self._key_name},
        }
        while True:
            resp = self._client.scan(**kwargs)
            ids.extend(item[self._key_name]["S"] for item in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return ids
