# MCP Document Project

A document management MCP server (`read_doc_contents`, `edit_document`), an MCP client, and a Claude-powered CLI that uses them.

## Setup

```bash
uv sync
cp .env.example .env   # then put your ANTHROPIC_API_KEY in .env
```

## Run

- **Inspector:** `uv run mcp dev mcp_server.py`, then open the URL it prints, click **Connect**, go to **Tools**, and click **List Tools**.
  - If port 6274/6277 is taken: `CLIENT_PORT=6284 SERVER_PORT=6287 uv run mcp dev mcp_server.py`
- **Client test:** `uv run mcp_client.py` prints the tool definitions and reads `report.pdf`.
- **Chat app:** `uv run main.py`, then ask: `What is the contents of the report.pdf document?`

Note: this project pins `mcp<2` because MCP SDK 2.x renamed `FastMCP` to `MCPServer`.
