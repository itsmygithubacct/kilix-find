# kilix-find

Search an explicit list of saved sources with BM25. The command is `kilix-find`.
It keeps exact excerpts, source identity, and a snapshot you can rebuild.
It does not crawl a disk, call a model, or install a Kilix verb.

Python 3.11 or newer, with the standard-library SQLite build that includes
FTS5. There are no package dependencies.

```sh
make test
./kilix-find index --config sources.json --output content.sqlite
./kilix-find --index content.sqlite "optical shutter"
./kilix-find --index content.sqlite sources
./kilix-find --index content.sqlite verify --sources
```

Without `--index`, search reads
`$GPU_TERMINAL_HOME/kilix-apps/kilix-find/content.sqlite`, or the same path
under `~/.local/gpu_terminal` when that variable is unset. `index` writes only
the path given by `--output` and refuses to overwrite it. Search opens the
database read-only.

`sources.json` uses schema `kilix.find.sources/v1`. Each entry needs `key`,
`path`, `kind`, `group`, `split`, and `title`. `kind` is `text` for a UTF-8
file, or `claude` or `codex` for one JSONL rollout. A rollout needs
`session_id` when the file does not carry one. See
`examples/sources.example.json`. At most 64 files, 16 MiB each, 64 MiB total.

Plain terms are OR candidates. The default `--match auto` keeps an excerpt
only when it covers at least 40% of the distinct query terms, counting terms
that are absent from the index. `--match any` and `--match all` are explicit.
Double-quoted phrases are mandatory. Scores are BM25 ranks, not probabilities.

The Kilix launcher verb and the content catalog record are not part of this
repository.
