#!/usr/bin/env python3
"""Search an explicit list of saved sources with BM25. CPU only, no model."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time

from kilix_find import sources as source

def state_dir():
    configured = os.environ.get("GPU_TERMINAL_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "gpu_terminal"
    return base / "kilix-apps" / "kilix-find"


def default_index():
    return state_dir() / "content.sqlite"


SCHEMA = "kilix.find.bm25/v1"
TOKENIZER = "porter unicode61 remove_diacritics 2"
STOP = set("a an and are as at be by can did do does for from had has have how i in is it me my of on or our please show tell that the their there these this to was we were what when where which who why with would find about locate give".split())


def normalize_text(text):
    # Split code identifiers without changing the original displayed excerpt.
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ")


def quote(term):
    return '"' + term.replace('"', '""') + '"'


class Tokenizer:
    """Use exactly the index's tokenizer, including its English stemmer."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(f"CREATE VIRTUAL TABLE text USING fts5(value, tokenize='{TOKENIZER}')")
        self.db.execute("CREATE VIRTUAL TABLE words USING fts5vocab(text, instance)")

    def tokens(self, text):
        self.db.execute("DELETE FROM text")
        self.db.execute("INSERT INTO text VALUES (?)", (normalize_text(text),))
        return [r[0] for r in self.db.execute("SELECT term FROM words ORDER BY offset")]

    def close(self):
        self.db.close()


def parse_query(query, tokenizer):
    if not query.strip() or len(query) > 2000:
        raise ValueError("query must contain 1..2000 characters")
    if query.count('"') % 2:
        raise ValueError('unclosed quoted phrase')
    phrases = []
    terms = []
    raw_words = []
    for n, part in enumerate(query.split('"')):
        if n % 2:
            words = tokenizer.tokens(part)
            if not words:
                raise ValueError("quoted phrase has no searchable terms")
            phrases.append(normalize_text(part))
            terms.extend(words)
        else:
            words = re.findall(r"[^\W_]+", normalize_text(part).casefold())
            words = [w for w in words if w not in STOP]
            raw_words.extend(words)
            terms.extend(tokenizer.tokens(" ".join(words)))
    terms = sorted(set(terms))
    if len(terms) > 32:
        raise ValueError("query has more than 32 distinct searchable terms")
    return terms, list(dict.fromkeys(phrases)), list(dict.fromkeys(raw_words))


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except ValueError:
        return None


def date_bound(value, until=False):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        if until:
            parsed += timedelta(days=1)
        return parsed.timestamp()
    result = timestamp(value)
    if result is None:
        raise ValueError("date filter requires YYYY-MM-DD or a timezone-aware ISO timestamp")
    return result


