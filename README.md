# kilix-find

Search an explicit list of saved sources with BM25. The command is `kilix-find`.
It keeps exact excerpts, source identity, and a snapshot you can rebuild.
It does not crawl a disk, call a model, or install a Kilix verb.

Installable on demand from the Kilix content catalog as `kilix-find`
(`kilix install kilix-find`). Kilix's `kilix content` verb runs the installed
command; the catalog pins it to an exact commit of this repository.

## What it does

- Reads only the files you list in a `sources.json` file: plain UTF-8 text, or
  saved JSONL session rollouts.
- Takes a checked snapshot of each file and writes one SQLite FTS5 index. An
  index is never overwritten; a new snapshot is a new file.
- Ranks excerpts with BM25 and prints each one exactly as saved, with its
  source path, record ID, timestamp and, for rollouts, session and line.
- Lets you list the indexed sources, verify the index, and revalidate one
  excerpt against its original file.
- Uses only the Python standard library. Nothing leaves the machine.

## Quick start

Python 3.11 or newer, with the standard-library SQLite build that includes
FTS5. There are no package dependencies.

```sh
make test
./kilix-find index --config sources.json --output content.sqlite
./kilix-find --index content.sqlite "optical shutter"
./kilix-find --index content.sqlite sources
./kilix-find --index content.sqlite verify --sources
```

The last two lines do not work as written: a first argument that is not a
command makes the whole line a search, so the first searches for the word
`sources` and the second fails on `--sources`. Put the command first:
`./kilix-find sources --index content.sqlite` and
`./kilix-find verify --index content.sqlite --sources`.

`make all` byte-compiles the checkout and checks that the `kilix-find`
launcher is executable; it is the target the content catalog builds. `make
check` compiles and runs the suite, `make clean` removes bytecode.

## Usage

A first argument that is not a command is treated as a search query, so
`kilix-find "some words"` is `kilix-find search "some words"`.

| Command | What it does |
|---|---|
| `index --config FILE --output DB` | snapshot the listed sources and build a new index at `DB` |
| `index --snapshot FILE --output DB` | build an index from a verified v1 JSON snapshot |
| `search QUERY...` (default) | rank excerpts; `--json` for machine output, `--explain` for scores |
| `sources` | print the indexed sources, their coverage and scope as JSON |
| `verify [--sources]` | check the index; `--sources` also re-reads each original file |
| `revalidate ID` | check one excerpt against its original source |

Search options: `--limit` (default 5), `--per-source` (default 2), `--match
auto|any|all`, `--min-coverage` (default 0.4), and the filters `--source`,
`--group`, `--kind`, `--session`, `--since` and `--until`. `--backend lexical`
is a compatibility alias for `bm25`.

## Index, sources and matching

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

When `index` is given no `--config`, it reads `sources.json` from the same
directory as the default index. Paths under `.ssh` or `.gnupg`, and files named
like credential stores, are refused.

## How it works

```
sources.json --> snapshot each file --> records --> SQLite FTS5 index
                 (size, mtime and       (1000-char     (porter unicode61,
                  bytes checked twice)   excerpts)      read-only at search)
```

`index` resolves each listed path to the regular file it names, reads it, and checks
that its size and timestamps did not change while it was read. Text files are
split into overlapping excerpts of about 1000 characters; rollout files are
read line by line and only eligible message text is kept. Each excerpt records
its source path, SHA-256 of the source bytes, byte range and timestamp. The
index is built in a temporary file in the destination directory, checked with
SQLite's integrity check, and then linked into place, so a concurrent builder
can never be overwritten.

Search parses the query, drops common request and stop words, and asks FTS5
for BM25 candidates, then applies the match rule and the filters above.

## Documentation

- [`examples/sources.example.json`](examples/sources.example.json): a minimal
  source list.
- [`THIRD-PARTY.md`](THIRD-PARTY.md): the rollout record export taken from
  kilix-tui-utils, with its licence.

## Related repositories

- [kilix-content](https://github.com/itsmygithubacct/kilix-content#readme):
  the catalog and installer that pins and builds this repository.
- [kilix](https://github.com/itsmygithubacct/kilix#readme): provides
  `kilix install kilix-find` and the `kilix content` verb.
- [kilix-tui-utils](https://github.com/itsmygithubacct/kilix-tui-utils#readme):
  the source of `src/kilix_find/rollout.py`.

The Kilix launcher verb and the content catalog record are not part of this
repository.

## Part of Plebian-OS

kilix-find is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It is installed on demand from the Kilix content catalog, not shipped in the
installer image. The map of every repository and how it ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

MIT; see [`LICENSE`](LICENSE).
