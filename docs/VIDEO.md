# 5-Minute Video Presentation — production run sheet

Everything needed to record the deliverable video in one take: a slide deck, a
keypress-driven demo driver, word-for-word narration timed to fit 5:00, and a
pre-flight checklist. Every number spoken below was produced by the commands
shown, on this machine, from the committed corpus.

| Asset | Path | What it is |
|---|---|---|
| Slide deck | `docs/slides/deck.html` | 9 slides, self-contained HTML (no network, no build step). Arrow keys or click to advance · `N` speaker notes · `R` reset timer · `F` fullscreen. A 5:00 presenter clock sits top-right and turns red when you run over. |
| Demo driver | `scripts/demo.ps1` | 6 live demos. Prints the real command, waits for Enter, runs it, waits again. `-Auto` runs without pauses for rehearsal; `-From N` starts mid-list. |
| Chat app | `scripts/app.py` | Browser chat UI over the same pipeline — an alternative to the terminal demos; see the variant section below. |
| Log viewer | `scripts/show_log.py` | Human-readable view of one `logs/queries.jsonl` record, so requirement 6 is legible on screen instead of a wrapped blob. `--grep <text>` selects by query. |

**Shape of the video:** slides carry the argument, the terminal carries the
evidence, three window switches total. Narration is **628 words ≈ 4:16 of
speech**, leaving ~40 s for transitions and reading beats. Target 4:55.

---

## Pre-flight (10 minutes, once)

1. **Clean build.**
   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q               # expect 52 passed
   .\.venv\Scripts\python.exe scripts\build_index.py      # expect docs 23, forum 20, blog 11 = 54
   ```
2. **Warm the log** so every trace you might need already exists as a fallback:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Auto
   ```
3. **Terminal.** Windows Terminal, maximised, single pane, **18–20 pt** Cascadia
   Mono, dark theme, ~120 columns. `cls` before each demo block.
4. **Deck.** Open `docs/slides/deck.html` in Chrome or Edge, press `F`. Toggle
   `N` once to check the notes render, then **turn them off** — they are for a
   second monitor or a rehearsal pass, not for the recording.
5. **Capture.** OBS Studio, or `Win`+`Alt`+`R`. 1080p / 30 fps, capture the
   **whole display** (you are switching windows, so a window capture will go
   black). One microphone, test ten seconds, listen back before the real take.
6. **Rehearse once** with `-Auto` and the deck's own timer. The pipeline runs in
   ~30 ms per query, so nothing on screen makes you wait — the only thing that
   runs long is you.

---

## Run sheet

Timings are cumulative; `w` is the word count of that narration block.
**Bold** marks what the cursor should be pointing at.

### 0:00 – 0:20 · Deck 1 — title · 46 w

> "Hi, I'm Mrinal. This is a multi-source RAG system for technical support. It
> answers customer questions about a fictional job-queue product, Zephyr, from
> three sources: product documentation, a community forum, and an engineering
> blog. It runs offline, and every number I quote reproduces from the README."

### 0:20 – 0:42 · Deck 2 — relevance is not enough · 51 w

> "Standard RAG asks one question: is this passage relevant? Support needs two
> more. Is it *trustworthy* — an accepted forum answer with forty-seven votes
> can be flatly wrong. And does it *agree* with the other sources? So I planted
> twelve specific contradictions in this corpus and made resolving them a
> first-class requirement."

### 0:42 – 1:16 · Deck 3 — architecture · 89 w

Walk the diagram top to bottom, once, no backtracking.

> "Three sources, three chunkers — a docs page, a forum thread and a blog post
> have different natural answer units. Forum chunks keep the question plus
> *one* answer, so competing answers stay in separate chunks. Each source gets
> its own BM25 and dense index; one global index lets a verbose forum swamp
> terse docs on volume alone. Fusion is rank-based RRF, weighted by intent,
> because BM25 and cosine scales aren't comparable. Then cheap recall into an
> expensive cross-encoder, with a floor of two per source. Twenty-seven
> milliseconds end to end."

