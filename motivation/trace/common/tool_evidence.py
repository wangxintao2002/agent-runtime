"""Recognize specific Claude pre-execution rejections, with exact CLI evidence."""
from collections import Counter
import re

STALE_WRITE_ERROR = (
    "<tool_use_error>File has been modified since read, either by the user or by a linter. "
    "Read it again before attempting to write it.</tool_use_error>"
)


def rejected_tools(messages, hooked_ids):
    """Account for known rejections, never excuse an arbitrary missing hook.

    These calls did not execute. Replay must still match the full original
    tool input and exact error result, and reject any actual execution hook.
    """
    rejected = rejected_edits(messages, hooked_ids)
    calls = [b for m in messages if m.get('type') == 'assistant'
             for b in m.get('message', {}).get('content', []) if b.get('type') == 'tool_use']
    results = [b for m in messages if m.get('type') == 'user'
               for b in m.get('message', {}).get('content', []) if b.get('type') == 'tool_result']
    call_counts = Counter(b['id'] for b in calls)
    result_counts = Counter(b['tool_use_id'] for b in results)
    results_by_id = {b['tool_use_id']: b for b in results}
    for call in calls:
        key = call['id']
        if key in hooked_ids or call_counts[key] != 1 or result_counts[key] != 1:
            continue
        args, result = call.get('input', {}), results_by_id[key]
        if (call.get('name') == 'Write' and isinstance(args, dict)
                and isinstance(args.get('file_path'), str) and args['file_path']
                and isinstance(args.get('content'), str)
                and result.get('is_error') is True and result.get('content') == STALE_WRITE_ERROR):
            rejected[key] = dict(tool_name='Write', tool_input=args, error_result=result)
    return rejected


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
