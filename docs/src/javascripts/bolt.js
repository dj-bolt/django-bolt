// Copy buttons on the landing page. Zensical loads this script on each page,
// and instant navigation replaces the page body, so the listener is on document.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  navigator.clipboard.writeText(button.dataset.copy).then(
    () => {
      button.textContent = "Copied";
      setTimeout(() => (button.textContent = "Copy"), 1500);
    },
    () => {
      // No clipboard access: select the command, so that Ctrl+C copies it.
      const target = document.getElementById("bl-install-cmd");
      if (target) window.getSelection().selectAllChildren(target);
      button.textContent = "Press Ctrl+C";
    },
  );
});