def build(snapshot, output):
    records = source.verify_index(snapshot)
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("index already exists; build a new snapshot path")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bm25-build-", dir=output.parent)
    os.close(fd)
    db = None
    try:
        db = sqlite3.connect(temporary)
        db.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents (
                rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                source_key TEXT NOT NULL, source_id TEXT NOT NULL,
                group_key TEXT NOT NULL, kind TEXT NOT NULL, session_id TEXT,
                timestamp REAL, payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL
            );
            CREATE INDEX source_filter ON documents(source_key);
            CREATE INDEX group_filter ON documents(group_key);
        """)
        db.execute(f"CREATE VIRTUAL TABLE search USING fts5(title, body, tokenize='{TOKENIZER}')")
        db.execute("CREATE VIRTUAL TABLE vocabulary USING fts5vocab(search, instance)")
        db.execute("CREATE VIRTUAL TABLE terms USING fts5vocab(search, row)")
        header = {k: v for k, v in snapshot.items() if k != "records"}
        for k, value in {"schema": SCHEMA, "snapshot": header, "records": len(records),
                         "sqlite_version": sqlite3.sqlite_version, "tokenizer": TOKENIZER}.items():
            db.execute("INSERT INTO metadata VALUES (?,?)", (k, json.dumps(value)))
        for n, record in enumerate(records, 1):
            payload = source.canonical(record).decode()
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (n, record["id"], record["key"], record["source_id"], record["group"],
                        record["kind"], record["session_id"], timestamp(record["timestamp"]),
                        payload, source.digest(payload.encode())))
            db.execute("INSERT INTO search(rowid,title,body) VALUES (?,?,?)",
                       (n, normalize_text(record["title"]), normalize_text(record["text"])))
        db.execute("INSERT INTO search(search) VALUES ('optimize')")
        db.execute("INSERT INTO search(search) VALUES ('integrity-check')")
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite index integrity check failed")
        db.close()
        db = None
        # Same-filesystem atomic publication; never overwrite a concurrent builder.
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.link(temporary, output)
        return {"index": str(output), "records": len(records), "bytes": output.stat().st_size,
                "snapshot_sha256": snapshot["index_sha256"],
                "complete": snapshot["complete_within_explicit_scope"]}
    finally:
        if db is not None:
            db.close()
        Path(temporary).unlink(missing_ok=True)


class Index:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("PRAGMA cache_size=-4096")
        self.db.execute("PRAGMA temp_store=MEMORY")
        try:
            self.metadata = {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT * FROM metadata")}
            if self.metadata.get("schema") != SCHEMA or self.metadata.get("tokenizer") != TOKENIZER:
                raise ValueError("unsupported BM25 index; rebuild with this application")
        except Exception:
            self.db.close()
            raise
        self.tokenizer = Tokenizer()

    def __enter__(self):
        return self

    def close(self):
        self.tokenizer.close()
        self.db.close()

    def __exit__(self, *args):
        self.close()

    @property
    def snapshot(self):
        return self.metadata["snapshot"]

    @staticmethod
    def decode(row):
        if source.digest(row["payload"].encode()) != row["payload_sha256"]:
            raise ValueError("stored excerpt digest mismatch; rebuild the index")
        record = json.loads(row["payload"])
        if record["id"] != row["id"]:
            raise ValueError("stored excerpt identity mismatch")
        return record

    def verify(self, live=False):
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite index integrity check failed")
        records = []
        for row in self.db.execute("SELECT d.*,s.title,s.body FROM documents d JOIN search s ON s.rowid=d.rowid ORDER BY d.rowid"):
            record = self.decode(row)
            if row["title"] != normalize_text(record["title"]) or row["body"] != normalize_text(record["text"]):
                raise ValueError("search text differs from the saved excerpt")
            expected = (record["key"],record["source_id"],record["group"],record["kind"],record["session_id"],timestamp(record["timestamp"]))
            actual = tuple(row[k] for k in ("source_key","source_id","group_key","kind","session_id","timestamp"))
            if actual != expected:
                raise ValueError("source filter metadata mismatch")
            records.append(record)
            if live:
                source.revalidate(record)
        if len(records) != self.metadata["records"] or len(records) != self.db.execute("SELECT count(*) FROM search").fetchone()[0]:
            raise ValueError("index population mismatch")
        source.verify_index({**self.snapshot, "records": records})
        return {"ok": True, "records": len(records), "sources_revalidated": live,
                "snapshot_sha256": self.snapshot["index_sha256"]}

    def search(self, query, limit=5, match="auto", min_coverage=0.4, per_source=2,
               sources=(), groups=(), kinds=(), session=None, since=None, until=None):
        began = time.perf_counter()
        if type(limit) is not int or not 1 <= limit <= 20 or type(per_source) is not int or not 1 <= per_source <= 20:
            raise ValueError("limit and per-source must be integers from 1 to 20")
        if match not in {"auto", "any", "all"} or not math.isfinite(min_coverage) or not 0 <= min_coverage <= 1:
            raise ValueError("invalid match mode or minimum coverage")
        terms, phrases, raw_words = parse_query(query, self.tokenizer)
        needed = len(terms) if match == "all" else (1 if match == "any" else max(1, math.ceil(min_coverage * len(terms))))
        result = {"schema": "kilix.find.result/v1", "backend": "bm25", "query": query,
                  "index_sha256": self.snapshot["index_sha256"], "matches": [], "warnings": [],
                  "coverage": self.snapshot["coverage"],
                  "query_rules": {"terms": terms, "required_phrases": phrases, "match": match,
                                  "required_terms": needed if terms else 0, "term_count": len(terms),
                                  "min_coverage": min_coverage, "per_source": per_source,
                                  "score_meaning": "BM25 ranking score; not a probability of relevance"},
                  "filters": {"sources": list(sources), "groups": list(groups), "kinds": list(kinds),
                              "session": session, "since": since, "until": until},
                  "freshness": "saved snapshot; revalidate before relying on a current source",
                  "empty_meaning": "no lexical matches meeting these query/filter rules in this snapshot; semantic matches may be missed"}
        if not self.snapshot["complete_within_explicit_scope"]:
            result["warnings"].append("Some configured sources were unavailable or partially excluded.")
        if not terms:
            result["warnings"].append("Query has no searchable terms after removing request/stop words.")
            result.update(wall_seconds=time.perf_counter()-began, examined_excerpts=0,
                          suppressed_duplicates=0, suppressed_by_source_limit=0)
            return result
        marks = ",".join("?" for _ in terms)
        present = {r[0] for r in self.db.execute(f"SELECT term FROM terms WHERE term IN ({marks})", terms)}
        result["query_rules"]["terms_absent_from_index"] = sorted(set(terms) - present)
        # MATCH receives original words: applying Porter twice is not generally idempotent.
        expression = "(" + " OR ".join(quote(t) for t in raw_words + phrases) + ")"
        for phrase in phrases:
            expression += " AND " + quote(phrase)
        clauses = ["search MATCH ?"]
        parameters = [expression]
        descriptors = self.snapshot["sources"]
        for values, field, allowed in ((sources, "source_key", {r["key"] for r in descriptors}),
                                       (groups, "group_key", {r["group"] for r in descriptors}),
                                       (kinds, "kind", {"text", "claude", "codex"})):
            if any(v not in allowed for v in values):
                raise ValueError(f"unknown {field} filter: {', '.join(v for v in values if v not in allowed)}")
            if values:
                clauses.append(f"d.{field} IN ({','.join('?' for _ in values)})")
                parameters.extend(values)
        if session:
            clauses.append("d.session_id = ?")
            parameters.append(session)
        lower = date_bound(since) if since else None
        upper = date_bound(until, until=True) if until else None
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("since must precede until")
        for value, operator in ((lower, ">="), (upper, "<")):
            if value is not None:
                clauses.append(f"d.timestamp {operator} ?")
                parameters.append(value)
        if lower is not None or upper is not None:
            result["warnings"].append("Date filters use saved timestamps; text notes use filesystem_mtime, not authored time. Undated records are excluded.")
        # Compute coverage once. Joining a vocabulary CTE to FTS can make SQLite
        # rescan that CTE for every candidate (quadratic for common terms).
        hits = {row["doc"]: (row["n"], row["terms"]) for row in self.db.execute(f"""
            SELECT doc, count(DISTINCT term) AS n, group_concat(DISTINCT term) AS terms
            FROM vocabulary WHERE term IN ({marks}) GROUP BY doc
        """, terms)}
        sql = f"""
            SELECT d.rowid, d.id, -bm25(search, 2.0, 1.0) AS score
            FROM search JOIN documents d ON d.rowid=search.rowid
            WHERE {' AND '.join(clauses)} ORDER BY bm25(search, 2.0, 1.0), d.id
        """
        # Rank IDs, then load only eligible payloads; do not sort whole excerpts.
        seen_text = set()
        selected = []
        counts = Counter()
        scanned = duplicates = source_limited = 0
        for row in self.db.execute(sql, parameters):
            hit_count, matched = hits.get(row["rowid"], (0, ""))
            if hit_count < needed:
                continue
            scanned += 1
            record = self.decode(self.db.execute("SELECT * FROM documents WHERE rowid=?", (row["rowid"],)).fetchone())
            text_key = (record["source_id"], source.digest(" ".join(record["text"].split()).encode()))
            overlaps = any(record["record_id"] == old["record_id"] and
                           max(0, min(record["end"], old["end"])-max(record["start"], old["start"])) >=
                           0.5 * min(record["end"]-record["start"], old["end"]-old["start"])
                           for old in selected)
            if text_key in seen_text or overlaps:
                duplicates += 1
                continue
            if counts[record["source_id"]] >= per_source:
                source_limited += 1
                continue
            seen_text.add(text_key)
            counts[record["source_id"]] += 1
            selected.append(record)
            result["matches"].append({**record, "excerpt": record["text"], "retrieval_score": row["score"],
                                      "matched_terms": sorted(matched.split(",")),
                                      "query_coverage": hit_count / len(terms)})
            if len(selected) == limit:
                break
        result.update(examined_excerpts=scanned, suppressed_duplicates=duplicates,
                      suppressed_by_source_limit=source_limited,
                      wall_seconds=time.perf_counter()-began)
        return result


def human(result, explain=False):
    print(f"bm25 — {len(result['matches'])} results")
    for n, row in enumerate(result["matches"], 1):
        print(source.safe_text(f"\n{n}. {row['title']} [{row['evidence_class']}]\n{row['source_path']}\n{row['excerpt']}"))
        print(source.safe_text(f"record {row['id']}; {row['timestamp']} ({row['timestamp_basis']}); saved snapshot"))
        if row["session_id"]:
            print(source.safe_text(f"session {row['session_id']}; line {row['line']}"))
        if explain:
            print(source.safe_text(f"BM25 {row['retrieval_score']:.6g}; query coverage {row['query_coverage']:.0%}; matched: {', '.join(row['matched_terms'])}"))
    if not result["matches"]:
        print("No lexical matches meeting these query/filter rules in the saved snapshot.")
        if result["query_rules"]["match"] == "auto":
            print("Use --match any to broaden keyword matching.")
    if explain:
        print(source.safe_text("Query rules: " + json.dumps(result["query_rules"], ensure_ascii=False)))
    for warning in result["warnings"]:
        print(source.safe_text("Notice: " + warning), file=sys.stderr)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"index", "search", "sources", "revalidate", "verify", "-h", "--help"}
    if argv and argv[0] not in commands:
        argv.insert(0, "search")
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("index", help="create a new persistent snapshot; never overwrite")
    config = p.add_mutually_exclusive_group()
    config.add_argument("--config", default=None)
    config.add_argument("--snapshot", help="import a verified v1 JSON snapshot")
    p.add_argument("--output", required=True)
    for name in ("search", "sources", "revalidate", "verify"):
        p = subs.add_parser(name)
        p.add_argument("--index", default=str(default_index()))
        p.add_argument("--json", action="store_true")
        if name == "search":
            p.add_argument("--backend", choices=("bm25", "lexical"), default="bm25", help="lexical is a compatibility alias")
            p.add_argument("--limit", type=int, default=5)
            p.add_argument("--per-source", type=int, default=2)
            p.add_argument("--match", choices=("auto", "any", "all"), default="auto")
            p.add_argument("--min-coverage", type=float, default=0.4)
            p.add_argument("--source", action="append", default=[])
            p.add_argument("--group", action="append", default=[])
            p.add_argument("--kind", choices=("text", "claude", "codex"), action="append", default=[])
            p.add_argument("--session")
            p.add_argument("--since")
            p.add_argument("--until")
            p.add_argument("--explain", action="store_true")
            p.add_argument("query", nargs="+")
        elif name == "verify":
            p.add_argument("--sources", action="store_true", help="also revalidate each original source generation")
        elif name == "revalidate":
            p.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.command == "index":
            if args.snapshot:
                snapshot = source.read_json(args.snapshot)
            else:
                config_path = Path(args.config) if args.config else state_dir() / "sources.json"
                if not config_path.is_file():
                    raise ValueError("pass --config sources.json, or --snapshot index.json")
                snapshot = source.index_sources(source.read_json(config_path))
            result = build(snapshot, args.output)
            print(json.dumps(result))
            return 0 if result["complete"] else 1
        with Index(args.index) as index:
            if args.command == "search":
                result = index.search(" ".join(args.query), limit=args.limit, match=args.match,
                                      min_coverage=args.min_coverage, per_source=args.per_source,
                                      sources=args.source, groups=args.group, kinds=args.kind,
                                      session=args.session, since=args.since, until=args.until)
                if args.json:
                    print(json.dumps(result, ensure_ascii=True, indent=2))
                else:
                    human(result, explain=args.explain)
            else:
                if args.command == "sources":
                    result = {"sources": index.snapshot["sources"], "coverage": index.snapshot["coverage"], "scope": index.snapshot["scope"]}
                elif args.command == "verify":
                    result = index.verify(live=args.sources)
                else:
                    row = index.db.execute("SELECT * FROM documents WHERE id=?", (args.id,)).fetchone()
                    if row is None:
                        raise ValueError("unknown indexed excerpt ID")
                    result = source.revalidate(index.decode(row))
                print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, "kilix-find: " + source.safe_text(exc) + "\n")


if __name__ == "__main__":
    sys.exit(main())
