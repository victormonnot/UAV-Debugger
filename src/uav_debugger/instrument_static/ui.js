(() => {
  "use strict";

  function retainFocus() {
    const origin = document.activeElement;
    let moved = !origin || origin === document.body || origin === document.documentElement;
    const onFocus = event => { if (event.target !== origin && event.target !== document.body) moved = true; };
    const onPointer = event => { if (!origin?.contains(event.target)) moved = true; };
    document.addEventListener("focusin", onFocus);
    document.addEventListener("pointerdown", onPointer, true);
    return ({ target = origin, restore = true } = {}) => {
      document.removeEventListener("focusin", onFocus);
      document.removeEventListener("pointerdown", onPointer, true);
      // Disabling a focused control can blur it to body; never undo a deliberate move.
      if (!restore || moved || !target?.isConnected || target.matches(":disabled") || !target.getClientRects().length) return;
      if (document.activeElement !== document.body && document.activeElement !== origin) return;
      target.focus({ preventScroll: true });
    };
  }
  globalThis.InstrumentUI = { retainFocus };
})();
