"""Run the SaleWell stdio MCP server with: python mcp_server.py"""
from mcp.server.fastmcp import FastMCP
from flask_app.rag_service import answer_question, default_index

mcp = FastMCP("salewell-smart-tank")


@mcp.tool()
def search_knowledge_base(query: str, limit: int = 5) -> dict:
    """Search SaleWell product, installation, operation, and support docs."""
    return {"results": [x.to_dict() for x in default_index.search(query, limit)]}


@mcp.tool()
def ask_knowledge_base(question: str, limit: int = 5) -> dict:
    """Answer a question using only SaleWell knowledge documents."""
    return answer_question(question, limit)


@mcp.tool()
def refresh_knowledge_base() -> dict:
    """Re-index the knowledge documents after they change."""
    return {"chunks": default_index.refresh(force=True)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
