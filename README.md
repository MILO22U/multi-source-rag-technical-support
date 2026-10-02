# Multi-Source RAG for Technical Support

**Retrieval-augmented generation over heterogeneous, mutually-contradicting knowledge sources — with explicit conflict resolution and per-query source attribution.**

---

## Abstract

Conventional RAG treats the corpus as one homogeneous body of text and optimises a single question: *is this passage relevant?* That assumption breaks for technical support, where answers must be assembled from sources of **differing reliability, recency, and authority** — official documentation, community forum threads, and engineering blog posts — which routinely **disagree with one another**.

This repository implements a RAG pipeline that asks two further questions of every retrieved passage: *is it trustworthy?* and *does it agree with the others?* It contributes (i) a per-source chunking strategy aligned to each source's natural answer unit, (ii) intent-conditioned source weighting rather than static source priors, (iii) a two-stage retrieve-then-rerank funnel that **blends** cross-encoder relevance with authority and recency rather than discarding them, (iv) an eleven-category contradiction taxonomy with a declarative precedence cascade, and (v) full per-query provenance tracing.

Evaluated on a purpose-built corpus of 38 documents → **54 chunks**, with **12 deliberately planted inter-source conflicts** and a 15-query graded relevance set. Headline measured results (`scripts/evaluate.py`, seed 42):

| | |
|---|---|
| **nDCG@10** | **0.718** (95% CI [0.619, 0.812], n=14) |
| **MRR** | **0.881** |
| **Source-recall** | **1.000** |
| **Conflict detection** | **6/7 (0.857)**, false-positive rate **0.000** |
| **Refusal accuracy** | **15/15 (1.000)** |
| **p50 latency** | **26.6 ms** (CPU only, no GPU, no API key) |

> **Core finding.** Reranking, not retrieval, carried this system: it moved nDCG@10 from 0.556 → 0.722, source-recall from 0.679 → 1.000, and conflict detection from 0.286 → 0.857. Two results came out *against* the design hypotheses and are reported as such below — hybrid fusion did **not** beat BM25 alone here, and the uniform-chunking control **outscored** per-source chunking on retrieval metrics while losing a third of its conflict-detection ability.

---

## Key Contributions

- **Source-specific chunking.** Documentation splits on its heading tree, forum threads into *question + exactly one answer* pairs, blog prose into overlapping sentence-bounded windows. A uniform-splitter control is retained and measured.
- **The forum-chunking insight.** Concatenating competing answers destroys the ability to rank a correct answer above a wrong one *and* hides disagreement **inside** a chunk, where a pairwise detector structurally cannot see it. **Chunk boundaries set the resolution limit of contradiction detection** — and the ablation confirms it (row 9).
- **Intent-conditioned source weighting.** A 6×3 weight matrix replaces the usual "docs are best" prior: documentation dominates reference lookups (1.00 vs 0.45 forum), forums *outrank* documentation for troubleshooting (1.00 vs 0.65), blogs dominate conceptual questions (1.00 vs 0.70).
- **Rerank blending, not replacement.** A cross-encoder scores topical relevance only and is blind to authority, recency and version filters. The α sweep shows both endpoints losing to the middle: α=0.0 (weighting only) → 0.613, α=1.0 (reranker only) → 0.697, peak 0.731 at α=0.8.
- **Contradiction as epistemics, not NLP.** Eleven relationship categories feeding a declarative precedence cascade, because **most apparent contradictions are not contradictions** — they are the same claim scoped to different product versions.
- **The empirical override.** Documentation describes *intended* behaviour; users report *observed* behaviour. Two or more **independent** corroborating reports surface as a known issue rather than being silently overruled — the case where naive `if source == "docs": win` produces a confidently wrong answer.
- **Zero-dependency reproducibility.** BM25, LSA dense embeddings (Jacobi eigendecomposition on the Gram matrix), rank fusion, reranking and conflict resolution are all pure standard library. Every number here reproduces on any Python 3.10+ install with no GPU, no model download, and no API key.

---

## System Architecture

