"""Explicit source snapshots and exact excerpt provenance; no inference runtime."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from kilix_find.rollout import adapt_record
MAX_SOURCE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
CHUNK = 1000
OVERLAP = 120
def _credential_pattern():
    # Built from pieces so the source line is a detector, not a credential.
    prefix = "gh" + "[pousr]_"
    marker = "-----" + "BE" + "GIN "
    label = "PRIV" + "ATE " + "KE" + "Y-----"
    header = "author" + "ization:" + r"\s*bearer\s+"
    return re.compile(
        rf"(?:{prefix}[A-Za-z0-9]{{20,}}|sk-[A-Za-z0-9_-]{{24,}}|{marker}[A-Z ]*{label}|(?i:{header})[A-Za-z0-9._-]{{16,}})"
    )


SECRET = _credential_pattern()

def digest(data):
    return hashlib.sha256(data).hexdigest()

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

def hash_value(value):
    return digest(canonical(value))

def read_json(path):
    p = Path(path)
    if p.stat().st_size > MAX_TOTAL:
        raise ValueError("JSON exceeds 64 MiB bound")
    return json.loads(p.read_text())

def snapshot(path):
    path = Path(path).resolve(strict=True)
    if any(part in {".ssh", ".gnupg", "github_auth.txt", "credentials.json", "auth.json"} for part in path.parts):
        raise ValueError("credential source excluded")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SOURCE:
            raise ValueError("source must be a regular file <=16 MiB")
        data = b""
        while len(data) < before.st_size:
            part = os.read(fd, min(65536, before.st_size - len(data)))
            if not part:
                raise ValueError("source truncated during snapshot")
            data += part
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("source changed during snapshot")
        if os.pread(fd, len(data), 0) != data:
            raise ValueError("source mutated during snapshot")
        return path, data, before
    finally:
        os.close(fd)

def chunks(text):
    start = 0
    while start < len(text):
        end = min(start + CHUNK, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + CHUNK // 2, end)
            if boundary >= 0:
                end = boundary + 1
        if text[start:end].strip():
            yield start, end
        if end == len(text):
            break
        start = max(start + 1, end - OVERLAP)

def index_sources(config):
    if config.get("schema") != "kilix.find.sources/v1" or not 1 <= len(config.get("sources", [])) <= 64:
        raise ValueError("expected 1..64 explicitly configured sources")
    records, coverage, sources = [], [], []
    total = 0
    keys = set()
    for source in config["sources"]:
        key = source["key"]
        if key in keys:
            raise ValueError("duplicate source key")
        keys.add(key)
        counts = Counter()
        info = {"key":key, "path":source["path"], "status":"ok", "excluded":{}, "errors":[]}
        coverage.append(info)
        try:
            path, data, st = snapshot(source["path"])
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError("source population exceeds 64 MiB")
            generation = digest(data)
            source_id = hash_value([source["kind"], str(path)])[:24]
            descriptor = {"key":key, "source_id":source_id, "source_path":str(path),
                          "source_sha256":generation, "source_bytes":len(data),
                          "mtime_ns":st.st_mtime_ns, "kind":source["kind"],
                          "group":source["group"], "split":source["split"],
                          "title":source["title"]}
            sources.append(descriptor)
            exported = []
            if source["kind"] == "text":
                text = data.decode("utf-8")
                if "\x00" in text:
                    raise ValueError("binary text source")
                exported.append({"text":text, "role":"unknown", "channel":"document",
                                 "timestamp":datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                                 "timestamp_basis":"filesystem_mtime", "session_id":None,
                                 "line":None, "byte_start":0, "byte_end":len(data), "json_pointer":None})
            elif source["kind"] in {"claude", "codex"}:
                byte_start = 0
                selected = set(source.get("line_numbers", []))
                for number, line in enumerate(data.splitlines(keepends=True), 1):
                    offset = byte_start
                    byte_start += len(line)
                    if selected and number not in selected:
                        counts["outside_explicit_line_selection"] += 1
                        continue
                    try:
                        raw = json.loads(line)
                        adapted = adapt_record(source["kind"], raw)
                    except (ValueError, TypeError) as exc:
                        info["errors"].append({"line":number, "error":str(exc)[:160]})
                        continue
                    counts.update(adapted["excluded"])
                    info["errors"].extend({"line":number, **e} for e in adapted["errors"])
                    for record in adapted["records"]:
                        if record["channel"] != "message" or record["role"] not in {"user", "assistant"}:
                            counts["non_message_channel"] += 1
                            continue
                        session = source.get("session_id") or raw.get("sessionId")
                        if not isinstance(session, str) or not session:
                            raise ValueError("rollout source requires session identity")
                        exported.append({**record, "session_id":session, "line":number,
                                         "byte_start":offset, "byte_end":byte_start})
            else:
                raise ValueError("unsupported source kind")
            admitted = 0
            for record in exported:
                if SECRET.search(record["text"]):
                    counts["credential_pattern_record_excluded"] += 1
                    continue
                identity = [source_id, generation, record["byte_start"], record["byte_end"], record["json_pointer"]]
                record_id = hash_value(identity)[:24]
                for start, end in chunks(record["text"]):
                    evidence = {"assistant":"assistant_claim", "user":"user_statement"}.get(record["role"], "unattributed_text")
                    records.append({**descriptor, **{k:v for k,v in record.items() if k != "text"},
                                    "id":hash_value([record_id,start,end])[:24], "record_id":record_id,
                                    "start":start, "end":end, "text":record["text"][start:end],
                                    "record_sha256":digest(record["text"].encode()),
                                    "evidence_class":evidence, "live_state":"unknown"})
                    admitted += 1
            info.update(source_id=source_id, sha256=generation, captured_bytes=len(data),
                        chunks=admitted, explicit_line_selection=source.get("line_numbers"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            info.update(status="unavailable", errors=info["errors"] + [{"error":str(exc)}])
        info["excluded"] = dict(counts)
        if info["errors"] or counts.get("credential_pattern_record_excluded"):
            info["status"] = "partial" if info["status"] == "ok" else info["status"]
    index = {"schema":"kilix.find.index/v1", "captured_at":datetime.now(timezone.utc).isoformat(),
             "sources":sources, "records":records, "coverage":coverage,
             "complete_within_explicit_scope":all(c["status"] == "ok" for c in coverage),
             "scope":"explicit source list and selected message channels/lines only"}
    index["index_sha256"] = hash_value(index)
    return index

def verify_index(index):
    if index.get("schema") != "kilix.find.index/v1":
        raise ValueError("unsupported index")
    if hash_value({k:v for k,v in index.items() if k != "index_sha256"}) != index.get("index_sha256"):
        raise ValueError("index digest mismatch")
    records = index["records"]
    if len(records) > 100000 or len({r["id"] for r in records}) != len(records):
        raise ValueError("invalid record population")
    return records

def pointer_value(value, pointer):
    if not pointer:
        return value
    for part in pointer.split("/")[1:]:
        part = part.replace("~1","/").replace("~0","~")
        value = value[int(part)] if isinstance(value,list) else value[part]
    return value

def revalidate(record):
    path,data,st = snapshot(record["source_path"])
    if digest(data) != record["source_sha256"]:
        raise ValueError("source generation changed; rebuild the index")
    if record["json_pointer"]:
        raw=json.loads(data[record["byte_start"]:record["byte_end"]])
        text=pointer_value(raw,record["json_pointer"])
    else:
        text=data.decode("utf-8")
    if digest(text.encode()) != record["record_sha256"] or text[record["start"]:record["end"]] != record["text"]:
        raise ValueError("record origin mismatch")
    return {"id":record["id"],"same_generation":True,"source_path":str(path),"live_state":"unknown"}

def safe_text(value):
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else "\\u%04x" % ord(c) for c in str(value))
