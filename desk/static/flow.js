// The overview's system map: one lost payment, played as a loop. Every frame is a pure function of the time
// into the loop (frame(t)), so the same moment always looks the same; motion uses a damped spring, not a fixed ease.
(function () {
  var root = document.getElementById("flow");
  if (!root) return;
  var D = 13.5;
  var $ = function (id) { return root.querySelector("#" + id); };
  var spring = function (p) {
    if (p <= 0) return 0;
    if (p >= 1) return 1;
    return 1 - Math.exp(-7 * p) * (Math.cos(5 * p) + 1.4 * Math.sin(5 * p));
  };
  var phase = function (t, a, b) { return Math.max(0, Math.min(1, (t - a) / (b - a))); };
  var links = {
    tg: [[208, 240], [304, 240]],
    gf: [[504, 200], [584, 124]],
    gl: [[504, 280], [584, 356]]
  };
  var along = function (link, p) {
    var a = links[link][0], b = links[link][1];
    return [a[0] + (b[0] - a[0]) * p, a[1] + (b[1] - a[1]) * p];
  };
  var place = function (id, link, p, reverse, visible) {
    var el = $(id), q = reverse ? 1 - p : p, xy = along(link, q);
    el.setAttribute("cx", xy[0]);
    el.setAttribute("cy", xy[1]);
    el.style.opacity = visible ? 1 : 0;
  };
  var text = function (id, value) { var el = $(id); if (el.textContent !== value) el.textContent = value; };
  var on = function (id, cls, yes) { $(id).classList.toggle(cls, !!yes); };
  var steps = [
    [0, "The tenant says yes to $240.00 on the phone."],
    [1.9, "Gatehouse writes the payment down before it calls the FMS."],
    [2.3, "It sends the payment once."],
    [3.3, "The FMS charges the card and applies it."],
    [3.9, "The answer breaks on the way back."],
    [4.7, "Outcome unknown: Gatehouse holds the account and does not retry."],
    [6.5, "A collections lead names the FMS payment that is this one."],
    [9.1, "Gatehouse checks the FMS ledger before it believes anyone."],
    [10.9, "Confirmed by the ledger. Only now is the tenant told it is applied."]
  ];
  var caption = $("flow-caption");

  function frame(t) {
    // tenant to Gatehouse
    place("p-tg", "tg", spring(phase(t, 1.0, 1.8)), false, t > 1.0 && t < 1.9);
    on("n-tenant", "lit", t < 1.9 || t > 10.9);
    text("tenant-sub", t < 10.9 ? "Says yes to $240.00" : "Told: payment applied");
    // Gatehouse to the FMS, and the answer that breaks halfway back
    place("p-gf", "gf", spring(phase(t, 2.3, 3.2)), false, t > 2.3 && t < 3.3);
    place("p-fg", "gf", 0.5 * spring(phase(t, 3.4, 3.9)), true, t > 3.4 && t < 4.6);
    on("l-gf", "broken", t > 3.9 && t < 9.1);
    on("x-break", "show", t > 3.9 && t < 9.1);
    on("n-fms", "lit", (t > 3.2 && t < 4.0) || (t > 9.6 && t < 10.9));
    text("fms-sub", t < 3.3 ? "Owed $240.00" : "Owed $0.00");
    // the retry that does not happen
    var retry = phase(t, 5.2, 6.2);
    place("p-retry", "gf", 0.18 * spring(retry), false, t > 5.2 && t < 6.3);
    on("x-retry", "show", t > 5.6 && t < 6.6);
    // the person, then the ledger check
    place("p-gl", "gl", spring(phase(t, 6.5, 7.4)), false, t > 6.5 && t < 7.5);
    on("n-lead", "lit", t > 7.3 && t < 9.2);
    text("lead-sub", t < 7.4 ? "Waiting" : "Names the payment");
    place("p-lg", "gl", spring(phase(t, 8.1, 9.0)), true, t > 8.1 && t < 9.1);
    place("p-chk", "gf", spring(phase(t, 9.1, 9.9)), false, t > 9.1 && t < 10.0);
    place("p-chk2", "gf", spring(phase(t, 10.0, 10.8)), true, t > 10.0 && t < 10.9);
    // Gatehouse's own state
    var state = t < 1.9 ? ["Idle", ""] : t < 2.3 ? ["Intent saved", ""] : t < 3.9 ? ["Sent once", ""]
      : t < 4.7 ? ["No usable answer", "act"] : t < 10.9 ? ["Outcome unknown: held", "act"] : ["Confirmed by the ledger", "done"];
    text("gh-state", state[0]);
    $("gh-chip").setAttribute("class", "chip-svg " + state[1]);
    on("n-gh", "lit", t > 1.8);
    var current = steps[0][1];
    for (var i = 0; i < steps.length; i++) if (t >= steps[i][0]) current = steps[i][1];
    if (caption.textContent !== current) caption.textContent = current;
  }

  var still = window.matchMedia("(prefers-reduced-motion: reduce)");
  if (still.matches) { frame(12); return; }
  var start = null, visible = true;
  new IntersectionObserver(function (e) { visible = e[0].isIntersecting; }).observe(root);
  function tick(now) {
    if (start === null) start = now;
    if (visible && !document.hidden) frame(((now - start) / 1000) % D);
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();
