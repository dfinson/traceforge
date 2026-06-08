"""Generate test fixtures with proper UUIDs for SDK parsing."""
import json
import uuid


def uid():
    return str(uuid.uuid4())


# --- Copilot session fixture ---
copilot_lines = [
    {"type": "session.start", "id": uid(), "timestamp": "2024-06-01T10:00:00Z", "data": {"sessionId": uid(), "producer": "copilot-cli", "copilotVersion": "1.2.3", "startTime": "2024-06-01T10:00:00Z", "version": 1, "selectedModel": "claude-sonnet-4-20250514", "context": {"cwd": "/home/user/project"}}},
    {"type": "user.message", "id": uid(), "timestamp": "2024-06-01T10:00:05Z", "data": {"content": "Create a hello world function in Python"}},
    {"type": "assistant.turn_start", "id": uid(), "timestamp": "2024-06-01T10:00:06Z", "data": {"turnId": "turn-1", "interactionId": "int-1"}},
    {"type": "assistant.message", "id": uid(), "timestamp": "2024-06-01T10:00:07Z", "data": {"messageId": "msg-1", "content": "I will create a hello world function for you."}},
    {"type": "tool.execution_start", "id": uid(), "timestamp": "2024-06-01T10:00:08Z", "data": {"toolCallId": "tc-0001", "toolName": "create", "arguments": {"path": "hello.py", "content": "print('hello')"}}},
    {"type": "tool.execution_complete", "id": uid(), "timestamp": "2024-06-01T10:00:09Z", "data": {"toolCallId": "tc-0001", "success": True, "result": {"content": "File created: hello.py", "detailedContent": None}}},
    {"type": "tool.execution_start", "id": uid(), "timestamp": "2024-06-01T10:00:10Z", "data": {"toolCallId": "tc-0002", "toolName": "powershell", "arguments": "python hello.py"}},
    {"type": "tool.execution_complete", "id": uid(), "timestamp": "2024-06-01T10:00:11Z", "data": {"toolCallId": "tc-0002", "success": True, "result": {"content": "Hello, world!", "detailedContent": None}}},
    {"type": "assistant.message", "id": uid(), "timestamp": "2024-06-01T10:00:12Z", "data": {"messageId": "msg-2", "content": "Done! I created hello.py and verified it works."}},
    {"type": "assistant.turn_end", "id": uid(), "timestamp": "2024-06-01T10:00:13Z", "data": {"turnId": "turn-1"}},
    {"type": "assistant.usage", "id": uid(), "timestamp": "2024-06-01T10:00:13Z", "data": {"model": "claude-sonnet-4-20250514", "inputTokens": 1250, "outputTokens": 340, "cacheReadTokens": 800, "cacheWriteTokens": 200, "cost": 0.0045, "duration": 3500}},
    {"type": "hook.start", "id": uid(), "timestamp": "2024-06-01T10:00:14Z", "data": {"hookInvocationId": "hook-1", "hookType": "pre-commit"}},
    {"type": "hook.end", "id": uid(), "timestamp": "2024-06-01T10:00:15Z", "data": {"hookInvocationId": "hook-1", "hookType": "pre-commit", "success": True}},
    {"type": "session.info", "id": uid(), "timestamp": "2024-06-01T10:00:16Z", "data": {"infoType": "notification", "message": "Session saved"}},
    {"type": "session.shutdown", "id": uid(), "timestamp": "2024-06-01T10:00:17Z", "data": {"shutdownType": "routine", "totalPremiumRequests": 3, "totalApiDurationMs": 3500, "codeChanges": {"filesModified": ["hello.py"], "linesAdded": 2, "linesRemoved": 0}, "modelMetrics": {}, "sessionStartTime": 1717232400}},
]

with open("tests/fixtures/copilot_session.jsonl", "w") as f:
    for line in copilot_lines:
        f.write(json.dumps(line) + "\n")

# --- Claude session fixture ---
claude_lines = [
    {"type": "user", "message": {"content": "Read the contents of main.py and fix any bugs"}, "cwd": "/home/user/project", "sessionId": "claude-sess-456"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll read main.py first to check for bugs."}, {"type": "tool_use", "id": "tu-1", "name": "read_file", "input": {"path": "main.py"}}], "model": "claude-sonnet-4-20250514"}},
    {"type": "assistant", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-1", "content": "def add(a, b):\n    return a - b\n", "is_error": False}], "model": "claude-sonnet-4-20250514"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I found a bug: the add function subtracts instead of adding. Let me fix it."}, {"type": "tool_use", "id": "tu-2", "name": "write_file", "input": {"path": "main.py", "content": "def add(a, b):\n    return a + b\n"}}], "model": "claude-sonnet-4-20250514"}},
    {"type": "assistant", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-2", "content": "File written successfully", "is_error": False}], "model": "claude-sonnet-4-20250514"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I've fixed the bug in main.py. The add function was using subtraction (-) instead of addition (+)."}], "model": "claude-sonnet-4-20250514"}},
    {"type": "user", "message": {"content": "Thanks, run the tests now"}, "cwd": "/home/user/project", "sessionId": "claude-sess-456"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu-3", "name": "bash", "input": {"command": "python -m pytest tests/"}}], "model": "claude-sonnet-4-20250514"}},
    {"type": "assistant", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-3", "content": [{"type": "text", "text": "===== 3 passed in 0.5s ====="}], "is_error": False}], "model": "claude-sonnet-4-20250514"}},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "The tests all pass now.", "signature": "sig123"}, {"type": "text", "text": "All 3 tests pass. The fix is working correctly."}], "model": "claude-sonnet-4-20250514"}},
    {"type": "result", "subtype": "success", "duration_ms": 12500, "duration_api_ms": 10200, "is_error": False, "num_turns": 4, "session_id": "claude-sess-456", "total_cost_usd": 0.0089, "usage": {"input_tokens": 3500, "output_tokens": 450, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 100}},
]

with open("tests/fixtures/claude_session.jsonl", "w") as f:
    for line in claude_lines:
        f.write(json.dumps(line) + "\n")

# --- Verify ---
from copilot.generated.session_events import SessionEvent as CSE

print("Verifying copilot fixture...")
with open("tests/fixtures/copilot_session.jsonl") as f:
    for i, raw_line in enumerate(f, 1):
        obj = json.loads(raw_line)
        try:
            CSE.from_dict(obj)
        except Exception as e:
            print(f"  Line {i} FAIL: {e}")
print("  Copilot fixture OK")

from claude_agent_sdk._internal.message_parser import parse_message

print("Verifying claude fixture...")
with open("tests/fixtures/claude_session.jsonl") as f:
    for i, raw_line in enumerate(f, 1):
        obj = json.loads(raw_line)
        try:
            parse_message(obj)
        except Exception as e:
            print(f"  Line {i} FAIL: {e}")
print("  Claude fixture OK")
