// Public host forwarder (tools.myinthermo.com). The tools live on the shop server,
// reachable only on Tailscale. Show a "shop tools" button + 20 s countdown to the
// public website. A background probe forwards immediately where the browser allows it
// (Chrome's Local Network Access blocks public→100.x fetches, so the button is the main path).
(function () {
  var TAILNET = 'https://inthermopc.tail881969.ts.net';
  var PUBLIC = 'https://myinthermo.com/';
  var target = TAILNET + location.pathname + location.search + location.hash;
  var done = false, left = 20;
  function go(u) { if (!done) { done = true; location.replace(u); } }
  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('tools'), cd = document.getElementById('cd');
    btn.href = target;
    btn.addEventListener('click', function () { done = true; });
    cd.textContent = left;
    var t = setInterval(function () {
      if (done) return clearInterval(t);
      left--; cd.textContent = left;
      if (left <= 0) { clearInterval(t); go(PUBLIC); }
    }, 1000);
  });
  try {
    fetch(TAILNET + '/healthz', { mode: 'no-cors', cache: 'no-store' })
      .then(function () { go(target); }).catch(function () {});
  } catch (e) {}
})();