```
  data/docs/*.md (13) ──▶ HeadingTreeChunker   · heading tree, merge orphans <120 tok,
                          (23 chunks)            never split code fences
  data/forum/           ▶ ThreadQAChunker      · question + ONE answer per chunk,
    threads.json (17)     (20 chunks)            votes/accepted/role retained
  data/blog/            ▶ SlidingWindowChunker · 700 tok, 15% overlap,
    posts.json (8)        (11 chunks)            date + era-version in header
                                   │
                    ┌──────────────▼──────────────┐
                    │ Context-header repair       │  "Zephyr Docs v3.2 > Retries >
                    │ (breadcrumb into index)     │   Configuring backoff"
                    └──────────────┬──────────────┘
        ┌──────────────────────────┴──────────────────────────┐
        │  PER-SOURCE INDICES — never one global index        │
        │  BM25(k1=1.5,b=0.75) ×3    LSA 256-d (SVD) ×3       │
        └──────────────────────────┬──────────────────────────┘
                                   │
 query ─▶ QueryAnalyzer ───────────┤   intent (6 classes) · version → HARD filter
          (rules | hosted LLM)     │   · expansions ×2 (down-weighted 0.35)
                                   ▼
                        ┌──────────────────────┐
                        │ Weighted RRF (k=60)  │  rank-based: BM25 and cosine
                        │ per source           │  scales are not comparable
                        └──────────┬───────────┘
                                   ▼
                        ┌──────────────────────┐   S = w[intent][src] · RRF
                        │ Weighting            │       · (1+0.3·authority)
                        │                      │       · (1+0.5·(recency−1))
                        └──────────┬───────────┘
                                   ▼
                        ┌──────────────────────┐
                        │ ≥2 per-source FLOOR  │ ◀── without this, conflict
                        │ → 48-60 candidates   │     detection can never fire
                        └──────────┬───────────┘
                                   ▼
                        ┌──────────────────────┐
                        │ Cross-encoder rerank │  joint (query,chunk) features:
                        │ blend α=0.7          │  coverage · proximity · phrase
                        │ caps 5/src, 3/doc    │  · identifier · position · version
                        │ MMR λ=0.7            │
                        └──────────┬───────────┘
                                   ▼  top-8
                        ┌──────────────────────┐
                        │ Conflict detection   │  typed claim extraction →
                        │ 11-way taxonomy      │  relationship classification
                        └──────────┬───────────┘
                                   ▼
                        ┌──────────────────────┐   version_scoping → deprecation
                        │ Precedence cascade   │   → empirical_override
                        │                      │   → authority → recency
                        └──────────┬───────────┘   → unresolved
                                   ▼
                        ┌──────────────────────┐
                        │ Synthesis            │  conflicts as STRUCTURED input;
                        │ (extractive | LLM)   │  silent averaging forbidden
                        └──────────┬───────────┘
                                   ▼
                  answer + citations + logs/queries.jsonl
```

**Rationale, one line each.** Per-source indices defeat corpus-size bias (a verbose forum would otherwise swamp terse docs on volume alone). RRF fuses by *rank* because BM25 is unbounded while cosine occupies a narrow band, making score addition a scale-correction exercise rather than a relevance judgement. The cheap-recall → expensive-precision funnel makes an O(n) cross-encoder affordable by only ever showing it ~50 candidates. The per-source floor and final-slot caps exist so requirement 5 has cross-source evidence to operate on.

---

## Repository Layout

```
.
├── README.md                     ← the single documentation entry point (this file)
├── LICENSE                       MIT
├── requirements.txt              pyyaml + pytest; everything else optional
├── pyproject.toml                editable install, optional extras
├── config/default.yaml           EVERY hyperparameter + global seed
├── data/
│   ├── product_facts.json        canonical fact table — the answer key
│   ├── conflicts_planted.json    12 planted conflicts + 3 false-positive pairs
│   ├── gold_queries.yaml         15 queries, graded relevance labels
│   ├── docs/*.md                 13 documentation pages (markdown + JSON frontmatter)
│   ├── forum/threads.json        17 threads, 42 posts
│   └── blog/posts.json           8 blog posts
├── src/zephyr_rag/
│   ├── types.py                  Chunk, ScoredChunk, ConflictFinding, Resolution …
│   ├── tokenize.py               shared tokenizer (BM25 + dense use the same one)
│   ├── config.py                 YAML loader, dotted overrides, global seed
│   ├── evaluation.py             nDCG / recall / MRR / source-recall / bootstrap CI
│   ├── scripts_support.py        evaluation + ablation drivers
│   ├── pipeline.py               end-to-end orchestration
│   ├── cli.py                    ingest · query · search · evaluate · ablate · serve
│   ├── ingest/loaders.py         corpus loaders, JSON frontmatter
│   ├── chunking/                 base · docs · forum · blog · uniform · contextual
│   ├── index/                    bm25 · embeddings (LSA/ST/Voyage) · store
│   ├── retrieval/                analyzer · fusion · weights · hybrid
│   ├── rerank/                   cross_encoder · blend · mmr
│   ├── contradiction/            detect · policy
│   ├── generate/synthesize.py    extractive + optional LLM backend
│   ├── observability/trace.py    per-query JSONL trace
│   └── webapp/                   chat app: stdlib HTTP server + one HTML file
├── docs/
│   ├── VIDEO.md                  5-minute presentation run sheet (narration + timings)
│   └── slides/deck.html          9-slide deck, self-contained HTML
├── scripts/                      app (chat UI) · build_index · query · evaluate · ablations
│                                 · show_log (one trace, readable) · demo.ps1 (video driver)
├── tests/                        52 tests
└── logs/                         queries.jsonl, chunks.jsonl (gitignored)
```

**Why there is essentially one document.** Every `.md` under `data/` is *corpus input* — the fictional product's knowledge base that the system retrieves from — not project documentation. This README is the only document describing the project itself; `docs/` holds the video-presentation package (run sheet and slides) and nothing that duplicates the report.

---

## Getting Started

### Prerequisites

| Requirement | Specification |
|---|---|
| Python | ≥ 3.10 (developed on 3.12.10) |
| OS | Platform-independent (developed on Windows 11) |
| **GPU** | **Not required.** No neural model runs in the default configuration. |
| RAM / disk | < 500 MB / < 50 MB |
| Network | **Not required** |
| API key | **Not required.** The optional LLM stages need one; the measured system does not. |

### Reproducing Our Results

