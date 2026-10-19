"""Assemble provider chat streams into the response shapes used by LLMService."""
from __future__ import annotations

import json
from typing import Any


class ChatStreamAssembler:
    def __init__(self, provider: str):
        self.provider = provider
        self.content: list[str] = []
        self.done = False
        self.done_reason: str | None = None
        self.progress_chunks = 0
        self._sse_data: list[str] = []

    def feed_line(self, line: str) -> bool:
        """Return true only when the provider reports generated output."""
        if self.provider == "ollama":
            return self._accept(line) if line.strip() else False
        if line.startswith(":") or line.startswith("event:"):
            return False
        if line.startswith("data:"):
            self._sse_data.append(line[5:].lstrip())
            return False
        if not line.strip() and self._sse_data:
            data = "\n".join(self._sse_data)
            self._sse_data.clear()
            return self._accept(data)
        return False

    def finish_frame(self) -> bool:
        if not self._sse_data:
            return False
        data = "\n".join(self._sse_data)
        self._sse_data.clear()
        return self._accept(data)

    def _accept(self, data: str) -> bool:
        if data == "[DONE]":
            self.done = True
            return False
        event = json.loads(data)
        if not isinstance(event, dict):
            raise ValueError("chat stream event is not an object")
        if event.get("error"):
            raise ValueError("provider reported a chat stream error")
        if self.provider == "ollama":
            message = event.get("message") or {}
            if not isinstance(message, dict):
                raise ValueError("Ollama chat stream message is invalid")
            content = message.get("content") or ""
            reasoning = message.get("thinking") or ""
            if content:
                self.content.append(str(content))
            if event.get("done") is True:
                self.done = True
                self.done_reason = event.get("done_reason")
        else:
            choices = event.get("choices") or []
            choice = choices[0] if choices else {}
            if not isinstance(choice, dict):
                raise ValueError("chat stream choice is invalid")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise ValueError("chat stream delta is invalid")
            content = delta.get("content") or ""
            if isinstance(content, list):
                content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
            if content:
                self.content.append(str(content))
            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking") or ""
            if choice.get("finish_reason") is not None:
                self.done = True
                self.done_reason = str(choice["finish_reason"])
        meaningful = bool(content or reasoning)
        if meaningful:
            self.progress_chunks += 1
        return meaningful

    def result(self) -> dict[str, Any]:
        if not self.done:
            raise ValueError("chat stream ended before completion")
        text = "".join(self.content)
        if self.provider == "ollama":
            return {"message": {"content": text}, "done": True, "done_reason": self.done_reason}
        return {"choices": [{"message": {"content": text}, "finish_reason": self.done_reason}]}
