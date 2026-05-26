"""Generate a skills-style MANIFEST.md that bridges modelpull's in-process
tool registry to the opencode CLI subprocess.

The OpenCodeRunner prepends this manifest to the user's message so the
underlying LLM (Claude via opencode) sees a catalog of available actions
plus the exact shell command to invoke each one. This avoids implementing
a full MCP server while still giving real backends access to the tools.

Single source of truth: READONLY_TOOLS + WRITE_TOOLS registries. If you
add a tool there, it shows up in the manifest automatically — no parallel
list to keep in sync.

Auth: the CLI commands assume `DLW_BEARER_TOKEN` is set in the opencode
subprocess env (or that a `dlw context` is already configured with a valid
token on the caller's machine). OpenCodeRunner forwards the controller's
admin token when running server-side.

Token cost: ~1500-2000 tokens per turn. Acceptable for SP4f; a smarter
implementation would push this through opencode's persistent-context
mechanism if/when one is wired up."""
from __future__ import annotations

from dlw.ai.tools import READONLY_TOOLS
from dlw.ai.write_tools import WRITE_TOOLS

# Tool name → CLI / curl invocation. Each entry is a short shell template
# (placeholders use {arg_name}). Empty string = no CLI wrapper exists yet;
# the manifest then instructs the model to call the REST endpoint directly
# via curl. Both modes assume DLW_BEARER_TOKEN is set in env.
_CLI_RECIPE: dict[str, str] = {
    # read tools
    "dlw_list_tasks":
        "dlw list --status {status?}",
    "dlw_get_task":
        "dlw show {task_id}",
    "dlw_get_task_events":
        "curl -s -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "\"$DLW_API_BASE/api/v1/tasks/{task_id}/events?limit={limit?20}\"",
    "dlw_quota_current":
        "curl -s -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "\"$DLW_API_BASE/api/v1/quota/current\"",
    "dlw_list_storages":
        "curl -s -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "\"$DLW_API_BASE/api/v1/storages\"",
    "hf_api_metadata":
        "curl -s \"https://huggingface.co/api/models/{repo_id}\"",
    "hf_model_card":
        "curl -s \"https://huggingface.co/{repo_id}/raw/{revision?main}/README.md\"",
    "search_huggingface_models":
        "curl -s \"https://huggingface.co/api/models?search={query}&sort={sort?lastModified}&direction=-1&limit={limit?10}\"",
    "search_modelscope_models":
        "curl -s \"https://modelscope.cn/api/v1/models?Name={query}&PageSize={limit?10}&SortBy=Downloads\"",
    "web_search": "",   # operator-gated; AI should mention it's disabled if needed
    "fetch_user_content": "",   # operator-gated
    # write tools — all go through `dlw` CLI (with built-in confirmation UX)
    "dlw_create_task":
        "dlw submit {repo_id} --revision {revision?main} --storage-id {storage_id?}",
    "dlw_cancel_task":
        "dlw cancel {task_id}",
    "dlw_delete_task":
        "dlw delete {task_id}",
    "dlw_retry_task":
        "# Fetch the original task params then submit a new task with them:\n"
        "  dlw show {task_id} --json | jq -r '.repo_id, .revision, .storage_id' "
        "| xargs -n 3 dlw submit",
    "dlw_upgrade_task":
        "# Same repo / storage as original, new revision:\n"
        "  ORIG=$(dlw show {task_id} --json); "
        "dlw submit $(jq -r .repo_id <<<\"$ORIG\") "
        "--revision {new_revision} "
        "--storage-id $(jq -r .storage_id <<<\"$ORIG\")",
    "dlw_patch_task":
        "curl -s -X PATCH -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "-H 'Content-Type: application/json' "
        "-d '{\"priority\": {priority?}, \"source_strategy\": \"{source_strategy?}\"}' "
        "\"$DLW_API_BASE/api/v1/tasks/{task_id}\"",
    "dlw_create_local_user":
        "curl -s -X POST -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "-H 'Content-Type: application/json' "
        "-d '{\"username\":\"{username}\",\"password\":\"{password}\",\"tenant_id\":{tenant_id},\"role\":\"{role}\"}' "
        "\"$DLW_API_BASE/api/v1/auth/local/users\"",
    "dlw_reset_local_password":
        "curl -s -X POST -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "-H 'Content-Type: application/json' "
        "-d '{\"new_password\":\"{new_password}\"}' "
        "\"$DLW_API_BASE/api/v1/auth/local/users/{user_id}/reset\"",
    "dlw_set_tenant_quota":
        "curl -s -X PUT -H \"Authorization: Bearer $DLW_BEARER_TOKEN\" "
        "-H 'Content-Type: application/json' "
        "-d '{\"quota_concurrent\": {quota_concurrent?}, \"quota_storage_gb\": {quota_storage_gb?}}' "
        "\"$DLW_API_BASE/api/v1/tenants/{tenant_id}/quota\"",
}


