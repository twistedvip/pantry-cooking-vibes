// Progressive enhancement for the URL-import form. import_url() fetches the
// page server-side (up to a 20s timeout plus retries), so without feedback the
// button looks frozen and invites a double-submit. On submit we disable the
// button and swap its label to "Importing…". The POST still proceeds: the
// browser has already begun navigating by the time this handler runs, so
// disabling here only blocks a second click. Without JS the form works exactly
// as before.
(function () {
  "use strict";
  var form = document.querySelector(".import-form");
  if (!form) return;

  function button() {
    return form.querySelector('button[type="submit"]');
  }

  form.addEventListener("submit", function () {
    var btn = button();
    if (!btn) return;
    btn.dataset.idleLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Importing…";
  });

  // Restore the button when the page is shown from the back/forward cache, so
  // returning to a submitted form never leaves a permanently disabled button.
  window.addEventListener("pageshow", function () {
    var btn = button();
    if (btn && btn.disabled && btn.dataset.idleLabel) {
      btn.disabled = false;
      btn.textContent = btn.dataset.idleLabel;
    }
  });
})();