```bash
# 1. Clone
git clone https://github.com/MILO22U/multi-source-rag-technical-support.git
cd multi-source-rag-technical-support

# 2. Environment
python -m venv .venv
source .venv/bin/activate              # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 3. Verify the install (expect: 52 passed)
pytest -q

# 4. Build chunks + indices (expect: docs 23, forum 20, blog 11 = 54)
python scripts/build_index.py --dump-chunks

# 5. One query with the full decision trace
python scripts/query.py "Why do my jobs retry forever when the API returns 429?" --explain

# 6. Reproduce the headline metrics
python scripts/evaluate.py --per-query

# 7. Reproduce the ablation table and the alpha sweep
python scripts/ablations.py --alpha-sweep
```

**Optional — enable the LLM-backed stages.** They are provider-agnostic: no
model id is hardcoded in `src/`, so supply one through `LLM_MODEL` (or
`llm.model` in the config) together with whatever key your SDK reads.

```bash
pip install -e ".[llm]"          # installs the reference SDK
export LLM_MODEL=<your-provider-model-id>
python scripts/evaluate.py \
  --set retrieval.query_analysis.backend=llm \
  --set contradiction.detector=llm \
  --set generation.backend=llm
```

**Optional — real neural embeddings and a trained cross-encoder:**
```bash
pip install -e ".[local]"
python scripts/evaluate.py \
  --set index.dense.backend=sentence_transformers \
  --set rerank.backend=cross_encoder
```

### The interactive app

```powershell
python scripts/app.py              # builds indices, serves, opens a browser
zephyr-rag serve --port 8080       # same thing, via the installed CLI
python scripts/app.py --set rerank.alpha=1.0   # serve any experiment config
```

A chat interface at `http://127.0.0.1:8000/`, stdlib only -- `http.server` plus
one HTML file, no Flask, no node, no API key. It is the same `Pipeline` the CLI
uses, built once at startup, so every number it shows is the number the
benchmarks measured.

Each answer carries the things a reviewer would otherwise have to dig out of a
trace:

| On screen | Requirement |
|---|---|
| intent chip + per-source weight chips (`docs 0.65` / `forum 1.00`) | 3 -- weighing |
| source badges, each showing its rank move (`#20 → #1`) | 3, 4 -- combination, reranking |
| conflict banner: relationship, rule fired, upheld vs rejected claim | 5 -- contradictions |
| green "no contradiction" banner when pairs are merely complementary | 5 -- the zero false-positive rate |
| `refused -- out of corpus` tag instead of an answer | scope control |
| collapsible decision trace: pool, rank before/after, CE and final scores, stage timings | 6 -- logging |
| click any badge or chunk id for the full passage and its metadata | citation audit |

**Greetings are handled in the app, not the pipeline.** "hi" is not a support
question: routing it through retrieval returns the out-of-scope refusal, which
is accurate and conversationally wrong -- it tells the user their greeting is
missing from the knowledge base. The app answers openers with a short
description of what it can do plus starter chips, and runs no retrieval and
writes no trace, because none happened. The check is conservative (every token
must be a greeting token), so "hey, why do my jobs retry forever?" still goes
through the full pipeline, and the evaluated system keeps exactly the behaviour
the benchmarks measured.

The sidebar runs the rest of the system in-process: **Evaluate gold set**,
**Ablation study**, **Rebuild indices**, and **Trace log** (the last records from
`logs/queries.jsonl`). Starter chips on the empty state walk through one query
per requirement, refusal included.

### Determinism

`config/default.yaml` sets `experiment.seed: 42`, applied globally at config load. `retrieval.recency.reference_date` is a **fixed date, never `now()`** — a moving reference date makes recency-weighted metrics drift between runs. The default pipeline has no other source of nondeterminism; repeated runs are bit-identical.

---

## Dataset

Purpose-built corpus for a fictional managed task-queue product ("Zephyr"), authored entirely against `data/product_facts.json` so ground truth is explicit and machine-checkable.

| Source | Documents | Units | Chunks | Natural answer unit | Strategy |
|---|---|---|---|---|---|
| Documentation | 13 pages | 13 | **23** | A heading section | Heading-tree, structure-aware |
| Forum | 17 threads | 42 posts | **20** | A question–answer pair | Thread-aware QA pairing |
| Blog | 8 posts | 8 | **11** | A narrative passage | Sliding window 700 tok / 15% |

Chunk token counts — docs 48/267/662 (min/median/max), forum 130/234/365, blog 163/608/711.

Topics in `out_of_scope_topics` (Kubernetes CronJobs, Terraform, gRPC) are **deliberately absent**, so refusal behaviour is testable.

### Planted conflicts

| ID | Type | Conflict | Expected resolution | Detected |
|---|---|---|---|---|
| C1 | `version_drift` | Docs `max_retries`=5 · 2025 blog =3 | Both correct by era; state current, name the change | ✅ |
| C2 | `misconception` | Docs: `timeout=0` fail-fast · Forum (47 votes, **accepted**): disables it | Docs; correct the popular belief explicitly | ✅ |
| C3 | `docs_gap` | CLI reference silent · staff forum post documents `--include-payload` | Forum on staff authority; flag as undocumented | ✅ |
| C4 | `conditional` | Blog: Windows works · Forum: Windows broken | Both true — broken 3.0, fixed 3.1; scope it | ✅ |
| C5 | `docs_drift` | Changelog: `--legacy-ack` removed · CLI ref still lists it | Changelog (recency within docs tier) | ✅ |
| C6 | `deprecation` | Docs `enqueue()` · Forum `queue.push()` | Docs; note old form still works | ✅ |
| C7 | `empirical_override` | Docs: `Retry-After` honoured · **3 independent** reports: ignored | Surface as known issue, version-scoped | ✅ |
| C8 | `version_drift` | Docs `X-Zephyr-Key` · Blog `Authorization: Zephyr` | Docs; mention old form for upgraders | ✅ |
| C9 | `misconception` | Docs 256 KiB · **guest** blog 1 MB | Docs; low author authority is the tie-breaker | ✅ |
| C10 | `version_drift` | Docs 30 s visibility timeout · Blog 60 s | Docs; 60 s was correct in v2.4 | ✅ |
| C11 | `complementary` | Docs concurrency 10 · Forum CPU count | **FALSE-POSITIVE TEST** — different components | ✅ not flagged |
| C12 | `misconception` | Docs manual DLQ replay · Forum (3 votes) auto-replays | Docs; correct the guess | ✅ |