### 1:16 – 1:34 · Deck 4 — intent-conditioned weights · 45 w

> "Requirement three: source reliability isn't constant. For API reference,
> docs are authoritative and the forum is mostly noise. For troubleshooting it
> inverts — the forum outranks the docs, because someone already hit that error
> at 2am and posted the fix. For conceptual questions, the blog wins."

**Switch to the terminal** and start the driver:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
```

### 1:34 – 2:02 · Demo 1 — weights, live · 51 w

> "Troubleshooting question: why do jobs retry forever on a 429? Classified
> **troubleshooting**, and the weights applied are **forum 1.0, docs 0.65** —
> exactly that inversion. Four forum and four docs chunks in the final eight,
> and an **empirical override** fires: independent users report behaviour the
> docs deny. It resolves to *disclose both*."

### 2:02 – 2:22 · Demo 2 — reranking, live · 36 w

> "Requirement four. Different question — reading a dead-lettered job's
> payload. Watch the rank columns. The correct answer, a staff post about an
> undocumented CLI flag, **was number twenty** after first-stage retrieval.
> Reranking moved it to **number one**."

**Switch back to the deck.**

### 2:22 – 2:50 · Deck 5 — ablation table · 51 w

> "Not one lucky query. Adding the cross-encoder — row five to row six — is
> **plus 0.167 nDCG**, thirty percent relative, the largest jump in the table,
> for twenty-two milliseconds. It also lifts conflict detection from 0.29 to
> 0.71. Row seven trades four thousandths of nDCG for source recall 1.0 and
> detection 0.857."

### 2:50 – 3:08 · Deck 6 — contradiction design · 38 w

> "Requirement five took most of the design effort. Claims are extracted as
> typed facts, pairs classified into eleven relationships, then a precedence
> cascade: version scoping, deprecation, empirical override, authority,
> recency — or unresolved, disclosing both. Silent averaging is forbidden."

**Switch to the terminal.**

### 3:08 – 3:32 · Demo 3 — the misconception · 43 w

> "The interesting case. *Can I set timeout equals zero to disable the
> timeout?* The **accepted** answer, forty-seven votes, says yes — and it's
> wrong. Classified **misconception**, resolved by authority, and the answer
> **names and corrects** the belief rather than quietly stating the right
> value."

### 3:32 – 3:54 · Demo 4 — the non-contradiction · 41 w

> "But most apparent contradictions aren't. Default worker concurrency: docs
> say ten, another page says CPU count. Both true — different components.
> Classified **complementary**, **no warning emitted**. My false-positive rate
> is zero, and that's what makes the eighty-six percent detection rate mean
> anything."

### 3:54 – 4:06 · Demo 5 — refusal · 21 w

> "Out of corpus — Kubernetes CronJobs. It refuses, and says why: zero percent
> term coverage. Fifteen out of fifteen on scope decisions."

### 4:06 – 4:22 · Demo 6 — the audit trail · 33 w

> "Requirement six. One JSON record per query: intent, weights applied,
> candidate pool, per-chunk scores, **rank before and after**, which sources
> filled the final slots, every conflict with the rule that fired, citations,
> timings."

**Switch back to the deck.**

### 4:22 – 4:48 · Deck 8 — two results that went against me · 60 w

> "Two results went against me. Hybrid fusion didn't beat BM25 alone —
> synthetic passages reuse the question's vocabulary, which is BM25's best
> case. And uniform chunking scored *higher* on nDCG, partly a chunk-size
> confound, but it lost **a third of its conflict detection**. So per-source
> chunking is justified by requirement five, not by nDCG. With fourteen queries
> my interval spans 0.19."

### 4:48 – 5:00 · Deck 9 — coverage and close · 23 w

> "All six requirements covered, fifteen example queries with full traces,
> fifty-two tests passing, every number reproducible from one build command.
> Thanks for watching."

---

## Variant: record the demos in the chat app instead of the terminal

`python scripts/app.py` serves the same pipeline as a chat UI, and it shows every
beat the terminal demos show — with less reading on screen. The narration above
needs **no changes**; only what you point at changes.

| Run sheet beat | In the app |
|---|---|
| Demo 1 — weights | The chips under the answer: `intent troubleshooting`, `forum 1.00`, `docs 0.65`, `sources used docs 4 / forum 4`. |
| Demo 2 — reranking | The source badge itself reads `#20 → #1`. Open **Decision trace** for the was/move/CE/final table. |
| Demo 3 — misconception | Amber banner: `MISCONCEPTION`, `rule authority_canonical`, with the two claims marked **upheld** and **rejected**. |
| Demo 4 — non-contradiction | Green banner: `NO CONTRADICTION — 3 pairs classified complementary`. Nothing is flagged, which is the point. |
| Demo 5 — refusal | Red `refused — out of corpus` tag in place of citations. |
| Demo 6 — the log | Sidebar → **Trace log**, which renders the last `queries.jsonl` records. |

