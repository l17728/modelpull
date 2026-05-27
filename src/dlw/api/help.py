"""Help endpoint: serves MANUAL.md + curated operator docs from the
process working directory.

Adds a fixed allowlist of operator docs (runbooks, SLA) so the frontend
can render a small docs menu in the sidebar without giving arbitrary
filesystem read access. Only files in the allowlist below are served;
adding a new file requires editing this module."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/help", tags=["help"])

_MANUAL_FILENAME = "MANUAL.md"

# Curated docs surfaced to end users via the sidebar Docs menu.
# Keys are URL slugs; values are repo-relative paths. Adding entries
# requires editing this allowlist — no arbitrary filesystem traversal.
_DOC_ALLOWLIST: dict[str, dict[str, str]] = {
    "ai-troubleshooting": {
        "title_en": "AI assistant troubleshooting",
        "title_zh": "AI 助手故障排查",
        "path": "docs/operator/runbook-ai-assistant.md",
    },
    "local-auth": {
        "title_en": "Local auth runbook (password recovery, bootstrap)",
        "title_zh": "本地认证运维（密码恢复 / Bootstrap）",
        "path": "docs/operator/runbook-local-auth.md",
    },
    "sla-slo": {
        "title_en": "SLA / SLO baseline",
        "title_zh": "SLA / SLO 基线",
        "path": "docs/operator/sla-slo.md",
    },
    "qa-test-plan": {
        "title_en": "QA test plan (for testers)",
        "title_zh": "QA 测试清单（测试人员用）",
        "path": "docs/operator/qa-test-plan.md",
    },
    "v21-production-deployment": {
        "title_en": "v2.1 production deployment checklist",
        "title_zh": "v2.1 生产部署清单",
        "path": "docs/operator/v21-production-deployment.md",
    },
    "post-mortem-template": {
        "title_en": "Post-mortem template",
        "title_zh": "事故复盘模板",
        "path": "docs/operator/post-mortem-template.md",
    },
}


class ManualResponse(BaseModel):
    content: str


class DocListItem(BaseModel):
    slug: str
    title_en: str
    title_zh: str


class DocListResponse(BaseModel):
    items: list[DocListItem]


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


@router.get("/docs", response_model=DocListResponse)
async def list_docs() -> DocListResponse:
    """List the docs exposed via the sidebar Docs menu — slug + bilingual
    title. The slug is the path parameter for GET /docs/{slug}."""
    items = [DocListItem(slug=k, title_en=v["title_en"],
                          title_zh=v["title_zh"])
             for k, v in _DOC_ALLOWLIST.items()]
    return DocListResponse(items=items)


@router.get("/docs/{slug}", response_model=ManualResponse)
async def get_doc(slug: str) -> ManualResponse:
    """Return the Markdown content of one allowlisted doc. Returns 404 if
    the slug is not in the allowlist (no path traversal possible)."""
    entry = _DOC_ALLOWLIST.get(slug)
    if entry is None:
        raise HTTPException(status_code=404, detail="unknown doc slug")
    path = Path(entry["path"])
    if not path.exists():
        raise HTTPException(
            status_code=404, detail=f"doc file missing: {entry['path']}")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail=f"could not read doc: {exc}") from exc
    return ManualResponse(content=content)