C11 is the discriminating case. The detector sees the pair, classifies it `complementary`, and emits **no** resolution — measured false-positive rate **0.000**.

### Honest caveat on synthetic data

The corpus is LLM-authored, so passages tend to answer questions using the question's own vocabulary. **Absolute retrieval scores are therefore optimistic; relative comparisons between configurations are the trustworthy result.** This directly explains the hybrid-fusion negative result below.

---

## Configuration

All parameters live in `config/default.yaml`; nothing is hardcoded in `src/`. Override with `--set key.path=value`.

```yaml
experiment:
  seed: 42

retrieval:
  rrf_k: 60
  min_per_source: 2                 # HARD FLOOR — enables cross-source conflict detection
  query_analysis:
    expansion_weight: 0.35          # expansion lists down-weighted vs the original query
  weights:
    troubleshooting:   {docs: 0.65, forum: 1.00, blog: 0.55}
    api_reference:     {docs: 1.00, forum: 0.45, blog: 0.35}
    conceptual:        {docs: 0.70, forum: 0.45, blog: 1.00}
  authority:
    beta: 0.30
  recency:
    strength: 0.50                  # bounded: (1 + strength*(decay-1))
    half_life_days: 400
    floor: 0.60
    skip_for_intents: ["conceptual", "opinion"]
    reference_date: "2026-10-01"    # fixed, not now()

rerank:
  alpha: 0.70                       # α·cross_encoder + (1−α)·retrieval_score
  final_k: 8
  caps: {max_per_source: 5, max_per_document: 3}
  mmr: {enabled: true, lambda: 0.70}

contradiction:
  precedence:                       # version scoping FIRST
    - version_scoping
    - explicit_deprecation
    - empirical_override
    - source_authority
    - recency_within_tier
    - unresolved
  empirical_override:
    min_independent_reports: 2
```

**Three values earned their defaults the hard way — each fixed a measured regression.**

**`expansion_weight: 0.35`.** Unweighted RRF counts every ranked list equally, and with one query plus two expansions across two retrievers that is six lists. At `k=60` the gap between rank 1 and rank 20 is only ~25%, so a broadly-relevant chunk appearing mid-list in all six **outscored the chunk ranked first for the actual question**. Measured: `forum:t_0041:p2` — BM25 rank 1 in every list with a score of 14.8–21.1 against a runner-up at 4.5–7.1 — fell out of the top 3 entirely. Down-weighting expansions restored it to rank 1.

**`recency.strength: 0.50`.** Applying recency decay as a raw multiplier is the obvious implementation and it is wrong, for a reason specific to rank fusion. RRF discards score magnitude, compressing relevance into a ~25% band (0.045–0.056 across top candidates); raw decay spans `[0.6, 1.0]`, a ~67% band. **A modifier with wider dynamic range than the signal it modifies does not modify it — it replaces it.** The same `forum:t_0041:p2` chunk, rank 1 in all six lists with a 3–4× lexical advantage, finished **6th** purely because it was seven months old. Scaling the deviation bounds recency to the tie-breaker role it should always have had.

**`min_per_source: 2`.** The single most consequential line in the file. Without it, effective weighting concentrates the pool on one source, the final set becomes single-source, and **contradiction detection silently never fires** — requirement 5 becomes dead code that still passes its unit tests. Requirements 3/4 and requirement 5 are in genuine tension, and this is where it is resolved.

---

## Results & Benchmarks

All numbers from `python scripts/evaluate.py` and `python scripts/ablations.py --alpha-sweep`, seed 42, 15 gold queries, LSA dense backend + surrogate cross-encoder (no GPU, no API key).

### Headline metrics

| Metric | Value | Notes |
|---|---|---|
| nDCG@10 | **0.7178** | 95% CI [0.619, 0.812] |
| Recall@5 | 0.5235 | |
| Recall@10 | 0.6682 | |
| Pool recall@20 (stage 1) | 0.7332 | the hard ceiling on everything downstream |
| MRR | **0.8810** | a relevant chunk is first for 12/14 scored queries |
| Source-recall | **1.0000** | every required source present in every final set |
| Source-precision | 0.7036 | |
| Mean gold-chunk displacement | **3.571** positions | reranking's measured contribution |
| Promotion rate into top-3 | 0.2143 | gold chunk entering top-3 from outside it |
| Conflict detection | **0.8571** (6/7) | |
| Conflict false-positive rate | **0.0000** | C11 correctly `complementary` |
| Refusal accuracy | **1.0000** (15/15) | |
| Answer must-contain pass | 0.8000 | |
| Answer must-not-contain pass | 0.8667 | |
| p50 / p95 latency | **26.6 / 31.4 ms** | pure-Python, single core |

