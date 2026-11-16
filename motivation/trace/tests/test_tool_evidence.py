import copy
import unittest
from tool_evidence import rejected_edits


class ToolEvidenceTests(unittest.TestCase):
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
