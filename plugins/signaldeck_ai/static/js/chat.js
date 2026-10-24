/* Portable AG-UI 1.0 client; no cli-agent-specific run API. */
(function () {
  const newid = () => (globalThis.crypto && crypto.randomUUID) ? crypto.randomUUID() :
    String(Date.now()) + "-" + Math.random().toString(36).slice(2);

  function initSignalDeckAIChat(element) {
    const root = typeof element === "string" ? document.getElementById(element) : element;
    if (!root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";

    const base = root.dataset.endpoint;
    const status = root.querySelector(".sdai-status");
    const transcript = root.querySelector(".sdai-transcript");
    const connect = root.querySelector(".sdai-connect");
    const fresh = root.querySelector(".sdai-new");
    const field = root.querySelector(".sdai-input");
    const send = root.querySelector(".sdai-send");
    const approvals = root.querySelector(".sdai-approval");
    const approvalText = root.querySelector(".sdai-approval-text");
    const form = root.querySelector(".sdai-form");
    let connected = false, busy = false, threadId = newid(), messages = [], interrupt = null;

    function render(role, value) {
      const node = document.createElement("div");
      node.className = "sdai-message sdai-" + role;
      node.textContent = value;
      transcript.appendChild(node);
      transcript.scrollTop = transcript.scrollHeight;
      return node;
    }
    function updateUI() {
      connect.textContent = connected ? "Trennen" : "Verbinden";
      field.disabled = !connected || busy;
      send.disabled = !connected || busy;
      fresh.disabled = !connected || busy;
      status.textContent = busy ? "Agent arbeitet ..." : connected ? "Verbunden" : "Nicht verbunden";
    }
    async function closeThread() {
      // Unsupported management endpoints may safely return an error.
      await fetch(base + "/thread/" + encodeURIComponent(threadId), { method: "DELETE" }).catch(() => {});
    }
    connect.addEventListener("click", async () => {
      if (connected) {
        if (busy) return;
        connected = false;
        interrupt = null;
        approvals.hidden = true;
        updateUI();
        return;
      }
      status.textContent = "Verbindung wird geprüft ...";
      try {
        const response = await fetch(base + "/capabilities");
        if (!response.ok) throw new Error("HTTP " + response.status);
        connected = true;
      } catch (err) {
        render("info", "Verbindung fehlgeschlagen: " + String(err));
      }
      updateUI();
    });
    fresh.addEventListener("click", async () => {
      if (busy) return;
      await closeThread();
      threadId = newid();
      messages = [];
      transcript.replaceChildren();
      interrupt = null;
      approvals.hidden = true;
      updateUI();
    });

    async function run(resume) {
      busy = true;
      updateUI();
      let response, assistantNode = null, assistantText = "", toolCalls = [];
      let finished = false;
      const requestBody = {
        protocolVersion: "1.0", threadId, runId: newid(), messages: messages.slice(),
      };
      if (resume) requestBody.resume = [resume];
      try {
        response = await fetch(base + "/run", {
          method: "POST", headers: {"Content-Type":"application/json", "Accept":"text/event-stream"},
          body: JSON.stringify(requestBody),
        });
        if (!response.ok || !response.body) throw new Error("HTTP " + response.status);
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        async function handleEvent(evt) {
          switch (evt.type) {
            case "TEXT_MESSAGE_START":
              if (!assistantNode) assistantNode = render("assistant", "");
              break;
            case "TEXT_MESSAGE_CONTENT":
              if (!assistantNode) assistantNode = render("assistant", "");
              assistantText += evt.delta || "";
              assistantNode.textContent = assistantText;
              transcript.scrollTop = transcript.scrollHeight;
              break;
            case "TOOL_CALL_START":
              render("info", "Tool: " + (evt.toolCallName || "unbekannt"));
              break;
            case "RUN_ERROR":
              render("info", "Agent-Fehler: " + (evt.message || "unbekannt"));
              finished = true;
              break;
            case "RUN_FINISHED":
              finished = true;
              if (evt.outcome && evt.outcome.type === "interrupt") {
                const item = (evt.outcome.interrupts || [])[0];
                if (item) {
                  interrupt = item;
                  approvalText.textContent = item.message || ("Tool-Freigabe: " + item.reason);
                  approvals.hidden = false;
                }
              }
              break;
          }
        }
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, {stream:true}).replace(/\r\n/g, "\n");
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) !== -1) {
            const frame = buffer.slice(0,boundary);
            buffer = buffer.slice(boundary+2);
            const data = frame.split("\n").filter(line => line.startsWith("data:"))
              .map(line => line.slice(5).trimStart()).join("\n");
            if (data) {
              try { await handleEvent(JSON.parse(data)); }
              catch (err) { render("info", "Ungültiges Event: " + String(err)); }
            }
          }
        }
        if (assistantText && !interrupt) messages.push({ id: newid(), role:"assistant", content:assistantText });
        if (!finished) render("info", "Verbindung während des Runs unterbrochen.");
      } catch (err) {
        render("info", "Übertragungsfehler: " + String(err));
      } finally {
        busy = false;
        updateUI();
      }
    }

    form.addEventListener("submit", event => {
      event.preventDefault();
      if (busy || !connected || interrupt) return;
      const prompt = field.value.trim();
      if (!prompt) return;
      field.value = "";
      messages.push({id: newid(), role: "user", content: prompt});
      render("user", prompt);
      void run(null);
    });
    approvals.addEventListener("click", event => {
      const decision = event.target.dataset.decision;
      if (!interrupt || !["yes","no","session"].includes(decision) || busy) return;
      const interruptId = interrupt.id;
      interrupt = null;
      approvals.hidden = true;
      void run({
        interruptId,
        status: "resolved",
        payload: {approved: decision !== "no", rememberForSession: decision === "session"},
      });
    });
    updateUI();
  }
  window.initSignalDeckAIChat = initSignalDeckAIChat;
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".sdai-chat").forEach(initSignalDeckAIChat);
  });
})();
