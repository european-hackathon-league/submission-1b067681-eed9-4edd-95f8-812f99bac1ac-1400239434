// Same origin when served by FastAPI; set to "http://localhost:8000" if you serve the frontend elsewhere.
const API_BASE = "";

const messagesEl = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("input");
const statusEl = document.getElementById("status");
const history = []; // [{role, content}] sent back so the agent keeps context

function addMessage(role, text, meta) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  if (meta) {
    const m = document.createElement("div");
    m.className = "meta";
    m.textContent = meta;
    div.appendChild(m);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  addMessage("user", message);
  const pending = addMessage("assistant", "thinking…");
  form.querySelector("button").disabled = true;

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history }),
    });
    const data = await res.json();
    pending.remove();
    if (!res.ok) {
      addMessage("error", data.detail || `HTTP ${res.status}`);
      return;
    }
    const meta = data.tool_calls.length ? `tools: ${data.tool_calls.join(", ")}` : "";
    addMessage("assistant", data.reply, meta);
    history.push({ role: "user", content: message }, { role: "assistant", content: data.reply });
  } catch (err) {
    pending.remove();
    addMessage("error", `Request failed: ${err}`);
  } finally {
    form.querySelector("button").disabled = false;
    input.focus();
  }
});

fetch(`${API_BASE}/api/health`)
  .then((r) => r.json())
  .then((d) => (statusEl.textContent = d.openai_key_set ? "backend ok" : "backend ok · OPENAI_API_KEY missing"))
  .catch(() => (statusEl.textContent = "backend unreachable"));