def _format_required(schema: dict) -> str:
    req = schema.get("required") or []
    if not req:
        return "(none)"
    return ", ".join(req)


def _format_props(schema: dict) -> str:
    props = schema.get("properties") or {}
    if not props:
        return "  (no parameters)"
    lines = []
    for name, spec in props.items():
        t = spec.get("type", "string") if isinstance(spec, dict) else "string"
        desc = (spec.get("description") if isinstance(spec, dict) else None) or ""
        lines.append(f"  - `{name}` ({t}){' — ' + desc if desc else ''}")
    return "\n".join(lines)


def build_skills_manifest(*, include_write: bool = True) -> str:
    """Render the full MANIFEST.md content as a string.

    `include_write=False` produces a read-only manifest (useful when the
    bearer token in env has only viewer permission)."""
    out: list[str] = []
    out.append("# modelpull AI assistant — available tools")
    out.append("")
    out.append("You are wired into a tool catalog. **When the user's question")
    out.append("matches one of the tools below, invoke that tool** instead of")
    out.append("answering from memory. Pick the most specific tool first — ")
    out.append("Hugging Face / ModelScope domain tools beat `web_search`,")
    out.append("which beats model knowledge.")
    out.append("")
    out.append("**How to invoke**: each tool lists a shell command. Execute")
    out.append("the command with the bash tool, then summarize the result for")
    out.append("the user. Required arg placeholders are `{name}`; optional")
    out.append("ones are `{name?default}`. Env vars `DLW_BEARER_TOKEN` and")
    out.append("`DLW_API_BASE` are pre-set; do not hard-code other secrets.")
    out.append("")
    out.append("**Write tools require explicit user confirmation in the chat**")
    out.append("before you run them — show the parameters and ask 'OK to")
    out.append("proceed?' first.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Read tools (safe, no confirmation needed)")
    out.append("")
    for name, tool in sorted(READONLY_TOOLS.items()):
        recipe = _CLI_RECIPE.get(name, "")
        out.append(f"### `{name}`")
        out.append("")
        out.append(tool.description)
        out.append("")
        out.append(f"**Required args**: {_format_required(tool.input_schema)}")
        out.append("**Parameters**:")
        out.append(_format_props(tool.input_schema))
        out.append("")
        if recipe:
            out.append("**Invoke**:")
            out.append("```bash")
            out.append(recipe)
            out.append("```")
        else:
            out.append("_(no CLI wrapper — operator-gated tool; mention to user if asked)_")
        out.append("")
    if include_write:
        out.append("---")
        out.append("")
        out.append("## Write tools (DESTRUCTIVE — ask user to confirm first)")
        out.append("")
        for name, tool in sorted(WRITE_TOOLS.items()):
            recipe = _CLI_RECIPE.get(name, "")
            out.append(f"### `{name}`")
            out.append("")
            out.append(tool.description)
            out.append("")
            out.append(f"**Required args**: {_format_required(tool.input_schema)}")
            out.append("**Parameters**:")
            out.append(_format_props(tool.input_schema))
            out.append("")
            if recipe:
                out.append("**Invoke (only after user confirms)**:")
                out.append("```bash")
                out.append(recipe)
                out.append("```")
            else:
                out.append("_(no shell template registered yet)_")
            out.append("")
    out.append("---")
    out.append("")
    out.append("End of tool catalog. Pick a tool and invoke it, or ask the")
    out.append("user a clarifying question if intent is unclear.")
    return "\n".join(out)