Two extra affordances worth 5 seconds each if you have them: clicking a badge
opens the full passage with its votes/role/version metadata, and the sidebar
**Evaluate gold set** button runs the 15-query benchmark in-process and prints
the metrics table — the same numbers as deck slide 5, produced live.

**Trade-off, decide before recording.** The app is the better-looking take and
the easier one to narrate. The terminal take proves there is no web layer hiding
the pipeline, and it cannot break on a rendering bug mid-recording. Pick one and
rehearse that one; mixing both costs window switches you do not have time for.
If you record the app, launch it *before* hitting record — startup prints build
output you do not want on camera.

---

## Numbers spoken, and where each comes from

| Claim in narration | Source | Verify with |
|---|---|---|
| 13 docs / 17 threads / 8 posts → 54 chunks | corpus | `python scripts/build_index.py` |
| 12 planted conflicts | `data/conflicts_planted.json` | — |
| troubleshooting → forum 1.00, docs 0.65 | `config/default.yaml` | demo 1 trace |
| #20 → #1 on the DLQ query | live | demo 2 trace (`top1_changed=True`) |
| +0.167 nDCG from reranking (0.556 → 0.722) | ablation rows 5 → 6 | `python scripts/ablations.py` |
| conflict detection 0.857 · false positives 0.000 | eval | `python scripts/evaluate.py` |
| refusal accuracy 15/15 | eval | `python scripts/evaluate.py` |
| p50 27 ms | eval | `python scripts/evaluate.py` |
| nDCG@10 95% CI spans 0.19 (n=14) | eval | `python scripts/evaluate.py` |
| 52 tests pass | tests | `python -m pytest -q` |

---

## If you run long

Cut in this order — each cut is self-contained and costs no requirement
coverage:

1. **Demo 5, the refusal** (−12 s). The metric is on deck slide 6's footer.
2. **The second half of the architecture walk** (−12 s). Say "cheap recall into
   an expensive cross-encoder, with a per-source floor" and move on.
3. **The confidence-interval sentence on deck 8** (−8 s). It is in the README.

Do **not** cut demo 4, the non-contradiction. The zero false-positive rate is
what makes the detection rate meaningful, and it is the thing most submissions
will not have.

## If a live demo fails on camera

Every trace is already in `logs/queries.jsonl` from the pre-flight run. Say
"here's the recorded trace for that query", run
`python scripts/show_log.py --grep "<a few words of the query>"`, and keep
going. Do not restart the take.

## Export and delivery

Record at 1080p/30, export H.264 MP4, and keep it under ~150 MB so it attaches
or uploads anywhere. Upload to Google Drive or YouTube (unlisted) and send the
link alongside the repository — a link cannot bounce off an attachment size
limit, and reviewers can scrub it.
