// HA Ingress path-rewriting shim for the Omnigent React SPA.
// Injected by nginx sub_filter into <head> so the bundle transparently
// dials back through /api/hassio_ingress/<slug>/ instead of the naked /.
//
// The ingress path is templated into `window.__INGRESS_PATH__` by the
// server block; if it isn't set we're being served over the LAN
// (bypassed the Ingress proxy) and no rewriting is needed.
(function () {
  var p = window.__INGRESS_PATH__;
  if (!p) return;

  var f = window.fetch;
  window.fetch = function (i, init) {
    if (typeof i === "string" && i.charAt(0) === "/" && i.indexOf(p) !== 0 && i.indexOf("//") !== 0) {
      i = p + i;
    }
    return f.call(this, i, init);
  };

  var XO = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    if (typeof u === "string" && u.charAt(0) === "/" && u.indexOf(p) !== 0 && u.indexOf("//") !== 0) {
      u = p + u;
    }
    return XO.apply(this, [m, u].concat([].slice.call(arguments, 2)));
  };

  var HP = history.pushState, HR = history.replaceState;
  history.pushState = function (s, t, u) {
    if (typeof u === "string" && u.charAt(0) === "/" && u.indexOf(p) !== 0) u = p + u;
    return HP.apply(this, [s, t, u]);
  };
  history.replaceState = function (s, t, u) {
    if (typeof u === "string" && u.charAt(0) === "/" && u.indexOf(p) !== 0) u = p + u;
    return HR.apply(this, [s, t, u]);
  };

  var W = window.WebSocket;
  window.WebSocket = function (u, pr) {
    if (typeof u === "string") {
      var m = u.match(/^(wss?:\/\/[^\/]+)(\/.+)/);
      if (m && m[2].indexOf(p) !== 0) u = m[1] + p + m[2];
    }
    return pr ? new W(u, pr) : new W(u);
  };

  // window.location.assign / replace / href are how the SPA does
  // programmatic navigation (e.g. `window.location = "/login"`). The
  // browser resolves those against origin, so without prefixing, an
  // unauthenticated redirect to `/login` escapes the ingress frame and
  // lands on HA's own root (404). Wrap them so root-absolute paths get
  // the ingress prefix.
  function pfx(u) {
    if (typeof u === "string" && u.charAt(0) === "/" && u.indexOf(p) !== 0 && u.indexOf("//") !== 0) {
      return p + u;
    }
    return u;
  }
  var LA = Location.prototype.assign;
  Location.prototype.assign = function (u) { return LA.call(this, pfx(u)); };
  var LR = Location.prototype.replace;
  Location.prototype.replace = function (u) { return LR.call(this, pfx(u)); };
  // `window.location = "/foo"` invokes the Location toString + href setter.
  try {
    var d = Object.getOwnPropertyDescriptor(Location.prototype, "href");
    if (d && d.set) {
      Object.defineProperty(Location.prototype, "href", {
        configurable: true,
        enumerable: true,
        get: d.get,
        set: function (u) { return d.set.call(this, pfx(u)); }
      });
    }
  } catch (e) { /* some browsers freeze Location — best-effort */ }
})();
