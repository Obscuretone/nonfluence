"""MCP tools on the official SDK's local stdio transport."""

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .references import classify, resolve
from .store import Invalid, page_id


def make_server(store, nira, *, nira_hq=None):
    if store.classification != "private":
        raise PermissionError("The MCP server only opens private workspaces.")
    mcp = FastMCP(
        "Nonfluence",
        instructions=f"This connection is scoped to the '{store.space}' documentation space.",
    )
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    @mcp.tool(annotations=read_only, structured_output=True)
    def search_pages(query: str, limit: int = 20) -> dict[str, Any]:
        """Find pages in this space by words in their titles and Markdown."""
        return {"pages": store.search(query, limit)}

    @mcp.tool(annotations=read_only, structured_output=True)
    def list_pages() -> dict[str, Any]:
        """List titles, stable page IDs and parent IDs in this space."""
        return {"pages": store.list_pages()}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False), structured_output=True)
    def read_page(id: str) -> dict[str, Any]:
        """Read a UUID-addressed page, including Markdown, typed references, and backlinks."""
        page, refs = store.read(id)
        return {**page, "references": resolve(refs, nira, nira_hq)}

    @mcp.tool(annotations=read_only, structured_output=True)
    def find_backlinks(reference: str, limit: int = 20) -> dict[str, Any]:
        """List pages linking to a Nonfluence page, standalone Nira ticket, Nira HQ permalink, or GitHub URL."""
        ref = classify(reference)
        if ref is None:
            raise Invalid("Use page:<UUID>, nira:<ticket ID or key>, a Nira HQ permalink, or a GitHub URL.")
        if not 1 <= limit <= 50:
            raise Invalid("Use a limit between 1 and 50.")
        return {"pages": store.sources(ref.kind, ref.target_id, limit)}

    if nira_hq is not None:
        @mcp.tool(annotations=read_only, structured_output=True)
        def read_nira_project(project: str) -> dict[str, Any]:
            """Read a Nira HQ project by key or UUID, with its brief, issue counts, epics and linked documentation."""
            record = nira_hq.project(project)
            return {**record, "documentation": store.sources("nira_hq_project", record["id"], 50)}

        @mcp.tool(annotations=read_only, structured_output=True)
        def read_nira_ticket(ticket: str) -> dict[str, Any]:
            """Read a Nira HQ task, bug, research issue or epic by key or UUID, including linked documentation."""
            record = nira_hq.issue(ticket)
            return {**record, "documentation": store.sources("nira_hq_issue", record["id"], 50)}

    @mcp.tool(annotations=read_only, structured_output=True)
    def page_history(id: str, revision: int | None = None) -> dict[str, Any]:
        """List saved revisions, or read the Markdown and metadata from one revision."""
        if revision is None:
            return {"revisions": store.history(id)}
        return store.revision(id, revision)

    if store.write:
        @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False), structured_output=True)
        def create_page(
            title: str, markdown: str, parent_id: str | None = None, request_id: str | None = None,
        ) -> dict[str, Any]:
            """Create a Markdown page. Reuse an optional request UUID for retries of the same inputs."""
            return store.create(title, markdown, parent_id, request_id)

        @mcp.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False), structured_output=True)
        def update_page(
            id: str, expected_revision: int, title: str, markdown: str,
            parent_id: str | None = None, move_to_root: bool = False,
        ) -> dict[str, Any]:
            """Replace title and Markdown at a known revision. Set parent_id or move_to_root to move."""
            if move_to_root and parent_id is not None:
                raise Invalid("Specify one destination for the page.")
            return store.update(
                page_id(id), expected_revision, title, markdown,
                parent=parent_id, move=parent_id is not None or move_to_root,
            )

    return mcp
