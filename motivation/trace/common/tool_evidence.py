"""Explicitly recognize a reproducible Claude Edit rejection with no tool hook."""
import re


def rejected_edits(messages, hooked_ids):
    calls = {b["id"]: b for m in messages if m.get("type") == "assistant"
             for b in m.get("message", {}).get("content", []) if b.get("type") == "tool_use"}
    results = {b["tool_use_id"]: b for m in messages if m.get("type") == "user"
               for b in m.get("message", {}).get("content", []) if b.get("type") == "tool_result"}
    rejected = {}
    for key in calls.keys() - set(hooked_ids):
        call, result = calls[key], results.get(key, {})
        old = call.get("input", {}).get("old_string")
        expected = "<tool_use_error>String to replace not found in file.\nString: " + old + "</tool_use_error>" if isinstance(old, str) else None
        content = result.get("content")
        multiple = re.fullmatch(r"<tool_use_error>Found (\d+) matches of the string to replace, but replace_all is false\. To replace all occurrences, set replace_all to true\. To replace only one occurrence, please provide more context to uniquely identify the instance\.\nString: " + re.escape(old) + r"</tool_use_error>", content) if isinstance(content, str) and isinstance(old, str) else None
        known_error = content == expected or (multiple and int(multiple[1]) > 1 and call.get("input", {}).get("replace_all", False) is False)
        if call.get("name") == "Edit" and isinstance(old, str) and old and result.get("is_error") is True and known_error:
            rejected[key] = {"tool_name": "Edit", "tool_input": call["input"], "error_result": result}
    return rejected