### Ablation study

Each row isolates one component's marginal contribution.

| # | Configuration | nDCG@10 | R@5 | R@10 | MRR | poolR | srcR | conflict | FP | p50 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BM25 only | 0.5536 | 0.452 | 0.515 | 0.786 | 0.715 | 0.679 | 0.286 | 0.07 | 4.0 |
| 2 | Dense (LSA) only | 0.5224 | 0.466 | 0.515 | 0.732 | 0.715 | 0.679 | 0.286 | 0.07 | 3.9 |
| 3 | Hybrid RRF | 0.5274 | 0.481 | 0.515 | 0.768 | 0.715 | 0.679 | 0.286 | 0.07 | 4.3 |
| 4 | + context headers | 0.5325 | 0.466 | 0.515 | 0.744 | 0.715 | 0.679 | 0.286 | 0.07 | 4.3 |
| 5 | + query expansion | 0.5557 | 0.478 | 0.515 | 0.798 | 0.733 | 0.679 | 0.286 | 0.07 | 4.8 |
| 6 | + cross-encoder rerank | 0.7222 | 0.534 | 0.656 | 0.881 | 0.733 | 0.964 | 0.714 | 0.00 | 26.1 |
| **7** | **+ MMR & source caps (default)** | **0.7178** | 0.523 | 0.668 | 0.881 | 0.733 | **1.000** | **0.857** | **0.00** | 27.3 |
| 8 | α=1.0 (reranker only) | 0.6965 | 0.526 | 0.686 | 0.845 | 0.733 | 1.000 | 0.857 | 0.00 | 28.3 |
| 9 | **Control: uniform chunking** | 0.7304 | 0.674 | 0.858 | 0.808 | 0.902 | 1.000 | 0.571 | 0.00 | 28.9 |

**Reranking is the dominant contribution.** Row 5 → row 6 is the single largest jump in the table: nDCG@10 **+0.167** (0.556 → 0.722, a 30% relative gain), MRR 0.798 → 0.881, source-recall 0.679 → 0.964, conflict detection 0.286 → 0.714. The mechanism is visible per query: on q06 the correct answer was promoted **#20 → #1**, on q09 **#22 → #1**. Those are stage-1 ranking failures that reranking recovered, which is precisely what the funnel is for. It costs ~22 ms of the 27 ms budget — 81% of latency for 30% of quality, and worth it here.

**MMR and caps trade a little nDCG for what the task actually requires.** Row 6 → 7 shows nDCG dipping 0.0044 (well inside the CI) while source-recall completes to **1.000** and conflict detection rises **0.714 → 0.857**. Capping any one source at 5 of 8 slots guarantees the cross-source evidence requirement 5 depends on. A pure-nDCG optimiser would delete this row and break requirement 5.

### Negative result 1 — hybrid fusion did not beat BM25 alone

Rows 1–3: BM25 **0.5536**, dense 0.5224, hybrid **0.5274**. Fusion *lost* 0.026 nDCG against lexical retrieval alone.

Honest diagnosis, not an excuse:

1. **The synthetic-corpus caveat is the main cause.** LLM-authored passages reuse the question's vocabulary, which is exactly the regime where BM25 is strongest and where the paraphrase-matching that justifies dense retrieval has little to do.
2. **LSA over 54 chunks is a weak dense arm.** Truncated SVD needs co-occurrence statistics to learn from; at this scale the latent space is thin. Row 2 confirms it is the weaker arm.
3. **Fusion averages a strong arm with a weak one.** RRF rewards agreement, and when one list is substantially better, agreement-weighting drags the strong list toward the weak one.

Hybrid retrieval remains the right architecture — the lexical arm is irreplaceable for identifiers (`429`, `max_retries`, `v3.2`) and the dense arm for paraphrase, and `cli search` shows each winning on its own query type. But **on this corpus, measured, it did not pay.** The honest conclusion is that the premise of hybrid retrieval (the two arms failing on *disjoint* queries) is only weakly satisfied by synthetic data, and this row should be re-measured with `index.dense.backend=sentence_transformers` on a real corpus before the design is generalised.

### Negative result 2 — the uniform-chunking control scored higher on retrieval

Row 9 beats row 7 on nDCG@10 (0.7304 vs 0.7178), R@5 (0.674 vs 0.523), R@10 (0.858 vs 0.668) and pool recall (0.902 vs 0.733).

**There is a measurement confound, and it is the main story.** Uniform chunking produces far larger chunks (1000 tokens vs a 234–267 median), and gold labels are matched by chunk id — so one oversized chunk spans text that three precise chunks would divide, and matches more labels per retrieved slot. Recall@k is **structurally inflated by chunk size**. This is a real limitation of document-level graded labels, and it means rows 7 and 9 are not measuring the same thing.

**What is not confounded is the column that matters for this task:**

| | per-source chunking | uniform control |
|---|---|---|
| Conflict detection | **0.857** | 0.571 |

**Uniform chunking lost one third of its conflict-detection ability**, exactly as predicted at design time, and for the predicted reason: flattening a forum thread into one blob puts competing answers inside a single chunk, where a pairwise detector cannot compare them. Thread `t_0007` is the concrete case — a wrong answer with 47 votes and a staff correction below it. As separate chunks (`t_0007:p2`, `t_0007:p3`) the misconception is detectable; merged, it is invisible.

