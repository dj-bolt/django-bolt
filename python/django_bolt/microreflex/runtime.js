/* micro-reflex client runtime — no framework, no build step.
 *
 * Responsibilities:
 *  1. Keep a WebSocket open to the server (with reconnect + resync).
 *  2. Delegate DOM events: elements carry data-rx-on='[["click",{spec}],...]';
 *     matching events are posted to the server verbatim.
 *  3. Apply server patches to slot markers:
 *       {k:"t",i,v} -> [data-rx-t=i].textContent = v
 *       {k:"h",i,v} -> [data-rx-h=i].innerHTML  = v
 *       {k:"a",e,a,v} -> set attribute/property on [data-rx-e=e]
 */
(function () {
  "use strict";
  var PAGE = window.__RX_PAGE || location.pathname;
  var WS_PATH = window.__RX_WS || "/_rx/ws";
  var ws = null;
  var isOpen = false;
  var everConnected = false;
  var queue = [];
  var reconnectDelay = 500;

  function connect() {
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + WS_PATH + "?page=" + encodeURIComponent(PAGE));
    ws.onopen = function () {
      isOpen = true;
      reconnectDelay = 500;
      if (everConnected) {
        // Server session was lost; ask for a full slot resync.
        ws.send(JSON.stringify({ t: "sync" }));
      }
      everConnected = true;
      while (queue.length) ws.send(queue.shift());
    };
    ws.onclose = function () {
      isOpen = false;
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 5000);
    };
    ws.onmessage = function (e) {
      var msg = JSON.parse(e.data);
      if (msg.t === "p") applyPatches(msg.p);
      else if (msg.t === "err") console.error("[microreflex] server error:", msg.m);
    };
  }

  function send(obj) {
    var data = JSON.stringify(obj);
    if (isOpen) ws.send(data);
    else queue.push(data);
  }

  function applyPatches(patches) {
    for (var i = 0; i < patches.length; i++) {
      var p = patches[i];
      var el;
      if (p.k === "t") {
        el = document.querySelector('[data-rx-t="' + p.i + '"]');
        if (el) el.textContent = p.v;
      } else if (p.k === "h") {
        el = document.querySelector('[data-rx-h="' + p.i + '"]');
        if (el) el.innerHTML = p.v;
      } else if (p.k === "a") {
        el = document.querySelector('[data-rx-e="' + p.e + '"]');
        if (el) setAttr(el, p.a, p.v);
      }
    }
  }

  function setAttr(el, name, value) {
    if (name === "value") {
      // Never stomp the control the user is typing in.
      if (el !== document.activeElement) el.value = value == null ? "" : value;
      return;
    }
    if (name === "checked") {
      el.checked = !!value;
      return;
    }
    if (value === false || value === null || value === undefined) {
      el.removeAttribute(name);
    } else if (value === true) {
      el.setAttribute(name, "");
    } else {
      el.setAttribute(name, value);
    }
  }

  // Events that do not bubble: match only on the exact target.
  var NON_BUBBLING = { blur: 1, focus: 1, mouseenter: 1, mouseleave: 1 };

  function findSpec(target, type) {
    var exactOnly = NON_BUBBLING[type];
    for (var el = target; el && el.getAttribute; el = el.parentElement) {
      var raw = el.getAttribute("data-rx-on");
      if (raw) {
        var entries = JSON.parse(raw);
        for (var i = 0; i < entries.length; i++) {
          if (entries[i][0] === type) return { el: el, spec: entries[i][1] };
        }
      }
      if (exactOnly) return null;
    }
    return null;
  }

  function eventValue(type, el, e) {
    if (type === "input" || type === "change") {
      if (el.type === "checkbox" || el.type === "radio") return el.checked;
      return el.value;
    }
    if (type === "keydown" || type === "keyup") return e.key;
    if (type === "submit") {
      var data = {};
      new FormData(el).forEach(function (v, k) {
        data[k] = v;
      });
      return data;
    }
    return undefined;
  }

  function fire(type, e) {
    var hit = findSpec(e.target, type);
    if (!hit) return;
    if (type === "submit") e.preventDefault();
    if (type === "click" && hit.el.tagName === "A") e.preventDefault();
    var spec = hit.spec;
    var msg = { t: "e", h: { s: spec.s, n: spec.n } };
    if (spec.a) msg.h.a = spec.a;
    var v = eventValue(type, hit.el, e);
    if (v !== undefined) msg.v = v;
    if (spec.o && spec.o.d) {
      clearTimeout(hit.el._rxDebounce);
      hit.el._rxDebounce = setTimeout(function () {
        send(msg);
      }, 30);
    } else {
      send(msg);
    }
  }

  var TYPES = [
    "click",
    "dblclick",
    "input",
    "change",
    "blur",
    "focus",
    "submit",
    "keydown",
    "keyup",
    "mouseenter",
    "mouseleave",
  ];
  for (var i = 0; i < TYPES.length; i++) {
    (function (type) {
      document.addEventListener(
        type,
        function (e) {
          fire(type, e);
        },
        true
      );
    })(TYPES[i]);
  }

  connect();
})();
