import asyncio
import json
import os
import sys

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion

from mcp_client import MCPClient

load_dotenv()

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4000


def to_claude_tools(mcp_tools) -> list[dict]:
    """Convert MCP tool definitions into the Anthropic API tool format."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


def tool_result_text(result) -> str:
    if result is None:
        return ""
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    return "\n".join(parts) if parts else json.dumps(
        [c.model_dump() for c in result.content]
    )


class DocMentionCompleter(Completer):
    """Suggests document ids when the word being typed starts with '@'."""

    def __init__(self, doc_ids: list[str]):
        self.doc_ids = doc_ids

    def get_completions(self, document, complete_event):
        word = document.text_before_cursor.split(" ")[-1]
        if not word.startswith("@"):
            return
        prefix = word[1:]
        for doc_id in self.doc_ids:
            if doc_id.startswith(prefix):
                yield Completion(doc_id, start_position=-len(prefix))


async def add_mentioned_docs(client: MCPClient, doc_ids: list[str], query: str) -> str:
    """Read every @mentioned document as a resource and inject it into the prompt."""
    mentioned = [doc_id for doc_id in doc_ids if f"@{doc_id}" in query]
    if not mentioned:
        return query

    context = []
    for doc_id in mentioned:
        uri = f"docs://documents/{doc_id}"
        print(f"  [resource] {uri}")
        content = await client.read_resource(uri)
        context.append(f'<document id="{doc_id}">\n{content}\n</document>')

    return (
        "The user mentioned these documents. Their contents are included below, "
        "so you don't need a tool to read them.\n\n"
        + "\n".join(context)
        + f"\n\n<query>\n{query}\n</query>"
    )


async def run_turn(
    claude: AsyncAnthropic,
    client: MCPClient,
    tools: list[dict],
    messages: list[dict],
) -> str:
    """Send messages to Claude, executing tool calls via MCP until Claude is done."""
    while True:
        response = await claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            messages=messages,
            tools=tools,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return "".join(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  [tool] {block.name}({json.dumps(block.input)})")
            try:
                result = await client.call_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_result_text(result),
                        "is_error": bool(result and result.isError),
                    }
                )
            except Exception as e:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": tool_results})


async def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).")
        sys.exit(1)

    claude = AsyncAnthropic()

    async with MCPClient(command=sys.executable, args=["mcp_server.py"]) as client:
        tools = to_claude_tools(await client.list_tools())
        doc_ids = await client.read_resource("docs://documents")
        print(f"Connected to DocumentMCP. Tools: {[t['name'] for t in tools]}")
        print(f"Documents (mention with @): {doc_ids}")
        print("Ask a question (Ctrl+C or 'exit' to quit).\n")

        # Autocomplete needs a real terminal; fall back to input() when piped.
        prompt_session = None
        if sys.stdin.isatty():
            prompt_session = PromptSession(
                completer=DocMentionCompleter(doc_ids),
                complete_while_typing=True,
            )

        messages: list[dict] = []
        while True:
            try:
                if prompt_session:
                    query = (await prompt_session.prompt_async("> ")).strip()
                else:
                    query = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not query:
                continue
            if query.lower() in {"exit", "quit"}:
                break

            prompt = await add_mentioned_docs(client, doc_ids, query)
            messages.append({"role": "user", "content": prompt})
            answer = await run_turn(claude, client, tools, messages)
            print(f"\n{answer}\n")


if __name__ == "__main__":
    asyncio.run(main())
