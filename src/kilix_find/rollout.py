"""Explicit-row export for saved Claude and Codex JSONL records.

Taken from kilix-tui-utils src/kilix_rollout/records.py at
c33c0ee453dbdb0655c7ade41f86c0ea0d84c4f8. MIT; see THIRD-PARTY.md.
"""
from __future__ import annotations


def _pointer(*parts: object) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def adapt_record(provider: str, row: dict) -> dict:
    """Export eligible text from a Claude or Codex JSONL row.

    Unsupported variants are explicit errors. Exclusions are categorical and
    should be copied into source coverage. Text is kept byte-for-byte after
    JSON decoding; embedded terminal controls remain for the renderer to escape.
    """
    result = {"records": [], "excluded": [], "errors": []}
    def emit(role: str, channel: str, text: object, pointer: str,
             *, tool_id: object = None, turn_id: object = None) -> None:
        if not isinstance(text, str):
            result["errors"].append({"code": "unsupported_record", "message": f"non-text content at {pointer}"})
            return
        result["records"].append({
            "role": role, "channel": channel, "text": text,
            "timestamp": row.get("timestamp") if isinstance(row.get("timestamp"), str) else None,
            "timestamp_basis": "source" if isinstance(row.get("timestamp"), str) else "unknown",
            "turn_id": turn_id if isinstance(turn_id, str) else None,
            "tool_call_id": tool_id if isinstance(tool_id, str) else None,
            "json_pointer": pointer,
        })
    def exclude(category: str) -> None:
        if category not in result["excluded"]:
            result["excluded"].append(category)
    def unsupported(what: object) -> None:
        result["errors"].append({"code": "unsupported_record", "message": f"unsupported {provider} record: {str(what)[:160]}"})
    if not isinstance(row, dict):
        unsupported("non-object")
        return result
    kind = row.get("type")
    if not isinstance(kind, str):
        unsupported("type")
        return result
    if provider == "claude":
        if kind in {"system", "summary", "file-history-snapshot", "file-history-delta", "last-prompt", "attachment", "queue-operation", "ai-title", "custom-title", "agent-name", "mode", "permission-mode", "agent-name"}:
            exclude("system_or_metadata")
        elif kind in {"user", "assistant"}:
            if row.get("isMeta") or row.get("isSidechain"):
                exclude("synthetic_or_sidechain")
                return result
            msg = row.get("message")
            if not isinstance(msg, dict) or (msg.get("role") is not None and
                    (not isinstance(msg.get("role"), str) or msg.get("role") != kind)):
                unsupported("message role mismatch")
                return result
            content = msg.get("content")
            if isinstance(content, str):
                if kind == "user" and content.lstrip().startswith(("<system-reminder>", "<task-notification>", "<local-command-stdout>")):
                    exclude("synthetic_user_wrapper")
                else:
                    emit(kind, "message", content, _pointer("message", "content"))
            elif isinstance(content, list):
                for i, block in enumerate(content):
                    if not isinstance(block, dict):
                        unsupported("content block")
                        continue
                    block_kind = block.get("type")
                    base = _pointer("message", "content", i)
                    if not isinstance(block_kind, str):
                        unsupported("content block type")
                        continue
                    if block_kind == "text":
                        value = block.get("text")
                        if kind == "user" and isinstance(value, str) and value.lstrip().startswith(("<system-reminder>", "<task-notification>", "<local-command-stdout>")):
                            exclude("synthetic_user_wrapper")
                        else:
                            emit(kind, "message", value, base + "/text")
                    elif block_kind == "tool_use" and kind == "assistant":
                        # The name is an exact source string; input is data, not an assistant claim.
                        emit("assistant", "tool_request", block.get("name"), base + "/name", tool_id=block.get("id"))
                        parameters = block.get("input")
                        if isinstance(parameters, dict) and isinstance(parameters.get("command"), str):
                            emit("assistant", "tool_request", parameters["command"], base + "/input/command", tool_id=block.get("id"))
                    elif block_kind == "tool_result" and kind == "user":
                        value = block.get("content")
                        if isinstance(value, str):
                            emit("tool", "tool_result", value, base + "/content", tool_id=block.get("tool_use_id"))
                        elif isinstance(value, list):
                            for j, item in enumerate(value):
                                if isinstance(item, dict) and isinstance(item.get("type"), str) and item.get("type") == "text":
                                    emit("tool", "tool_result", item.get("text"), base + _pointer("content", j, "text"), tool_id=block.get("tool_use_id"))
                                else:
                                    exclude("nontext_tool_payload")
                        else:
                            exclude("nontext_tool_payload")
                    elif block_kind in {"thinking", "redacted_thinking", "image", "document"}:
                        exclude("reasoning_or_binary")
                    else:
                        unsupported(block_kind)
            else:
                unsupported("message content")
        else:
            unsupported(kind)
    elif provider == "codex":
        payload = row.get("payload")
        if not isinstance(payload, dict):
            unsupported("payload")
            return result
        if kind == "response_item":
            item_type = payload.get("type")
            if not isinstance(item_type, str):
                unsupported("payload type")
                return result
            channel = payload.get("channel")
            if not (channel is None or isinstance(channel, str)):
                unsupported("response channel")
                return result
            if channel in {"analysis", "reasoning"}:
                exclude("reasoning")
                return result
            if channel not in {None, "final", "commentary"}:
                unsupported("response channel")
                return result
            if item_type == "message":
                role = payload.get("role")
                if not isinstance(role, str):
                    unsupported("message role")
                    return result
                if role in {"system", "developer"}:
                    exclude("system_or_developer")
                    return result
                if role not in {"user", "assistant"}:
                    unsupported("message role")
                    return result
                content = payload.get("content")
                if not isinstance(content, list):
                    unsupported("message content")
                    return result
                for i, part in enumerate(content):
                    if not isinstance(part, dict):
                        unsupported("content part")
                        continue
                    typ = part.get("type")
                    if not isinstance(typ, str):
                        unsupported("content part type")
                        continue
                    if typ in {"input_text", "output_text"} and ((role == "user" and typ == "input_text") or (role == "assistant" and typ == "output_text")):
                        value = part.get("text")
                        if role == "user" and isinstance(value, str) and value.lstrip().startswith(("<environment_context>", "<permissions instructions>", "<skills_instructions>", "<codex_internal_context")):
                            exclude("synthetic_user_context")
                        else:
                            emit(role, "message", value, _pointer("payload", "content", i, "text"))
                    elif typ in {"input_image", "output_image", "image", "audio", "input_audio"}:
                        exclude("binary_payload")
                    else:
                        unsupported(typ)
            elif item_type in {"function_call", "custom_tool_call"}:
                emit("assistant", "tool_request", payload.get("name"), _pointer("payload", "name"), tool_id=payload.get("call_id"))
                if isinstance(payload.get("arguments"), str):
                    emit("assistant", "tool_request", payload["arguments"], _pointer("payload", "arguments"), tool_id=payload.get("call_id"))
                if isinstance(payload.get("input"), str):
                    emit("assistant", "tool_request", payload["input"], _pointer("payload", "input"), tool_id=payload.get("call_id"))
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                emit("tool", "tool_result", payload.get("output"), _pointer("payload", "output"), tool_id=payload.get("call_id"))
            elif item_type in {"reasoning", "compaction"}:
                exclude("reasoning_or_summary")
            else:
                unsupported(item_type)
        elif kind == "event_msg":
            event = payload.get("type")
            if not isinstance(event, str):
                unsupported("event type")
                return result
            if event in {"task_started", "task_complete", "turn_started", "turn_complete", "turn_completed"}:
                emit("unknown", "lifecycle", event, _pointer("payload", "type"))
            elif event in {"agent_reasoning", "agent_reasoning_raw_content", "agent_reasoning_section_break"}:
                exclude("reasoning")
            elif event in {"user_message", "agent_message"}:
                # Duplicated by response_item in current rollouts; cannot prove a
                # standalone event is the authoritative user turn.
                exclude("event_message_mirror")
            elif event in {"token_count", "context_compacted", "turn_aborted"}:
                exclude("metadata")
            else:
                unsupported(event)
        elif kind in {"session_meta", "turn_context"}:
            exclude("system_or_metadata")
        else:
            unsupported(kind)
    else:
        unsupported("provider")
    return result
