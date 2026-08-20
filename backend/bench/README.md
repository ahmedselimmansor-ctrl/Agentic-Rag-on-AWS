# Benchmarks

Three questions these answer, that unit tests cannot:

| Script | Question |
|---|---|
| `bench_retrieval.py` | At what corpus size does pgvector stop being fast, and is the HNSW index actually used? |
| `bench_stream.py` | How many simultaneous streams does a task sustain before time-to-first-token degrades? |
| `bench_ingest.py` | How long does a document take end to end, and where is the time spent? |

## Retrieval

Synthetic random unit vectors — this measures the **index**, not relevance, so
real embeddings would cost money without changing the result.

```bash
docker compose run --rm backend python -m bench.bench_retrieval --rows 100000 --queries 200 --explain
```

Sweep the HNSW search parameter to find the recall/latency knee:

```bash
docker compose run --rm backend python -m bench.bench_retrieval --rows 200000 --ef-search 40,100,200,400
```

Higher `ef_search` means better recall and slower queries. There is no correct
value — only the one that matches your latency budget.

Remove the synthetic rows when done:

```bash
docker compose run --rm backend python -m bench.bench_retrieval --cleanup
```

## Streaming

Needs a running API and a real account, because it exercises the whole path
including generation.

```bash
python -m bench.bench_stream --url http://localhost:8000 --email you@example.com --password '...' --concurrency 20 --requests 100
```

Read the two latency series separately:

- **rising TTFT** under concurrency — the server saturates before generation starts. Scale tasks.
- **flat TTFT, rising total** — the model is the bottleneck. Scaling tasks will not help.

## Ingestion

```bash
docker compose run --rm backend python -m bench.bench_ingest --file /path/to/doc.pdf --repeat 3
```

Reports per-stage timing. Embedding usually dominates; if parsing does, the
document is likely a scan going through OCR.
