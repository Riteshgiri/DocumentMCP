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


class CommandCompleter(Completer):
    """Completes /commands (MCP prompts), their document argument, and @mentions."""

    def __init__(self, doc_ids: list[str], prompts):
        self.doc_ids = doc_ids
        self.prompts = prompts

    def _complete_doc_ids(self, prefix: str):
        for doc_id in self.doc_ids:
            if doc_id.startswith(prefix):
                yield Completion(doc_id, start_position=-len(prefix))

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # "/for" -> suggest prompt names
        if text.startswith("/") and " " not in text:
            prefix = text[1:]
            for p in self.prompts:
                if p.name.startswith(prefix):
                    yield Completion(
                        p.name, start_position=-len(prefix), display_meta=p.description
                    )
            return

        word = text.split(" ")[-1]

        # "/format pl" -> suggest document ids for the prompt's argument
        if text.startswith("/"):
            yield from self._complete_doc_ids(word)
            return

        # "what's in @rep" -> suggest document ids
        if word.startswith("@"):
            yield from self._complete_doc_ids(word[1:])


async def run_command(client: MCPClient, prompts, query: str) -> list[dict] | None:
    """Turn '/name arg1 arg2' into the messages of the matching MCP prompt."""
    name, *values = query[1:].split()
    prompt = next((p for p in prompts if p.name == name), None)
    if prompt is None:
        print(f"Unknown command /{name}. Available: {[f'/{p.name}' for p in prompts]}")
        return None

    arg_names = [a.name for a in prompt.arguments or []]
    required = [a.name for a in prompt.arguments or [] if a.required]
    args = dict(zip(arg_names, values))
    missing = [n for n in required if n not in args]
    if missing:
        usage = " ".join(f"<{n}>" for n in arg_names)
        print(f"Usage: /{name} {usage}")
        return None

    print(f"  [prompt] {name}({json.dumps(args)})")
    prompt_messages = await client.get_prompt(name, args)
    return [
        {"role": m.role, "content": m.content.text}
        for m in prompt_messages
        if m.content.type == "text"
    ]


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
        prompts = await client.list_prompts()
        print(f"Connected to DocumentMCP. Tools: {[t['name'] for t in tools]}")
        print(f"Documents (mention with @): {doc_ids}")
        print(f"Commands: {[f'/{p.name}' for p in prompts]}")
        print("Ask a question (Ctrl+C or 'exit' to quit).\n")

        # Autocomplete needs a real terminal; fall back to input() when piped.
        prompt_session = None
        if sys.stdin.isatty():
            prompt_session = PromptSession(
                completer=CommandCompleter(doc_ids, prompts),
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

            if query.startswith("/"):
                prompt_messages = await run_command(client, prompts, query)
                if not prompt_messages:
                    continue
                messages.extend(prompt_messages)
            else:
                prompt = await add_mentioned_docs(client, doc_ids, query)
                messages.append({"role": "user", "content": prompt})
            answer = await run_turn(claude, client, tools, messages)
            print(f"\n{answer}\n")


if __name__ == "__main__":
    asyncio.run(main())