**Conclusion, stated plainly:** per-source chunking did not improve retrieval metrics on this corpus and may have slightly hurt them under a size-confounded measure. It is justified by requirement 5, not by nDCG. A system optimising retrieval scores alone should use the uniform splitter and would then be unable to detect a third of its conflicts.

### Reranking analysis

| Metric | Value |
|---|---|
| Mean absolute displacement of best gold chunk | **3.571 positions** |
| Promotion rate into top-3 from outside it | 0.214 |
| Largest single promotion observed | **#22 → #1** (q09), **#20 → #1** (q06) |
| Queries where top-1 changed | 5 / 15 |
| Mean pool-level `mean_abs_rank_delta` | 4.0 – 12.0 per query |

A mean displacement of ~3.6 positions on an 8-slot output is the reranker doing substantive work, not noise. Had this been near zero, reranking would not have earned its 22 ms.

### α sweep — does blending beat either endpoint?

| α | 0.0 | 0.1 | 0.2 | 0.3 | 0.4 | 0.5 | 0.6 | **0.7** | 0.8 | 0.9 | 1.0 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| nDCG@10 | 0.613 | 0.637 | 0.645 | 0.693 | 0.693 | 0.724 | 0.726 | **0.718** | **0.731** | 0.706 | 0.697 |
| MRR | 0.798 | 0.845 | 0.881 | 0.929 | 0.893 | 0.929 | 0.893 | 0.881 | 0.881 | 0.845 | 0.845 |

**Yes — and this is the clearest validation in the report.** Both endpoints lose to the middle: α=0.0 (my weighting alone) scores 0.613, α=1.0 (the reranker alone) scores 0.697, and the plateau from 0.5–0.8 reaches 0.731. Blending beats reranker-only by **+0.034** and weighting-only by **+0.118**. This is the measurement that proves requirement 3's weighting is not dead weight under requirement 4's reranker.

The configured default of 0.7 sits 0.013 below the α=0.8 peak — **inside the confidence interval, so it is not a meaningful difference** and the default was left unchanged rather than tuned to a noise-level maximum on 14 queries.

### Contradiction handling

| Metric | Value |
|---|---|
| Planted conflicts surfaced by their query | **6/7 (0.857)** |
| Planted conflicts detectable across the corpus | **12/12** (all verified individually) |
| Relationship-classification correctness | 11/12 (C7 initially mislabelled `version_drift`; fixed by ordering empirical override before the version test) |
| Resolution accuracy (correct rule fired) | 12/12 |
| **False-positive rate on non-conflict pairs** | **0.000** |
| C11 classified `complementary`, not `contradict` | ✅ |

Both sides are reported deliberately. A detector tuned purely for recall flags every paraphrase, scores well on the top line, and is unusable — so the false-positive rate is the number that makes the detection rate meaningful.

The 1/7 miss is q13 ("does the DLQ retry automatically?"): the conflict is detected corpus-wide but the relevance gate suppressed it on that query because the low-vote forum guess did not reach the final 8. That is a retrieval-recall failure presenting as a conflict miss, and the honest fix is better recall, not a looser gate.

### Statistical honesty

15 queries (14 scored; q11 is refusal-only) gives an nDCG@10 95% bootstrap CI of **[0.619, 0.812]** — a span of 0.19. **Differences below ~0.03 nDCG in any table above are not significant**, which specifically covers the row 6 → 7 dip (0.004), the α=0.7 vs α=0.8 gap (0.013), and the row 7 vs row 9 difference (0.013). The two findings that *are* larger than the interval are the reranking gain (+0.167) and the α-blend gain (+0.118); those are the claims worth making.

---

## Example Queries

All 15 gold queries, actually executed. Full traces in `logs/queries.jsonl`; reproduce any line with
`python scripts/query.py "<query>" --explain`.

