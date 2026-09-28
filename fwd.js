// Public host forwarder (tools.myinthermo.com). The tools live on the shop server,
// reachable only on Tailscale. Probe it; forward there (same path) if reachable,
// otherwise send the visitor to the public website.
(function () {
  var TAILNET = 'https://inthermopc.tail881969.ts.net';
  var PUBLIC = 'https://myinthermo.com/';
  var done = false;
  function go(u) { if (!done) { done = true; location.replace(u); } }
  var ac = window.AbortController ? new AbortController() : null;
  setTimeout(function () { if (ac) ac.abort(); go(PUBLIC); }, 3000);
  fetch(TAILNET + '/healthz', { mode: 'no-cors', cache: 'no-store', signal: ac && ac.signal })
    .then(function () { go(TAILNET + location.pathname + location.search + location.hash); })
    .catch(function () { go(PUBLIC); });
})();
