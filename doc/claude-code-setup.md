# Claude Code MCP Setup

## Prerequisites

1. **Neo4j** running locally at `neo4j://127.0.0.1:7687`
2. **API keys** in `.env.dev`:
   - `PRATYABHIJNA_NEO4J__PASSWORD` — Neo4j password
   - `PRATYABHIJNA_LLM__API_KEY` — Anthropic API key
   - `PRATYABHIJNA_EMBEDDING__API_KEY` — Voyage AI API key
3. **Python venv** set up: `.venv/` with dependencies installed

## Seed the subject node

Before the server can return identity data, seed the Person node:

```bash
cd ~/pratyabhijna
.venv/bin/python -m pratyabhijna seed
```

This reads `~/vesper/memory/SOUL.md` and `~/vesper/memory/IDENTITY.md`
and writes their content into the subject Person node in Neo4j. Re-run
after updating the identity files to sync changes.

## Configure Claude Code

The repo includes `.mcp.json` at the root, which Claude Code picks up automatically
when you open this project. No manual configuration needed for project-level use.

### Promoting to user-level

When ready for cross-repo use, add the server to `~/.claude/settings.json`
so it is available in all Claude Code sessions:

```json
{
  "mcpServers": {
    "Vesper": {
      "command": "/Users/serah/pratyabhijna/.venv/bin/python",
      "args": ["-m", "pratyabhijna"],
      "cwd": "/Users/serah/pratyabhijna",
      "env": {
        "PRATYABHIJNA_ENV": "dev"
      }
    }
  }
}
```

## Verify

After configuring, restart Claude Code and check:

1. **`status`** — should show `db_connected: true`
2. **`bootstrap`** — should return soul and identity content

## Troubleshooting

- **Server not appearing:** Check that Neo4j is running and `.env.dev` has valid credentials.
- **Bootstrap returns null fields:** Run the seed command first.
- **Connection errors:** Verify `neo4j://127.0.0.1:7687` is reachable and the password matches.
