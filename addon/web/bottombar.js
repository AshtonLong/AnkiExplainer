/* Anki Explainer: the "Explain"/"Hint" button in the reviewer's bottom bar.
 * Sits right after Anki's "Edit" button, styled like Anki's own buttons. */
(function () {
  "use strict";
  let current = { visible: true, label: "Explain", busy: false, nudge: false, shortcut: "X" };

  function button() {
    let btn = document.getElementById("explainer-btn");
    if (btn) return btn;
    const edit = document.querySelector("#innertable td.stat button");
    if (!edit) return null;
    btn = document.createElement("button");
    btn.id = "explainer-btn";
    btn.type = "button";
    btn.addEventListener("click", function () {
      pycmd("explainer:explain");
    });
    edit.insertAdjacentElement("afterend", btn);
    return btn;
  }

  function render() {
    const btn = button();
    if (!btn) return;
    btn.hidden = !current.visible;
    btn.classList.toggle("busy", !!current.busy);
    btn.classList.toggle("nudge", !!current.nudge && !current.busy);
    btn.textContent = "";
    if (current.busy) {
      const spin = document.createElement("span");
      spin.className = "explainer-spin";
      spin.setAttribute("aria-hidden", "true");
      btn.appendChild(spin);
      btn.appendChild(document.createTextNode(current.label === "Hint" ? "Hinting…" : "Explaining…"));
    } else {
      btn.appendChild(document.createTextNode(current.label));
    }
    if (current.nudge && !current.busy) {
      const dot = document.createElement("span");
      dot.className = "explainer-dot";
      dot.setAttribute("aria-hidden", "true");
      btn.appendChild(dot);
    }
    const key = current.shortcut ? "Shortcut key: " + current.shortcut : "";
    btn.title = current.nudge
      ? "You've missed this a few times. Want an explanation?" + (key ? "\n" + key : "")
      : key;
    btn.setAttribute("aria-label", current.label + (current.nudge ? " (you've missed this card a few times)" : ""));
  }

  window.ExplainerBar = {
    update: function (state) {
      current = Object.assign(current, state || {});
      render();
    },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render);
  else render();
})();
