# Personal Knowledge Operating System: Project Proposal

Oct 4, 2026 · @Kamal

## Problem and thesis

A person's own information is relational and time bound, and flat vector retrieval loses both properties. This project builds a self hosted personal knowledge operating system: it ingests email, calendar, notes, files and chat through MCP connectors, extracts a bitemporal knowledge graph of people, projects, events and commitments, and answers questions over that graph with citations back to the source item.

Three question types motivate the design, and none of them is answered well by chunk and embed:

1. Relational. "Who introduced me to Sarah?" No single document states this. The answer is structural: a thread where a third party included both of you.
2. Temporal. "Who was on this project in March?" A flat store overwrites the old fact when a new one arrives, so the past becomes unanswerable.
3. Obligational. "What did I agree to do for the design review?" This needs commitments as first class objects with an owner, a due date and a state.

The thesis is that a bitemporal graph plus hybrid retrieval plus strict provenance is the right substrate for personal data, and that this can run entirely on a user's own machine. The build is scoped so that each phase ships something usable rather than waiting for the full system.

Scope note. This is an engineering and systems project with an evaluation component, not a claim of novel research. The novelty sits in applying temporal graph memory to multi source personal data, and in measuring it honestly.

## Literature and prior systems

The design borrows the bitemporal model from Zep, the community layer from GraphRAG, graph traversal retrieval from HippoRAG, and extraction discipline from the personal knowledge graph literature. None of them combines all four over multi source personal data with provenance back to the original item.

