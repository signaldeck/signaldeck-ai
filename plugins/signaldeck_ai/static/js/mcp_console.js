/* Interactive SignalDeck MCP tool console: actions use the standard /run. */
(function () {
  function initSignalDeckMCPConsole(element) {
    const root = typeof element === "string" ? document.getElementById(element) : element;
    if (!root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";
    const actionHash = root.dataset.actionhash;

    root.querySelectorAll(".sdai-mcp-tool").forEach(panel => {
      const name = panel.querySelector("summary strong")?.textContent?.trim();
      const button = panel.querySelector(".sdai-mcp-execute");
      const field = panel.querySelector(".sdai-mcp-args");
      const feedback = panel.querySelector(".sdai-mcp-feedback");
      const resultBox = panel.querySelector(".sdai-mcp-result-box");
      const result = panel.querySelector(".sdai-mcp-result");
      const writeConfirmation = panel.querySelector(".sdai-mcp-confirm");

      button.addEventListener("click", async () => {
        if (button.disabled) return;
        resultBox.hidden = true;
        let argumentsObject;
        try {
          argumentsObject = JSON.parse(field.value);
          if (!argumentsObject || Array.isArray(argumentsObject) || typeof argumentsObject !== "object") {
            throw new Error("Argumente müssen ein JSON-Objekt sein.");
          }
        } catch (error) {
          feedback.textContent = "Ungültige Argumente: " + error.message;
          return;
        }
        if (writeConfirmation && !writeConfirmation.checked) {
          feedback.textContent = "Bitte die Schreibaktion ausdrücklich bestätigen.";
          return;
        }
        button.disabled = true;
        feedback.textContent = "Wird ausgeführt …";
        try {
          const data = new FormData();
          data.append("payload", JSON.stringify({
            actionhash: actionHash,
            mcp_action: "execute_tool",
            tool_name: name,
            arguments: JSON.stringify(argumentsObject),
            confirm_write: writeConfirmation?.checked ? "true" : "false",
          }));
          const response = await fetch("/run", {
            method: "POST",
            credentials: "same-origin",
            headers: {"Accept": "application/json"},
            body: data,
          });
          if (!response.ok) throw new Error("HTTP " + response.status);
          const payload = await response.json();
          if (!payload.ok) throw new Error(payload.error || "Unbekannter Fehler");
          result.textContent = JSON.stringify(payload.result, null, 2);
          resultBox.hidden = false;
          feedback.textContent = "Erfolgreich";
        } catch (error) {
          feedback.textContent = "Fehler: " + String(error.message || error);
        } finally {
          if (writeConfirmation) writeConfirmation.checked = false;
          button.disabled = false;
        }
      });
    });
  }

  window.initSignalDeckMCPConsole = initSignalDeckMCPConsole;
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".sdai-mcp").forEach(initSignalDeckMCPConsole);
  });
})();
