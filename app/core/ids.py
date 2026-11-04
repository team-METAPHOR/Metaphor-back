from uuid import uuid4


def new_id() -> str:
    """32-char hex id: valid for both engine QARequest ids and RunPolicy program ids."""
    return uuid4().hex
