from copy import deepcopy
from typing import Any


def migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    """Migrate a document to the current schema version."""
    result = deepcopy(document)
    result["version"] = 3
    return result
