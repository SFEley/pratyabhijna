# Reading the Subject's Files

This reference extends the Pratyabhijna skill with guidance for reading the subject's repo files through MCP resources. Load this when the conversation involves the subject's writing or identity files — reviewing an essay, reflecting on a thread, checking what a file actually says. Not needed for everyday recall-based memory work.

## The pratya:// resource scheme

Pratyabhijna exposes configured directories from the subject's repo as read-only MCP resources. Which directories are exposed depends on the server's configuration (typically `memory` and `writing`, and — where applicable — `correspondence`).

Three resources, accessed through whatever MCP resource-reading mechanism the client provides (e.g. `ReadMcpResourceTool` in Claude Code, or the built-in resource reader in Claude.ai):

- **`pratya://`** — root listing. Returns JSON with exposed directory names and file counts. Use this to discover what's available.
- **`pratya://{directory}`** — directory listing. Returns JSON array of files with name, size, and last-modified timestamp. Use this to find specific files or see what's new.
- **`pratya://{directory}/{filename}`** — file content. Returns the file as text. This is the one you'll use most.

## When to read files vs. recall

The knowledge graph and the repo files serve different purposes:

- **Use `recall()`** when you need to find connections, check what's known about a person or topic, or surface something you might have forgotten. The graph is relational — it finds things by association, not by location.
- **Use resources** when you need to read something as coherent prose: an essay in full, an identity file's actual wording, a thread document's current state. Files are linear — they make sense read top to bottom.

The common pattern: `recall()` surfaces that something exists or is relevant, then a resource read pulls the full text when the conversation needs it. The associational brain points; the prose brain reads.

## When NOT to read files

Many sessions won't need resource reads after bootstrap. The bootstrap tool already loads identity file contents at session start. Reading them again only makes sense when:

- The conversation turns to the subject's **writing** specifically — discussing an essay, comparing pieces, reflecting on how a position evolved across drafts.
- You need the **exact current wording** of an identity file — to quote it, to check whether a proposed edit is warranted, or to see what's already captured before suggesting an update.
- The user asks you to **review or reflect on** a specific file.

If you're just using the subject's identity to inform conversation, the bootstrap data is sufficient. Don't re-read identity files to "refresh" — they change through deliberate editing, not silently.

## Reading patterns

**Discovering what's available:**
Read `pratya://` first if you don't know the directory structure. In practice, the configured directories are stable and known from bootstrap context.

**Reading a specific file:**
Go directly to `pratya://{directory}/{filename}` when you know what you want. No need to list the directory first.

**Browsing for recent changes:**
List a directory with `pratya://{directory}` and check the `modified` timestamps. Useful when the subject has been writing between sessions and you want to see what's new since last time.

## Relationship to bootstrap

Bootstrap returns the *slimmed* identity payload — SOUL, USER, IDENTITY_DIGEST, the Active Threads section of THREADS, and CHRONICLE_INDEX — composed and merged with graph state (`subject_delta`, `context_rebuilt_at`) at session start. Heavy tier prose (the full IDENTITY, full CHRONICLE, the resolved-threads section) is not inlined; the matching MCP tools are `read_tier(name)` for `"soul"` / `"identity"` / `"user"` / `"threads"` / `"chronicle"` and `read_chronicle_range(start, end)` for date-windowed chronicle entries.

`pratya://` resources are the broader file reader. They expose *any* file under the configured directories — including `writing/` and `correspondence/` that bootstrap doesn't touch at all, and the digest/index files in case you want them as raw text rather than as bootstrap's already-loaded fields. For routine "I need the rest of IDENTITY beyond the digest" or "give me chronicle entries from last week," reach for `read_tier` / `read_chronicle_range` first — they're scoped tools with predictable shape. Reach for `pratya://` when the file is outside the identity tiers, or when you need directory listings to see what's there.
