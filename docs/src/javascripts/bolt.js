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
      button.textContent = "Press Ctrl+C";
    },
  );
});
