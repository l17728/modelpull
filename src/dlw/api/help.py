"""Help endpoint: serves MANUAL.md from the process working directory."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/help", tags=["help"])

_MANUAL_FILENAME = "MANUAL.md"


class ManualResponse(BaseModel):
    content: str


@router.get("/manual", response_model=ManualResponse)
async def get_manual() -> ManualResponse:
    """Return the contents of MANUAL.md from the process working directory.

    Reads at request time so the file can be updated without restarting the
    server. The AI assistant can also fetch this endpoint to answer help
    questions in context.
    """
    path = Path(_MANUAL_FILENAME)
    if not path.exists():
        raise HTTPException(status_code=404, detail="MANUAL.md not found")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"could not read manual: {exc}") from exc
    return ManualResponse(content=content)
