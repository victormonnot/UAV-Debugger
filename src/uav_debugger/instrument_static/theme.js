(() => {
  "use strict";
  const allowed = ["system", "light", "dark"];
  let preference = "system";
  try {
    const saved = localStorage.getItem("uav-debugger.appearance");
    if (allowed.includes(saved)) preference = saved;
  } catch { /* Appearance remains usable when browser storage is unavailable. */ }
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = preference === "system" ? (dark ? "dark" : "light") : preference;
  document.documentElement.dataset.appearance = preference;
})();
