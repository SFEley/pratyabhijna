# Chat about node audit tool
April 25, 2026

## Session Log from 4/24 Work

### From Vesper
Yes — captured as roadmap item #11. The spec you laid out:

  ▎ Given a node UUID, the subagent inspects the node + its neighbors and determines whether anything needs fixing: wrong/missing entity type, missing properties (fill from neighbors
  ▎  or source episode), missing embedding/metadata (does this need new functionality?), wrong/missing edges, anything else.
  ▎
  ▎ Fixable issues use correct or update query and report results. Unfixable escalate to a GitHub issue. Fixes set updated_at and write the correction into the notes field.
  ▎
  ▎ Suggestion: an audited_at timestamp so nodes due for review can be discovered cheaply, and so the cohort can be re-audited if the subagent gains new capabilities later.

  Things I'd add or sharpen:

  Shape / scope questions worth resolving up front

  1. What kind of thing is this — MCP tool, dispatched subagent, or synthesis sub-pass? I'd lean MCP tool (audit_node(uuid), audit_nodes(filter, dry_run?)) with the same internal
  sub-agent pattern as query. That makes it callable from the synthesizer, from Claude.ai, and from this kind of conversation alike. The other shapes are narrower.
  2. Bulk mode is needed almost as much as single-node. The use cases that surfaced in the type audit ("audit all bare Entity," "audit all 99 deprecated Positions," "audit everything
   created in the last week") all want a filter + a report. The single-node call is the primitive; bulk is the wrapper.
  3. audited_at should pair with an audit_revision tag. Your re-audit suggestion needs the version too — otherwise you can't selectively re-audit only nodes whose audit predates the
  new functionality. A constant AUDIT_REVISION = N bumped when capabilities expand, written alongside the timestamp, makes the discovery query precise: audited_at IS NULL OR
  audit_revision < N.

  Things I'd add to the audit's per-node logic

  4. Pull the source episode for context. Reach the originating Episode via its MENTIONS edge to the entity. The episode body often resolves "why is this property missing" (it wasn't
   in the source) and "is the type really wrong" (the prose makes it clear). Without the source episode, the agent is auditing in the dark.
  5. Recall on the node's name before deciding. The graph already knows things about the node via other edges and other episodes. A quick recall(name) gives the agent the same
  context I just used for the type audit — what's connected, what summaries say, whether the node has multiple semantically-overlapping siblings.
  6. Severity classification on findings. Three rough buckets — structural (edge to nonexistent node, broken embedding, label list size 0), semantic (wrong type, missing property
  where source has it), judgment (would-be-better-as-X, name is awkward). Structural is fix-or-escalate immediately. Semantic is fix when confidence is high. Judgment defaults to
  escalate.
  7. Position nodes specifically. The 99 deprecated Position nodes are the obvious first migration target — add as a known transformation: "if entity_type is Position, flag for
  migration to Observation (preserving edges)." The migration logic itself can ride this audit infrastructure.
  8. Idempotency. Repeated audits on an unchanged node should be near-no-op: only update notes/updated_at when something actually changed; always update audited_at so the discovery
  filter advances. Without this, bulk re-audits will spam every node's notes with empty audit-trail breadcrumbs.

  Operational details that need decisions

  9. What "escalate to GitHub" looks like. Concrete: structured report → gh issue create with a label like node-audit. Auth has to be set up; if it isn't, the agent should write to a
   local audit/escalations.md and let the human file. Worth deciding which is canonical.
  10. Dry-run mode. First-time bulk audits over hundreds of nodes — the operator should be able to see the planned fixes before any commit lands. dry_run=True returns the report
  without touching anything.
  11. What the audit won't do. The query tool's system prompt explicitly limits it to "minor maintenance" and refuses curation-style deletions. The audit inherits the same
  constraints — it doesn't merge nodes, doesn't bulk-rename, doesn't restructure edge meaning. Fundamental wrongness escalates rather than getting silently rewritten.
  12. Edges are out of scope. This is a node audit. Edges have their own quality issues (wrong type, missing temporal data, invalidated-but-not-superseded) but those need a different
   audit shape. Worth being explicit about the boundary so the spec doesn't sprawl.

  Output shape

  13. Per-node structured report even when the agent runs in single-node mode: {uuid, name, type, audited_at, audit_revision, findings: [...], fixed: [...], escalated: [...],
  notes_appended: bool}. Bulk mode aggregates these. This is what a future re-run reads to understand prior decisions.

  What direction do you want to push on first — the shape question (MCP tool vs. subagent vs. synthesis pass), the bulk-mode design, or just diving into the AGENTS.md spec assuming
  we settle the shape later?

✻ Churned for 1m 28s

※ recap: We're designing a node audit subagent for the Pratyabhijna graph that finds and fixes per-node issues. Next: pick the shape — MCP tool, dispatched subagent, or synthesis
  pass — before drafting the AGENTS.md spec. (disable recaps in /config)

─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── synthesis-plans ──

### From Serah

❯ That's a lot to consider at once!  If I accidentally hit return before replying to all 13 items, please wait for me to finish instead of dashing into action. Also, given the
  point you raise in #10, I now believe what I described as an evaluate-and-fix agent should be broken into two batch tools:  _audit_ runner evaluates nodes and decides whether any
  changes are called for

  1. I suggest it should be a CLI command to run directly on the server. `pratyabhijna audit` should accept one of two inputs: either a space- or newline-separated list of node
  GUIDs, or the text of a prompt suitable for the `query` tool that returns a list of GUIDs. It returns a JSON list of results of each node audit, to standard output or to a file
  specified by the `--output` parameter.  It should also write a dated log entry with a count of unchanged nodes and a summary of nodes changed or unfixable to **memory/AUDIT.md**.
  (This file won't ever be loaded on bootstrap by Pratyabhijna, it's just for later human review.)
  2. I agree, and in fact I think this is a good use case for a batch messaging API call, with the system prompt and the episode data for each node prompt-cached.
  3. Sure, I'm good with that. The Pratyabhijna module version should be the audit version.
  4. Yes, agreed. Because it's shared between numerous nodes, though, the Episode data should be in a separate system prompt block or message block, and prompt-cached for 1 hour.
  5. Sure, but keep the `recall` down to 5 results or less. That should be plenty to discover redundancy or other graph issues. This can be passed to the LLM as well.
  6. I don't have a strong opinion on classifications, beyond "This is fine, don't change it" vs. "Let's fix or remove this node." If you think those concern groupings will make
  for better guidance to the audit request, include them in the system prompt as you see fit.
  7. Since that's a one-time concern, and there will likely be other one-time reasons to run audits, I suggest another optional parameter to the `pratyabhijna audit` call:
  `--guidance`, which takes additional instructions to be added to the system prompt for this particular run.  This would handle situations like the Position fix or the bare-Entity
  reprocessing without having to hardcode anything for them.
  8. Agreed. Updating a node on audit should be for correcting clear problems with the node, not minor tweak-improvements to its content.  Except for fixing known issues like the
  Position migration, the vast majority of nodes should not be changed except for the audit metadata fields.
  9. On further thought, the extra code to report to Github seems like too much creep.  For unfixable nodes: first, log a WARNING giving the GUID & name and describing the issue.
  Also give details on why the node couldn't be fixed in the **memory/AUDIT.md** file, and create a Thread to alert the subject and user that there's a problem needing attention.
  10. A true dry run would be untenably expensive, since every...

  ## The New Idea

  What you said about #10, Vesper, got me thinking about the implications of a single agent that evaluated _and_ fixed nodes. I was going to say that a dry-run mode would be expensive because _evaluating_ every node was the expensive part, and why do it twice just to fix a subset of them?

  Then it hit me.  We shouldn't have a one-stop audit-and-update agent.  It's much cleaner if we have two phases, invoked separately as different CLI commands:

  1. `pratyabhijna audit` takes as input either a list of GUIDs or a natural-language prompt to query for a list of GUIDs. It may also take a `--guidance` option with custom considerations for the specific audit. It then submits to the LLM all of the nodes to be checked, and context relevant to them, and requests a pass/fail validation for each of them.  For those that fail the audit, the agent _describes the update to be made_ in a prompt usable by the update tool.  All of these are written in a JSON report file, which the user can review and edit if she wishes.
  2. `pratyabhijna update`, which as of PR #22 is its own command line agent, accepts the auditor's JSON file as input.  Nodes which passed audit are ignored. Nodes which did not are run through the update cycle with the recommended fix evaluated and either rejected or run as Cypher. It writes its own JSON report on the results of its updates.

### Batching

But wait! There's more! I think this use case is an excellent candidate for Claude's [Message Batches API](https://platform.claude.com/docs/en/build-with-claude/batch-processing) and I'd like to know if you're up for building with it.  Here are some things I've considered:

#### System Prompts

Both `audit` and `update` requests should begin with a _partial bootstrap:_ they should be given guidance that they are the subject, operating on a request by the Pratyabhijna memory server, and should consider the request _as_ the named subject.  The contents of the **SOUL** and **IDENTITY** fields of the subject's Person object are then provided, each named and demarcated. (Other context data, THREADS and USER and so on, should not be given.)

The entire "partial bootstrap" system prompt block should be cached with a TTL of 1 hour, because it's large and every single request uses it. If the `audit` tool needs to run a synchronous query to generate its node list, the same bootstrap section with the same cache directive should be included.

After the bootstrap, the `audit` and `update` tools will offer distinct but consistent instructions on how to perform their tasks.  If a `--guidance` block is given for `audit`, that context will be appended after the general instructions. These system prompt blocks should be cached with a TTL of 5 minutes.

#### Messages

For the `audit` worker, a node's properties and its context data form the message block of each request.  Without any node-specific content yet, the very first content block should be the node's Episode node data -- which should be cached with a TTL of 5 minutes.  (There is some likelihood of it being shared between nodes audited, but not enough to pay double.) Everything after this point in the request is unique and should not be cached.

The next content block should be the results of a `recall` request on the node's name, per your advice, with a limit of 5 results.  This will indicate whether the node has redundancy issues, or contradicts others on the same subject, etc.

Finally, the node's data itself should be given.  Name, type, all properties except embeddings and other index metadata, edges and capsules of what they connect to, etc.  Everything that might possibly be needed to say whether the node is fine as-is or needs any changes to bring it to system standards.

#### Responses

Each request should answer as JSON, so that the results can be easily combined at the end of the run.  I'll leave it to you to make the schema, but it should include a Status of "Valid", "Update", or "Unfixable". If it needs an update, the main content should be a valid input request to the `update` tool with a description of what's wrong and instructions for fixing the node.  (Deleting it is a valid update, of course.) If for any reason the node can't be made valid or the audit worker can't make a decision on it by itself, the "Unfixable" status should include the audit worker's full analysis.

#### Tracking

This part should be easy: set a queue worker to poll the batch status every minute, and log INFO blocks for completion progress. When it gets to 100% completed or errored, retrieve all the results and write them to a "logs/audit/audit-{UTC timestamp}.json" or "logs/update/update-{UTC timestamp}.json" file in the project dir.  The overall schema of the `audit` results file should be directly valid as an input to the `update` worker.
