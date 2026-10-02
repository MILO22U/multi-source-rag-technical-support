# 3-minute video — architecture only, simple words

One tab. One diagram. Seven things to say.

**Open this and nothing else:**

```
https://github.com/MILO22U/multi-source-rag-technical-support#system-architecture
```

That scrolls straight to the diagram. Zoom the browser to 125% so the boxes are
readable, and keep the diagram on screen for the whole video. You point at boxes
top to bottom, in order. No other tabs, no terminal, no app.

Total speech: 437 words, about 3:00 at a normal pace. Read it as written.

---

### [0:00 – 0:20] Top of the repo page, then scroll to the diagram

> "This is a question-answering system for customer support. The product is
> made up — a job queue called Zephyr. The information about it lives in three
> different places: the official documentation, a community forum, and the
> company's engineering blog. My job was to answer a customer's question using
> all three."

### [0:20 – 0:50] Point at the three boxes at the top of the diagram

> "First step. I cut each source into small pieces, because you need the
> paragraph that answers the question, not the whole page. But I cut each source
> differently. Documentation is cut by heading, because a section is what
> answers a question. A forum thread is cut into question-plus-one-answer, so
> two answers that disagree stay in separate pieces. Blog posts are cut into
> fixed overlapping windows, because an argument runs across headings. One shape
> would not fit all three."

### [0:50 – 1:15] Point at the "PER-SOURCE INDICES" box

> "Second step. Each source gets its own search index, instead of one shared
> one. The forum is chatty and long; the documentation is short and precise. In
> one shared index the forum would win on volume alone and drown the
> documentation out. Separate indexes let me decide how much each source
> counts."

### [1:15 – 1:45] Point at "QueryAnalyzer", then the "Weighting" box

> "Third step, and this is the main idea. Before searching, I look at what kind
> of question was asked. If someone asks what a default value is, the
> documentation is the authority and the forum is noise. If someone is debugging
> an error, it flips — the forum wins, because a real person hit that error at
> two in the morning and posted the fix. If the question is 'why is it built
> this way', the blog wins."

### [1:45 – 2:15] Point at the "Cross-encoder rerank" box

> "Fourth step: reranking. The first search is cheap and fast, so it is rough. I
> take about fifty candidates from it and run a slower, more careful scorer over
> just those fifty. This gave me the single biggest quality improvement in the
> project — on one question the correct answer came out ranked twentieth, and
> after reranking it was first."

### [2:15 – 2:45] Point at the "Conflict detection" and "Precedence cascade" boxes

> "Fifth step: the sources disagree. Sometimes both are right — the default is
> ten for one kind of worker and the CPU count for another. Sometimes one is
> simply wrong: a forum answer with forty-seven votes says you can switch a
> timeout off by setting it to zero, and you cannot. So I classify what kind of
> disagreement it is, then apply rules in a fixed order. I never average two
> numbers, and I never hide the disagreement."

### [2:45 – 3:00] Point at the bottom line of the diagram

> "Last step. Every answer writes a record of how it was produced: which sources
> were used, how the ranking changed, and which rule settled the disagreement.
> So that is the system — three sources, cut differently, weighted by the kind
> of question, reranked, and checked for contradictions."

---

## If he asks one follow-up

**"Does it ever refuse?"** "Yes. If the words in the question don't appear
anywhere in those three sources, it says it doesn't know instead of guessing."

**"How fast is it?"** "About 27 milliseconds per question, and it runs offline —
no API key, no model download."

**"What's the weakest part?"** "The reranker. It's a scoring function I designed
by hand rather than a trained model, so on a couple of questions it puts the
right answer second instead of first. Swapping in a trained reranker is a
one-line change, and it's written up in the README."
