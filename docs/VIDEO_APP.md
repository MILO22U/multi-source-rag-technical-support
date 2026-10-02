# 5-Minute Video — app-first run sheet (no terminal on camera)

The variant to record if you want the demos in the chat app, the supporting
evidence from files, and the architecture diagram from GitHub. The terminal
appears **once, before you press record**, to start the app.

Narration is identical to [`VIDEO.md`](VIDEO.md) — 628 words, ≈4:16 of speech.
Only what is on screen changes. Keep that file open on a second monitor or
phone to read from.

---

## Before you press record

**1. Start the app (the only terminal command).**

```powershell
cd "D:\Multi-Source RAG for Technical Support"
.\.venv\Scripts\python.exe scripts\app.py
```

Wait for `chunks indexed: {'docs': 23, 'forum': 20, 'blog': 11}  total=54`, then
**minimise the terminal window.** It never appears again.

**2. Click through all six starter chips once**, then press **New chat**. This
warms the trace log, so the sidebar's **Trace log** panel has records even if a
live query misbehaves. The empty chat is your opening shot.

**3. Open Chrome with these two tabs, in this order.** Pin them (right-click →
Pin tab) so they cannot be closed by accident, and switch between them with
`Ctrl+Tab`.

| Tab | URL |
|---|---|
| 1 — the app | `http://127.0.0.1:8000/` |
| 2 — GitHub | `https://github.com/MILO22U/multi-source-rag-technical-support` |

**4. Have these deep links ready** in tab 2 (paste into the address bar, or open
each once beforehand so autocomplete finds them). All verified to resolve:

```
.../multi-source-rag-technical-support#system-architecture
.../multi-source-rag-technical-support#ablation-study
.../multi-source-rag-technical-support#negative-result-1--hybrid-fusion-did-not-beat-bm25-alone
.../multi-source-rag-technical-support#negative-result-2--the-uniform-chunking-control-scored-higher-on-retrieval
.../multi-source-rag-technical-support#requirements-coverage
.../blob/main/data/conflicts_planted.json
.../blob/main/config/default.yaml#L114-L121
.../blob/main/src/zephyr_rag/types.py#L85-L96
.../tree/main/tests
```

GitHub's `#L85-L96` syntax highlights exactly those lines, so the thing you are
talking about is already outlined when the page loads — no scrolling on camera.

**5. Browser chrome.** `Ctrl+Shift+N` for a clean window with no bookmarks bar
or extensions, zoom to **110–125%** (`Ctrl` + `+`) so text is readable at 1080p,
and enable dark mode on GitHub (Settings → Appearance) so it matches the app.

**6. Recording.** Capture the whole display, 1080p/30. Three windows exist —
browser, VS Code (optional), minimised terminal — so a single-window capture
will go black when you switch.

---

## Run sheet

| Time | Window | On screen | Narration block (from `VIDEO.md`) |
|---|---|---|---|
| 0:00–0:20 | GitHub | repo home: title, description, topics, file tree | **Deck 1 — title** |
| 0:20–0:42 | GitHub | `data/conflicts_planted.json` — scroll the 12 entries, `id` → `topic` → `relationship` | **Deck 2 — relevance is not enough** |
| 0:42–1:16 | GitHub | `#system-architecture` — the pipeline diagram in the README | **Deck 3 — architecture** |
| 1:16–1:34 | GitHub | `config/default.yaml#L114-L121` — the intent × source weight matrix, pre-highlighted | **Deck 4 — intent weights** |
| 1:34–2:02 | **App** | `Ctrl+Tab`. Click chip 1 (*why do jobs retry forever… 429*). Point at the chips: `intent troubleshooting`, `forum 1.00`, `docs 0.65`, `sources used`. Then the amber `EMPIRICAL_OVERRIDE` banner. | **Demo 1 — weights, live** |
| 2:02–2:22 | App | New chat → chip 2 (*dead-lettered payload*). Point at the badge reading **`#20 → #1`**, then expand **Decision trace** for the was/move/CE/final table. | **Demo 2 — reranking, live** |
| 2:22–2:50 | GitHub | `#ablation-study` — rows 1–9, cursor on the row 5 → row 6 jump | **Deck 5 — ablation table** |
| 2:50–3:08 | GitHub | `src/zephyr_rag/types.py#L85-L96` — the 11 relationship classes | **Deck 6 — contradiction design** |
| 3:08–3:32 | **App** | New chat → chip 3 (*timeout=0*). The `MISCONCEPTION` banner, `rule authority_canonical`, and the two claims marked **upheld** / **rejected** | **Demo 3 — the misconception** |
| 3:32–3:54 | App | New chat → chip 4 (*default worker concurrency*). The green `NO CONTRADICTION — 3 pairs classified complementary`. Nothing flagged — that is the point. | **Demo 4 — the non-contradiction** |
| 3:54–4:06 | App | New chat → chip 6 (*Kubernetes CronJobs*). The red `refused — out of corpus` tag where citations would be. | **Demo 5 — refusal** |
| 4:06–4:22 | App | Sidebar → **🧾 Trace log**. Scroll one record: weights applied, sources used, conflicts with the rule fired, citations, timings. | **Demo 6 — the audit trail** |
| 4:22–4:48 | GitHub | `#negative-result-1…`, then `#negative-result-2…` | **Deck 8 — two results that went against me** |
| 4:48–5:00 | GitHub | `#requirements-coverage`, then `tests/` if you have a beat spare | **Deck 9 — close** |

Five window switches total, all `Ctrl+Tab` between two pinned tabs.

---

## Two upgrades, if you have the time

**Run the benchmark live instead of pointing at the table.** At 2:22, instead of
switching to GitHub, click the sidebar's **📊 Evaluate gold set** — it scores all
15 gold queries in-process in ~0.5 s and prints the metrics panel: nDCG 0.7178,
conflict detection 0.857, false positives 0.000, refusal accuracy 1.0. Live
numbers beat a screenshot of numbers, and it keeps you in one window. The
**🧪 Ablation study** button prints the full table the same way, in ~7 s — long
enough that you should only use it if you have something to say while it runs.

**Click a source badge once.** Any badge opens the full passage with its real
metadata — votes, author role, accepted flag, version. Worth five seconds during
demo 3: it shows the 47-vote accepted answer *and* the staff correction as
separate chunks, which is the concrete reason per-source chunking exists.

---

## If something breaks on camera

| Problem | Recovery, without leaving the browser |
|---|---|
| A query returns something unexpected | Sidebar → **Trace log** has the warm-up run's records. "Here's the recorded trace for that query." |
| The app is unreachable (`fetch failed`) | The terminal is minimised, not closed — restore it, confirm it is still serving, `Ctrl+Tab` back. Say nothing about it. |
| A GitHub page is slow | Keep talking; the narration for that beat does not depend on the page having painted yet. |
| You lose your place | Every beat starts with **New chat** in the app or a pinned-tab URL. Both are one click. |

## Why this version is the safer take

Nothing on screen requires typing. Every demo is a click on a pre-written chip,
so a typo cannot cost you a take, and the chips are in requirement order — 1, 2,
3, 4, 6 — which means clicking straight down the list *is* the demo sequence.
The only state you have to manage is pressing **New chat** between questions so
the previous answer is not still on screen.
