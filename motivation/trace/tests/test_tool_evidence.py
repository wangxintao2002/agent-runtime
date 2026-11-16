import copy
import unittest
from tool_evidence import rejected_edits, rejected_tools, STALE_WRITE_ERROR


class ToolEvidenceTests(unittest.TestCase):
    def test_stale_write_needs_exact_failure_input_and_unique_evidence(self):
        call = dict(type='tool_use', id='w', name='Write', input=dict(file_path='/repo/a.py', content='new'))
        result = dict(type='tool_result', tool_use_id='w', is_error=True, content=STALE_WRITE_ERROR)
        messages = [dict(type='assistant', message=dict(content=[call])),
                    dict(type='user', message=dict(content=[result]))]
        self.assertEqual(rejected_tools(messages, [])['w']['tool_input'], call['input'])
        self.assertEqual(rejected_tools(messages, ['w']), {})
        self.assertEqual(rejected_edits(messages, []), {})
        for changes in ({'is_error': False}, {'content': 'unknown failure'},
                        {'content': STALE_WRITE_ERROR + ' extra'}, {'content': None}):
            changed = copy.deepcopy(messages)
            changed[1]['message']['content'][0].update(changes)
            self.assertEqual(rejected_tools(changed, []), {})
        for field, value in [('content', None), ('file_path', '')]:
            changed = copy.deepcopy(messages)
            changed[0]['message']['content'][0]['input'][field] = value
            self.assertEqual(rejected_tools(changed, []), {})
        self.assertEqual(rejected_tools(messages[:1], []), {})
        self.assertEqual(rejected_tools(messages + messages[:1], []), {})
        self.assertEqual(rejected_tools(messages + messages[1:], []), {})

    def test_only_exact_explicit_failed_edit_is_accounted_for(self):
        messages = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "e",
                     "name": "Edit", "input": {"file_path": "/repo/a.py", "old_string": "old", "new_string": "new"}}]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "e", "is_error": True,
                     "content": "<tool_use_error>String to replace not found in file.\nString: old</tool_use_error>"}]}}]
        self.assertEqual(set(rejected_edits(messages, [])), {"e"})
        self.assertEqual(rejected_edits(messages, ["e"]), {})
        for field, value in [("is_error", False), ("content", "success"), ("content", "<tool_use_error>Other error</tool_use_error>")]:
            changed = copy.deepcopy(messages)
            changed[1]["message"]["content"][0][field] = value
            self.assertEqual(rejected_edits(changed, []), {})
        changed = copy.deepcopy(messages)
        changed[1]["message"]["content"][0]["content"] = "<tool_use_error>Found 3 matches of the string to replace, but replace_all is false. To replace all occurrences, set replace_all to true. To replace only one occurrence, please provide more context to uniquely identify the instance.\nString: old</tool_use_error>"
        self.assertEqual(set(rejected_edits(changed, [])), {"e"})
        changed[0]["message"]["content"][0]["input"]["replace_all"] = True
        self.assertEqual(rejected_edits(changed, []), {})
        changed[0]["message"]["content"][0]["input"]["old_string"] = 1
        changed[1]["message"]["content"][0]["content"] = None
        self.assertEqual(rejected_edits(changed, []), {})


if __name__ == "__main__":
    unittest.main()