| System | Core idea to borrow | Gap for this project |
| --- | --- | --- |
| [Zep and Graphiti](https://arxiv.org/abs/2501.13956) | Bitemporal edges with validity windows; superseded facts invalidated, not overwritten | Built for conversational agent memory, not multi source personal archives |
| [GraphRAG](https://arxiv.org/abs/2404.16130) | Leiden community detection with precomputed community summaries for global questions | Static corpus, no time model, costly to reindex |
| [HippoRAG](https://arxiv.org/abs/2405.14831) | Personalized PageRank over an extraction graph for single step multi hop retrieval | No temporal reasoning; retrieves passages rather than facts |
| [Mem0](https://arxiv.org/abs/2504.19413) | Explicit add, update, delete and noop memory operations; token cost as a first class metric | Facts are mostly flat; the graph variant is a secondary mode |
| [PKG survey](https://arxiv.org/abs/2304.09572) | Taxonomy of personal information and population from email, files and calendar | Largely conceptual, few end to end open implementations |
| [Ditto](https://arxiv.org/abs/2004.00584) | Serialize record pairs for a language model, with blocking before matching | Benchmarked on product and bibliographic data, not personal identities |

Seven findings shape the decisions in the rest of this document.

1. Bitemporal modelling is the differentiator. Zep maintains a timeline of facts and relationships with explicit periods of validity, so the graph can represent an evolving world without losing history, and can answer both the current and the historical question.
2. Hybrid retrieval beats pure vector search. Zep combines semantic, keyword and graph search and reports 94.8 percent against 93.4 percent for MemGPT on Deep Memory Retrieval, with gains up to 18.5 percent and 90 percent lower latency on LongMemEval.
3. Graph structure buys multi hop retrieval cheaply. HippoRAG seeds Personalized PageRank with query concepts and matches or beats iterative retrieval in a single step, reporting 89.1 percent Recall@5 on 2WikiMultiHopQA against 68.2 percent for ColBERTv2.
4. Communities answer what retrieval cannot. GraphRAG partitions the graph with hierarchical Leiden clustering and pregenerates summaries, with root level summaries needing 9 to 43 times fewer tokens per query than direct text summarization. For a personal archive this is what answers a question like what I have been spending my time on this semester.
5. Token cost is reportable. Mem0 reports 92.5 on LoCoMo and 94.4 on LongMemEval at roughly 6,900 tokens per retrieval call against 25,000 or more for full context.
6. Benchmarks have known limits. LoCoMo covers only 10 conversations and about 1,700 question answer pairs, and its statistical validity has been criticised, so published gains belong in this proposal with that caveat attached.
7. Entity matching does not require a large model. A fine tuned Ditto at 110 million parameters reaches F1 72.9 on unseen datasets, on par with most prompted large models at far higher throughput, which supports a cheap first pass with an LLM reserved for hard pairs.

## Architecture

&#91;embedded content: system architecture, four layers\]

Sources enter as episodes through MCP connectors, pass a three stage extraction cascade that spends model calls only on what survives filtering, and land in one bitemporal store. The planner and the agents read from that store alone, which is why every answer can name the episode behind it.

## Data model

Every fact carries two timelines and a pointer to the item it came from. Valid time is when the fact was true in the world. Transaction time is when the system learned it. That pair is what makes point in time questions answerable and what makes a wrong extraction reversible without destroying history.

Six tables carry the system.

1. `episodes`. One row per ingested item: source, external id, raw content, timestamp, participants, content hash. Nothing is extracted twice because the hash is unique.
2. `entities`. Canonical people, organisations, projects, places, topics and documents, with a type, a canonical name and an embedding.
3. `aliases`. Surface forms pointing at an entity: email addresses, display names, handles, nicknames, with a confidence and the resolution method that produced them.
4. `facts`. The edges. Subject entity, predicate, object entity or literal, `valid_from`, `valid_to`, `recorded_at`, `invalidated_at`, confidence, and the episode that produced it. A superseded fact gets `valid_to` set; it is never deleted.
5. `commitments`. Promisor, promisee, the text of the obligation, due date, state, and the episode it came from. Modelled separately because the user interface and the daily brief both query it directly.
6. `communities`. Leiden clusters over the entity graph with generated summaries, refreshed on a schedule rather than per write.

Three rules keep the graph trustworthy.

1. No unsourced fact. Every row in `facts` references an episode. An answer that cannot name its source is a bug, not a degraded answer.
2. No destructive update. Contradiction closes the old interval and opens a new one. The reader chooses whether to ask for current truth or truth as of a date.
3. Confidence travels. Extraction confidence and resolution confidence are stored, surfaced in the interface, and used to filter what reaches an answer.

The predicate set stays small and closed at first, roughly twenty relations such as `works_with`, `member_of`, `attended`, `authored`, `mentioned`, `introduced`, `owes`, `due_on`. An open predicate vocabulary looks attractive and then fragments into near duplicates that make retrieval worse.

## Ingestion and extraction

MCP is the connector layer, which means every new source is a server rather than a bespoke integration. Each connector normalises into one episode shape: source, external id, timestamp, participants, title, body, references. Everything downstream reads episodes only, so adding a source never touches the extraction or retrieval code.

Ingestion runs incrementally with a per source watermark and a content hash, so a reprocess costs nothing for unchanged items. Order of sources by value for effort: email first, since it carries people, commitments and threads; then calendar, which is cheap and gives the time skeleton; then notes and documents; then chat, which is high volume and low density.

Extraction is a three stage cascade, which keeps cost down by refusing to send most text to a model.

1. Filter. Rule based drops of newsletters, automated notifications, receipts and calendar invitations already covered by the calendar connector. On a typical mailbox this removes the majority of items before any model call.
2. Structural extraction. Headers, participants, thread identifiers, attachments and explicit dates come from parsing, not inference. These facts are free and exact, and they are the backbone of relational questions such as who introduced whom.
3. Semantic extraction. A constrained model call per surviving episode returns entities, facts and commitments as structured output, each with a span pointing at the text it came from and a confidence. The prompt carries the closed predicate list and the entity types, and the output is schema validated before it is written.

Facts are reconciled rather than appended. When a new fact contradicts an existing one on the same subject and predicate, the pipeline closes the old interval and opens a new one, following the add, update, delete and noop pattern Mem0 reports and the invalidation model Zep uses. Contradiction is detected by predicate cardinality: a person has one current employer but many collaborators.

Two controls keep the archive affordable. A per run budget caps model spend and processes by recency when the cap binds, and a local model path through Ollama lets a user run extraction entirely offline at lower quality. Extraction cost should be published per thousand items so people can predict what their own archive will cost.

## Connecting the sources

Sources connect over their own APIs, not file exports, and each one syncs into the episode store before anything else reads it. Keeping that local copy is what lets extraction be re-run after a prompt change without re-hitting an API or burning quota.

| Source | Access path | Incremental sync |
| --- | --- | --- |
| Gmail | Gmail API, OAuth2 installed app flow, refresh token stored locally | History list against a stored history id |
| Outlook and Microsoft 365 | Microsoft Graph, Mail.Read and Calendars.Read scopes | Built in delta queries on the messages endpoint |
| Granola | [Public REST API](https://www.scalekit.com/blog/granola-mcp-vs-api) at public-api.granola.ai/v1 with an API key, or the hosted MCP server | Cursor pagination with date range filters, 30 notes per page |

Granola is the right first connector. An API key takes minutes, the notes are dense and high value, and it proves the episode schema before Gmail OAuth setup costs an afternoon.

Credentials are the one place the distribution model forces a decision. Every Gmail scope that can read a mailbox is a restricted scope, so shipping a hosted version to other users would require Google verification plus an annual third party CASA security assessment, with reviews quoted at [four to six weeks](https://www.nango.dev/docs/api-integrations/google-shared/google-security-review.md). While the app stays in testing with the developer as a test user, none of that applies.

The self hosted design resolves this cleanly. Each user creates their own cloud project, their own OAuth client, and adds themselves as a test user, so no verification is ever needed and no credentials pass through anyone else's infrastructure. The cost is a setup section in the README. This is a further argument for local first deployment rather than a workaround for it.

API surfaces and review requirements in this area change often, so the current provider docs win over anything written here.

## Entity resolution

A bad merge is the failure users notice first, so the pipeline is tuned for precision and sends everything uncertain to a review queue rather than guessing. The same person appears as a work address, a personal address, a display name, a chat handle and a nickname, and personal archives offer no clean identifier to join on.

Four stages, in the order the entity matching literature recommends.

1. Blocking. Candidate pairs come from cheap keys: normalised name tokens, email local part, domain, and approximate nearest neighbours over name embeddings. This cuts the comparison space from quadratic to something linear in practice.
2. Deterministic rules. Exact email match, or an identical handle on the same platform, resolves immediately with confidence 1.0. In a personal archive this settles the clear majority of pairs at zero model cost.
3. Scored matching. For surviving pairs, a feature vector combines string similarity on names, embedding similarity, co occurrence in threads and events, and domain overlap. A small supervised model or a tuned linear score produces a probability. Ditto class models reach F1 in the seventies on unseen domains at very high throughput, which is enough for a first pass.
4. LLM adjudication. Only the uncertain band, roughly 0.4 to 0.8, goes to a model, with both records serialized and the shared context included. Anything still below threshold becomes a review card in the interface: two profiles side by side, merge or keep separate.

Merges are reversible. A merge writes an alias row and an operation record rather than rewriting history, so an unmerge restores the previous state without reprocessing. Transitivity is applied with care, since blindly closing transitive chains is a known source of false positives in multi source matching.

The evaluation for this component is separate from the question answering evaluation: a hand labelled set of a few hundred pairs from the author's own archive, reporting pairwise precision, recall and the share of pairs sent to review. Precision is the number that matters, and the target is above 0.98 with recall traded away as needed.

## Retrieval and query planning

A planner classifies the question first, because the four question classes need different machinery and running all of them on every query is what makes hybrid systems slow.

| Question class | Example | Retrieval path |
| --- | --- | --- |
| Lookup | What is Priya's address | Vector plus BM25 over facts and episodes, reranked |
| Relational | Who introduced me to Sarah | Entity linking, then Personalized PageRank seeded on the query entities |
| Temporal | Who was on this project in March | Graph traversal filtered to facts whose validity window covers the date |
| Aggregate | What have I spent time on this semester | Community summaries over the period, in the GraphRAG style |

The planner is a small constrained model call that outputs the class, the entities it recognises, a date range if one is implied, and the predicates likely involved. Most real questions are mixed, so the planner returns a set of paths and the executor runs them in parallel.

Results from different paths are fused by reciprocal rank, then reranked by a cross encoder, then filtered by extraction confidence. The answer stage receives facts with their source episodes attached, not raw chunks, which is what keeps citations exact and keeps the context small.

Two rules govern the answer itself. The model answers only from retrieved facts and must cite the episode behind each claim, and when retrieval returns nothing above threshold the system says so instead of composing a plausible answer. Abstention is one of the five abilities LongMemEval tests, and on personal data a confident fabrication about your own life is worse than silence.

Community summaries are recomputed on a schedule rather than per write, since Leiden clustering over the whole entity graph is far too expensive to run on every ingested email. A nightly job is enough for a personal archive.

## Agents and the MCP interface

The system consumes MCP on the way in and serves MCP on the way out. Serving it is what turns the project from an application into infrastructure other people build on, and it is the cheapest route to adoption: anyone running Claude, an IDE agent or their own assistant can point it at their own graph.

The server exposes a small tool surface, deliberately narrower than the internal API.

1. `search_memory`. Natural language question in, cited facts out, with the planner behind it.
2. `get_entity`. Everything known about a person, project or organisation, with the validity windows intact.
3. `timeline`. What happened or changed in a window, optionally scoped to an entity.
4. `open_commitments`. Obligations by owner and state.

Three agents run on top of the graph rather than beside it.

1. Daily brief. A scheduled run that reads the calendar, open commitments, threads that have gone quiet and items waiting on the user, then writes a short brief. This is the feature that produces a reason to open the application every day.
2. Commitment tracker. Watches for promises in both directions, resolves them against later episodes that fulfil them, and flags ones approaching a due date without any follow up.
3. Review agent. Works the entity resolution queue and the low confidence facts, proposing merges and corrections for one click approval instead of asking the user to go hunting.

Write access is restricted. Agents can propose merges, corrections and commitment state changes, and all of these land as proposals the user approves. The graph is the user's record of their own life, so silent machine edits to it are a trust failure rather than a convenience.

## Privacy and security

Nobody installs this on trust alone, so the privacy model is a design constraint and the first section of the README, not a policy page. The default deployment is self hosted and single tenant: the database runs on the user's machine and no data leaves it except model calls the user has explicitly enabled.

Seven commitments, each of which is a build task rather than a promise.

1. Local first. Docker Compose on a laptop or home server is the primary target, not a hosted tier.
2. Fully offline mode. Local embeddings and a local model through Ollama let a user run the whole pipeline with no external calls, at lower extraction quality. The quality difference should be measured and published rather than glossed over.
3. Source level control. Each connector can be disabled, scoped to date ranges, or restricted to specific folders and labels before anything is read.
4. Encryption at rest with a user held key, so a stolen disk is not a stolen life.
5. Visible processing log. Every episode processed, every model call made, and what it cost, viewable by the user.
6. Real deletion. Deleting a source deletes its episodes, the facts derived from them, and any commitment or alias that has no other support. Derived data is the part most systems forget.
7. Third party data minimisation. The archive contains other people's words. The graph stores facts about relationships and obligations, and avoids building inferred profiles of third parties such as sentiment or personality judgements.

Two risks deserve naming rather than mitigation theatre. Prompt injection arrives through the content itself, since an email can contain instructions aimed at the extractor, so extracted text is treated as data and never as instructions, and the extraction prompt is structurally separated from the content. Second, an account compromise on a machine running this system exposes far more than any single application would, which is an argument for local only deployment and for making the hosted option opt in at most.

## Evaluation

The evaluation runs on two tracks: public benchmarks so the numbers are comparable, and a private question set over the author's own archive so the numbers are about the actual use case. The eval harness is built in week two, before most features exist, because a system like this degrades silently without one.

Public track. LongMemEval covers the five abilities this system claims, which are information extraction, multi session reasoning, temporal reasoning, knowledge updates and abstention, and its knowledge update and temporal categories are exactly where a bitemporal graph should win. LoCoMo gives a second comparison point against Mem0, Zep and A-Mem, with the caveat that it is small enough that differences of a few points mean little.

Private track. Roughly 150 hand written questions over the author's own email and calendar, split across the four question classes in the retrieval section, each with a gold answer and the source items that support it. Judged by exact match where possible and by a model judge with the gold source attached where not.

Ablations are what make the results interesting, since they test the thesis rather than the implementation.

| Ablation | What it tests |
| --- | --- |
| Vector only, no graph | Whether the graph earns its complexity |
| Graph without validity windows | Whether bitemporality earns its complexity |
| No community summaries | Cost and quality on aggregate questions |
| No planner, all paths always | Latency and token cost of routing |
| Local model against hosted model | The real price of fully offline operation |

Metrics to publish: accuracy per question class, retrieval recall at k, citation precision, which is the share of cited sources that actually support the claim, abstention rate on unanswerable questions, tokens per query, end to end latency at p50 and p95, and extraction cost per thousand items. Entity resolution precision and recall are reported separately, as described above.

Targets are set as ranges rather than promises, since they depend on the archive. The ones worth stating in advance: citation precision above 0.95, false answer rate on unanswerable questions below 0.05, and a measurable gain over the vector only ablation on temporal and relational questions. If the graph does not beat flat retrieval on those two classes, the thesis is wrong and the writeup should say so.

## Stack

The guiding constraint is that a stranger must get this running with one command on their own machine, which rules out anything needing a cluster or a managed service.

| Layer | Choice | Why |
| --- | --- | --- |
| Storage | Postgres with pgvector and Apache AGE or recursive CTEs | One database for relational, vector and graph work; no second engine to operate |
| Ingestion | Python with the MCP SDK | Connectors are the extension point; the ecosystem is here |
| Jobs | Celery or Arq with Redis | Extraction is slow and must not block the interface |
| Models | Pluggable: hosted API or Ollama locally | Offline mode is a stated commitment, so the abstraction is needed from day one |
| Embeddings | A small local sentence model | Runs on a laptop and avoids sending every item to an API |
| Interface | Next.js | Person pages, graph explorer, review queue, daily brief |
| Serving | FastAPI plus an MCP server | Same core, two front doors |
| Packaging | Docker Compose | One command install is the adoption path |

Two choices to justify in the README, since reviewers will ask. Postgres rather than Neo4j, because the traversal depth here is shallow, two or three hops, and keeping vectors and rows in one place removes an entire class of consistency problems from a single user deployment. Note that the Graphiti maintainers argue the opposite for their workload, that a purpose built temporal graph runtime beats general graph storage, and their reasoning applies at multi tenant scale rather than at one user on a laptop.

The second is a closed predicate vocabulary rather than open extraction. GraphRAG runs with no schema at all, which works for sensemaking over a corpus but produces near duplicate relations that hurt precise retrieval over a personal graph.

Vibe coding note. The pieces that survive being written quickly are the connectors, the interface and the agents. The pieces that must be written deliberately, with tests, are the bitemporal fact reconciliation, the entity resolution scoring and the deletion cascade, because all three corrupt data silently when they are wrong and no amount of later polish recovers a corrupted graph.

## Roadmap

&#91;embedded content: build roadmap, four phases and two gates\]

The gates are the point of this schedule. If phase one answers are no better than keyword search over the same mailbox, the extraction is wrong and no amount of graph work fixes it. If phase two does not beat the vector only ablation on temporal and relational questions, the central thesis of the project has failed and the honest move is to write that up rather than build on it.

Phase one is a usable product on its own: ingest email and calendar, extract, search with citations. Everything after it increases capability rather than making it work.

## Risks and open questions

Six risks, ordered by how likely they are to sink the project.

1. Extraction quality caps everything. Every downstream capability inherits the error rate of stage three. Mitigation: the eval harness exists from week two, and the private question set grows as failures are found.
2. Entity resolution errors are highly visible. One wrong merge and the user stops trusting the graph. Mitigation: precision first thresholds, a review queue, reversible merges.
3. Cost on a large archive. A decade of email is hundreds of thousands of items. Mitigation: aggressive filtering, structural extraction before semantic, per run budgets, and a published cost per thousand items.
4. Scope. Graph, retrieval, agents, interface, connectors and privacy is a lot for one person in ten weeks. Mitigation: each phase ships something usable, and the phase one system is genuinely useful without the graph layer.
5. Crowded space. Several projects already do personal retrieval over email and notes. Mitigation: differentiate on the temporal graph, citations and the eval table, not on the chat box, and say plainly in the README what is different.
6. Evaluation honesty. Benchmarks on a self built question set over the author's own data can be tuned to flatter the system. Mitigation: freeze the question set before tuning, publish the ablations including unfavourable ones, and report the benchmark caveats rather than the headline numbers alone.

Four open questions to settle while building.

1. How much does the graph actually beat flat retrieval on a real personal archive, as opposed to on conversational benchmarks where the prior work measured it?
2. What is the usable quality floor for a fully local model in extraction, and is offline mode good enough to be a default rather than a fallback?
3. Does commitment extraction work well enough to be a headline feature, or is it a secondary view over facts?
4. Is there a safe path to multi user households or teams, or does shared data break the privacy model badly enough that it stays out of scope?

## Sources

Pages opened for this proposal.

| Work | Relevance |
| --- | --- |
| [Zep: A Temporal Knowledge Graph Architecture for Agent Memory](https://arxiv.org/abs/2501.13956) | Bitemporal model, edge invalidation, hybrid retrieval, DMR and LongMemEval results |
| [Graphiti](https://github.com/getzep/graphiti) | Open implementation of the temporal graph engine behind Zep |
| [From Local to Global: A Graph RAG Approach to Query-Focused Summarization](https://arxiv.org/abs/2404.16130) | Leiden communities, community summaries, token savings |
| [HippoRAG: Neurobiologically Inspired Long-Term Memory for LLMs](https://arxiv.org/abs/2405.14831) | Personalized PageRank retrieval, single step multi hop |
| [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/abs/2504.19413) | Memory operations, LoCoMo results, token efficiency |
| [LongMemEval](https://arxiv.org/abs/2410.10813) | The five memory abilities used as the evaluation frame |
| [An Ecosystem for Personal Knowledge Graphs](https://arxiv.org/abs/2304.09572) | Personal information taxonomy, population from email and calendar |
| [Deep Entity Matching with Pre-Trained Language Models](https://arxiv.org/abs/2004.00584) | Ditto, serialization and blocking for entity matching |
| [A Deep Dive Into Cross-Dataset Entity Matching with LLMs](https://openproceedings.org/2025/conf/edbt/paper-224.pdf) | Small fine tuned matchers against prompted large models |
| [State of AI Agent Memory 2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026) | Current benchmark landscape and competitor scores |

Figures quoted in this document come from the pages above. Benchmark numbers from vendor published pages should be treated as self reported.
