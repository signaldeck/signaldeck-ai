/* SignalDeck action integration and a read-only AG-UI SSE listener. */
(function () {
  function initSignalDeckAIChat(element) {
    const root = typeof element === "string" ? document.getElementById(element) : element;
    if (!root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";

    const base = root.dataset.endpoint;
    const actionHash = root.dataset.actionhash;
    const status = root.querySelector(".sdai-status");
    const tokenField = root.querySelector(".sdai-token");
    const transcript = root.querySelector(".sdai-transcript");
    const connect = root.querySelector(".sdai-connect");
    const fresh = root.querySelector(".sdai-new");
    const field = root.querySelector(".sdai-input");
    const send = root.querySelector(".sdai-send");
    const approvals = root.querySelector(".sdai-approval");
    const approvalText = root.querySelector(".sdai-approval-text");
    const form = root.querySelector(".sdai-form");
    let connectionId = null, busy = false, interrupt = null;

    function render(role, value) {
      const node = document.createElement("div");
      node.className = "sdai-message sdai-" + role;
      node.textContent = value;
      transcript.appendChild(node);
      transcript.scrollTop = transcript.scrollHeight;
      return node;
    }

    function updateUI() {
      const connected = Boolean(connectionId);
      connect.textContent = connected ? "Trennen" : "Verbinden";
      connect.disabled = busy;
      tokenField.hidden = connected;
      tokenField.disabled = connected || busy;
      field.disabled = !connected || busy || Boolean(interrupt);
      send.disabled = !connected || busy || Boolean(interrupt);
      fresh.disabled = !connected || busy;
      status.textContent = busy ? "Agent arbeitet ..." :
        connected ? (interrupt ? "Freigabe erforderlich" : "Verbunden") : "Nicht verbunden";
    }

    async function action(ai_action, params = {}) {
      const fd = new FormData();
      fd.append("payload", JSON.stringify({
        actionhash: actionHash,
        ai_action,
        ...(connectionId ? {connection_id: connectionId} : {}),
        ...params
      }));
      const response = await fetch("/run", {
        method: "POST", body: fd, credentials: "same-origin",
        headers: {"Accept": "application/json"},
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const result = await response.json();
      if (!result.ok) throw new Error(result.error || "Aktion fehlgeschlagen");
      return result;
    }

    connect.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      updateUI();
      try {
        if (connectionId) {
          await action("disconnect");
          connectionId = null;
          interrupt = null;
          transcript.replaceChildren();
          approvals.hidden = true;
        } else {
          const token = tokenField.value.trim();
          if (!token) throw new Error("Bitte API-Token eingeben.");
          const result = await action("connect", {token});
          connectionId = result.connection_id;
          if (!connectionId) throw new Error("Verbindungskennung fehlt.");
          tokenField.value = "";
        }
      } catch (err) {
        render("info", "Verbindung: " + String(err.message || err));
      } finally {
        busy = false;
        updateUI();
      }
    });

    fresh.addEventListener("click", async () => {
      if (!connectionId || busy) return;
      busy = true;
      updateUI();
      try {
        await action("new_chat");
        interrupt = null;
        approvals.hidden = true;
        transcript.replaceChildren();
      } catch (err) {
        render("info", "Neuer Chat: " + String(err.message || err));
      } finally {
        busy = false;
        updateUI();
      }
    });

    async function listen(runId) {
      const response = await fetch(
        base + "/events/" + encodeURIComponent(runId),
        {method: "GET", credentials: "same-origin",
         headers: {"X-SignalDeck-AI-Connection": connectionId, "Accept": "text/event-stream"}}
      );
      if (!response.ok || !response.body) throw new Error("SSE HTTP " + response.status);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let completed = false;
      const assistantNodes = new Map();

      function dispatch(evt) {
        switch (evt.type) {
          case "TEXT_MESSAGE_START":
            if (evt.role === "assistant") {
              assistantNodes.set(evt.messageId, render("assistant", ""));
            }
            break;
          case "TEXT_MESSAGE_CONTENT": {
            let node = assistantNodes.get(evt.messageId);
            if (!node) {
              node = render("assistant", "");
              assistantNodes.set(evt.messageId, node);
            }
            node.textContent += String(evt.delta || "");
            transcript.scrollTop = transcript.scrollHeight;
            break;
          }
          case "TOOL_CALL_START":
            render("info", "Tool: " + (evt.toolCallName || "unbekannt"));
            break;
          case "RUN_ERROR":
            render("info", "Agent-Fehler: " + (evt.message || "unbekannt"));
            completed = true;
            break;
          case "RUN_FINISHED":
            completed = true;
            if (evt.outcome?.type === "interrupt") {
              const item = evt.outcome.interrupts?.[0];
              if (item && item.id) {
                interrupt = item;
                approvalText.textContent = item.message || ("Freigabe: " + item.reason);
                approvals.hidden = false;
              }
            }
            break;
        }
      }

      function parseFrame(frame) {
        const data = frame.split("\n").filter(line => line.startsWith("data:"))
          .map(line => line.slice(5).trimStart()).join("\n");
        if (data) dispatch(JSON.parse(data));
      }

      try {
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, {stream:true}).replace(/\r\n/g, "\n");
          let index;
          while ((index = buffer.indexOf("\n\n")) >= 0) {
            parseFrame(buffer.slice(0, index));
            buffer = buffer.slice(index + 2);
          }
        }
        if (buffer.trim()) parseFrame(buffer);
        if (!completed) render("info", "AG-UI-Stream unerwartet beendet.");
      } finally {
        reader.releaseLock();
      }
    }

    async function runAction(name, args) {
      busy = true;
      updateUI();
      try {
        const result = await action(name, args);
        await listen(result.run_id);
      } catch (err) {
        render("info", "Agentenlauf: " + String(err.message || err));
      } finally {
        busy = false;
        updateUI();
      }
    }

    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!connectionId || busy || interrupt) return;
      const text = field.value.trim();
      if (!text) return;
      field.value = "";
      render("user", text);
      void runAction("send", {message: text});
    });

    approvals.addEventListener("click", event => {
      const decision = event.target.dataset.decision;
      if (!interrupt || busy || !["yes","no","session"].includes(decision)) return;
      const interruptId = interrupt.id;
      // Keep the current approval displayed if the server rejects the action.
      busy = true;
      updateUI();
      (async () => {
        try {
          const result = await action("approve", {interrupt_id: interruptId, decision});
          interrupt = null;
          approvals.hidden = true;
          await listen(result.run_id);
        } catch (err) {
          render("info", "Freigabe: " + String(err.message || err));
        } finally {
          busy = false;
          updateUI();
        }
      })();
    });

    updateUI();
  }

  window.initSignalDeckAIChat = initSignalDeckAIChat;
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".sdai-chat").forEach(initSignalDeckAIChat);
  });
})();