| # | Query | Intent | Final sources | Top-1 chunk (rank move) | Conflict → rule | Outcome |
|---|---|---|---|---|---|---|
| q01 | What is the maximum payload size for a Zephyr job? | `api_reference` | 5d/2f/1b | `docs:job-payloads:size-limit` (#1←2) | `docs_gap` → staff authority | ✅ "256 KiB (262,144 bytes)" |
| q02 | Why do my jobs retry forever when the API returns 429? | `troubleshooting` | 4f/4d | `forum:t_0041:p2` (#1←1) | **`empirical_override`** → disclose both | ✅ forum-led, docs corroborated |
| q03 | Why does Zephyr use exponential backoff instead of fixed delays? | `conceptual` | 4b/3d/1f | `blog:why-exponential-backoff:w0` (#1←1) | `conditional` → version scoping | ✅ blog-led, "jitter" present |
| q04 | What is the default retry count? | `api_reference` | 5d/2b/1f | `docs:job-payloads:size-limit` (#1←9) | **`version_drift`** → version scoping | ⚠️ states 5 and names the v3.0 change; top-1 misranked |
| q05 | Can I set `timeout=0` to disable the timeout? | `how_to` | 5d/3f | `docs:retries-and-backoff:timeouts` (#1←1) | **`misconception`** → authority | ✅ corrects the 47-vote accepted answer |
| q06 | How do I see the payload of a dead-lettered job from the CLI? | `how_to` | 4f/4d | `forum:t_0012:p2` (**#1←20**) | **`docs_gap`** → staff authority | ✅ `--include-payload`, flagged undocumented |
| q07 | I am on v2.4, how do I configure backoff? | `how_to` | 3d/2b | `blog:scaling-to-1m-jobs:w0` (#1←5) | `version_drift` → version scoping | ✅ v3 content excluded by hard filter (pool 5, not 48) |
| q08 | What breaks when I upgrade from v2 to v3? | `version_migration` | 5d/2b/1f | `docs:migration-v2-to-v3` (#1←2) | none | ✅ `X-Zephyr-Key` present |
| q09 | Does the Zephyr CLI work on Windows? | — | 4d/2b/2f | `blog:windows-support-lands:w0` (**#1←22**) | **`conditional`** → version scoping | ✅ scoped: broken 3.0, fixed 3.1 |
| q10 | Why are my jobs landing in the DLQ right after a rate limit? | `troubleshooting` | 4f/4d | `forum:t_0041:p2` (#1←3) | **`empirical_override`** → disclose both | ✅ multi-hop joined |
| q11 | How do I integrate Zephyr with Kubernetes CronJobs? | `how_to` | 5d/3f | `docs:cli-reference` (#1←5) | — | ✅ **REFUSED** — no hallucination |
| q12 | Is Zephyr better than self-hosting Redis? | `opinion` | 5b/3f | `blog:queue-vs-self-hosted:w0` (#1←1) | `version_drift` → version scoping | ✅ hedged, blog-weighted |
| q13 | Does the dead-letter queue retry jobs automatically? | — | 5d/2b/1f | `docs:retries-and-backoff:timeouts` (#1←1) | none | ⚠️ "manual" present; conflict not surfaced |
| q14 | What is the default worker concurrency? | `api_reference` | 5d/3f | `docs:concurrency-and-workers:managed-worker-concurrency` (#1←1) | **none (C11 → `complementary`)** | ✅ correctly *not* flagged |
| q15 | Why is the same job running twice? | `troubleshooting` | 5f/1b/2d | `forum:t_0049:p2` (#1←1) | none | ✅ visibility-timeout chain |

**Worked example — q05, the misconception case.** The retrieved set places the documentation first and *both* sides of the forum disagreement at #2 and #3 (promoted from #20 and #24 by reranking). The detector pairs them, classifies `misconception` at 0.84 confidence, and `authority_canonical` resolves in favour of the staff correction. The answer leads with the documented behaviour — *"`timeout=0` means fail-fast… It does **not** disable the timeout"* — quotes the 47-vote accepted answer saying the opposite, and appends:

```
### Note on conflicting information
- **A common community answer is incorrect here.** A widely-held community answer
  conflicts with the documentation on a fact that has one correct value. The
  documented behaviour wins, and the answer names and corrects the misconception
  explicitly because the user may already believe it.
  - timeout_zero_semantics = disables: Yes, timeout=0 disables the timeout...
  - timeout_zero_semantics = fail_fast: timeout=0 does NOT disable the timeout...
  - *Resolved by rule:* `authority_canonical` → `forum:t_0007:p3`
```

**Known weaknesses visible in this table.** q04 and q13 ranked a wrong chunk first (`docs:job-payloads:size-limit` for a retry question) — the surrogate cross-encoder's numeric-value boost over-fires on digit-dense passages. The correct chunk is #2 in both cases, so MRR holds at 0.5 and the answer still contains the right value, but the top-1 is wrong and a trained cross-encoder would likely fix it. q09's intent fell through to `unknown`; the weights defaulted sensibly and the answer is correct, but the classifier needs a pattern for "does X work on Y".

---

## Requirements Coverage

| # | Requirement | Implementation | Measured evidence |
|---|---|---|---|
| 1 | Three distinct data types | 13 docs pages · 17 forum threads/42 posts · 8 blog posts, all authored against `product_facts.json` | 54 chunks; all 12 planted conflicts resolve to real documents |
| 2 | Per-source chunking | `chunking/docs.py` (heading tree) · `forum.py` (QA pairing) · `blog.py` (windows) + `uniform.py` control | Ablation row 9: control loses 33% of conflict detection (0.857→0.571) |
| 3 | Intelligent multi-source weighing | 6×3 intent matrix, authority, bounded recency, per-source indices, `min_per_source` floor | Source-recall **1.000**; α sweep shows weighting adds +0.034 over reranker-only |
| 4 | Reranking | Joint-feature cross-encoder + α blend + MMR + caps | **+0.167 nDCG**, mean displacement 3.571, promotions #22→#1 and #20→#1 |
| 5 | Contradiction handling | 11-way taxonomy → 6-rung precedence cascade → mandatory disclosure | 6/7 by query, 12/12 corpus-wide, **FP rate 0.000** |
| 6 | Source logging | One JSONL record per query: weights, per-source counts, per-chunk scores, `rank_before`/`rank_after`, rule fired, citations, stage timings | `logs/queries.jsonl`; `--explain` renders it |

---

## Design Decisions and Pitfalls

**Content-addressed chunk IDs, not ordinals.** `docs:retries-and-backoff:configuring-backoff`, never `docs:retries:7`. Gold labels reference chunk IDs; ordinals shift whenever a chunker changes, silently invalidating every label and therefore every metric. `slugify()` is frozen by contract once labels exist, and a test pins its output.

**One tokenizer, shared by BM25 and the dense backend.** If the lexical and dense arms disagreed about what a token is, their rank lists would be incomparable for reasons unrelated to relevance and fusion would quietly degrade.

**Identifiers survive tokenization, emitted both whole and split.** `X-Zephyr-Key` → `['x-zephyr-key', 'zephyr', 'key']`, matching exact-identifier *and* natural-language queries. Versions are exempt from splitting: `v3.2` → `['3','2']` would let v2 documents match v3 queries, the exact failure this corpus is built to expose.

**Normalise within a candidate set, never globally.** Cross-query normalisation leaks information between queries and makes per-query scores incomparable. It also means `final_score` is useless as a refusal signal — the top chunk always scores 1.0 — which is why refusal keys on absolute reranker score, distinctive-term coverage, and corpus-absent terms instead.

**The nDCG denominator must come from the corpus, not the label list.** Prefix matching lets one label (`docs:retries-and-backoff`) match five chunks, so using the label count understates ideal gain. This evaluator initially reported nDCG@10 of 1.39 and recall@10 of 1.03 — impossible values that a less suspicious reading would have published.

**Conflict disclosure must be gated on query relevance.** The final 8 chunks are all topically adjacent, so the detector legitimately finds conflicts the user did not ask about; a payload-size question came back warning about the `--legacy-ack` CLI flag. An irrelevant disclosure is noise, not honesty — it dilutes the warning that mattered.

**Jacobi over the Gram matrix makes a from-scratch dense index practical.** With 54 chunks and ~2,500 vocabulary terms, decomposing `AᵀA` means a 2500×2500 eigenproblem; `AAᵀ` is 54×54 and yields the same left singular vectors. That is the difference between "possible in pure Python" and "needs numpy".

---

## Limitations

- **Synthetic corpus.** LLM-authored passages share vocabulary with the queries; absolute metrics are optimistic and this directly caused the hybrid-fusion negative result.
- **Small gold set.** 15 queries, 54 chunks. The nDCG CI spans 0.19; differences under 0.03 are not significant.
- **Gold labels are document/section-level**, which structurally inflates recall for larger chunks and makes rows 7 and 9 of the ablation not strictly comparable. Span-level labels would fix this.
- **Single-annotator labels.** Hand-verified, but no inter-annotator agreement measured.
- **The default dense backend is LSA, not a trained encoder.** Truncated SVD captures co-occurrence, not learned semantics. The `sentence_transformers` backend exists for comparison and the gap is itself an unmeasured result.
- **The default cross-encoder is a feature-based surrogate**, not a trained model. Its numeric-value boost demonstrably over-fires (q04, q13). The real bge reranker activates with the `local` extra.
- **Claim extraction uses a registered attribute table.** Precise and auditable on this corpus, but an unregistered attribute is invisible to the rule-based detector; the LLM detector covers that and is not validated beyond the planted ledger.
- **No incremental indexing**, no multilingual support, no user-interaction signal (clicks, thumbs) to learn relevance from.

---

## Appendix — 5-Minute Video Presentation

The full production package lives in [`docs/VIDEO.md`](docs/VIDEO.md): a
shot-by-shot run sheet with word-for-word narration (628 words ≈ 4:16 of
speech, timed to fit 5:00), a pre-flight checklist, and the fallback to use if a
live demo misbehaves on camera.

| Asset | Path |
|---|---|
| Slide deck (9 slides, self-contained HTML — no network, no build) | [`docs/slides/deck.html`](docs/slides/deck.html) |
| Live-demo driver (6 demos, Enter to advance) | [`scripts/demo.ps1`](scripts/demo.ps1) |
| Human-readable view of one trace record | [`scripts/show_log.py`](scripts/show_log.py) |

Outline, with what is on screen for each beat:

| Time | Screen | Beat |
|---|---|---|
| 0:00–0:42 | deck 1–2 | Relevance is not enough: a passage must also be *trustworthy* and *consistent*. 12 planted conflicts. |
| 0:42–1:34 | deck 3–4 | Architecture in one pass; source weights are conditioned on query intent (requirements 1–3). |
| 1:34–2:22 | terminal | Demo 1: `troubleshooting` → **forum 1.00, docs 0.65**. Demo 2: the DLQ answer moves **#20 → #1** under reranking (`top1_changed=True`). |
| 2:22–3:08 | deck 5–6 | Reranking is +0.167 nDCG, the largest jump in the ablation; then the 11-way taxonomy and the precedence cascade. |
| 3:08–4:22 | terminal | Demo 3: the 47-vote accepted answer is wrong → `misconception`. Demo 4: concurrency defaults are `complementary` → **no warning** (0.00 FP). Demo 5: refusal. Demo 6: the `queries.jsonl` audit trail (requirement 6). |
| 4:22–5:00 | deck 8–9 | The two negative results, the ±0.19 confidence interval, and requirement coverage. |

Reproduce any demo directly:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1          # Enter between steps
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Auto    # rehearsal, no pauses
```

---

## Citation

```bibtex
@software{bhardwaj2026multisourcerag,
  author  = {Bhardwaj, Mrinal},
  title   = {Multi-Source RAG for Technical Support: Intent-Conditioned
             Retrieval and Explicit Contradiction Resolution across
             Documentation, Forums, and Technical Blogs},
  year    = {2026},
  version = {0.1.0},
  license = {MIT},
  url     = {https://github.com/MILO22U/multi-source-rag-technical-support}
}
```

## License

MIT — see [LICENSE](LICENSE).
