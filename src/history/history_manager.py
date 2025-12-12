from langchain_core.messages import HumanMessage, AIMessage
import asyncio
import json
class HistoryManager:
    def __init__(self, thread_id: str, checkpoint_ns: str):
        self.thread_id = thread_id
        self.checkpoint_ns = checkpoint_ns
        self._messages = []
        self._store = None

    def attach_store(self, store):
        self._store = store

    def record_user(self, text: str):
        self._messages.append(HumanMessage(content=text))

    def record_ai(self, text: str):
        self._messages.append(AIMessage(content=text))

    def get_messages(self):
        return list(self._messages)

    async def save_latest(self):
        if not self._store:
            return
        import datetime
        ts = datetime.datetime.utcnow().isoformat()
        value = {
            "thread_id": self.thread_id,
            "checkpoint_ns": self.checkpoint_ns,
            "messages": [
                {"type": m.__class__.__name__, "content": m.content}
                for m in self._messages
            ],
            "ts": ts,
        }
        prefix = f"history/{self.thread_id}"
        key = f"/{self.checkpoint_ns}/latest"
        await self._store.aput(prefix, key, value)

    async def load_latest(self):
        if not self._store:
            return False
        prefix = f"history/{self.thread_id}"
        key = f"/{self.checkpoint_ns}/latest"
        row = await self._store.aget(prefix, key)
        if not row:
            return False
        val = row.get("value") if isinstance(row, dict) else getattr(row, "value", None)
        if val is None:
            return False
        if isinstance(val, (bytes, bytearray)):
            try:
                val = val.decode("utf-8")
            except Exception:
                return False
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                return False
        if not isinstance(val, dict):
            return False
        msgs = val.get("messages", [])
        restored = []
        for m in msgs:
            t = m.get("type")
            c = m.get("content")
            if t == "HumanMessage":
                restored.append(HumanMessage(content=c))
            elif t == "AIMessage":
                restored.append(AIMessage(content=c))
            else:
                restored.append(HumanMessage(content=c))
        self._messages = restored
        return True

    def clear(self):
        self._messages = []


