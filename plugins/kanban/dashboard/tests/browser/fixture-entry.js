// Browser-fixture entry for the kanban dashboard IIFE.
//
// Bundled with esbuild into a single browser script. Provides a minimal
// __HERMES_PLUGIN_SDK__ shim (React + primitives + fetch helpers) and a
// __HERMES_PLUGINS__.register shim, then requires the real IIFE (which reads
// those globals and registers the KanbanPage component), then renders it.
//
// fetchJSON/authedFetch use the real fetch() so the IIFE exercises its own
// network paths against the fixture server (network boundary mocking only,
// never source-grep). buildWsUrl resolves to undefined so the WebSocket
// constructor throws synchronously and is swallowed by the IIFE's try/catch.
const React = require("react");
const ReactDOMClient = require("react-dom/client");
const { useState, useEffect, useCallback, useMemo, useRef } = React;

function cn() {
  return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
}
function timeAgo(ts) {
  if (!ts) return "";
  const d = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (d < 60) return d + "s ago";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}

function Card(props) { return React.createElement("div", props); }
function CardContent(props) { return React.createElement("div", props); }
function Badge(props) { return React.createElement("span", props); }
function Button(props) { return React.createElement("button", props); }
function Input(props) { return React.createElement("input", props); }
function Label(props) { return React.createElement("label", props); }
function Select(props) {
  const rest = Object.assign({}, props);
  delete rest.onValueChange; delete rest.onChange; delete rest.children; delete rest.value;
  return React.createElement("select", Object.assign({}, rest, {
    value: props.value == null ? "" : String(props.value),
    onChange: function (e) {
      if (props.onChange) props.onChange(e);
      if (props.onValueChange) props.onValueChange(e.target.value);
    },
  }), props.children);
}
function SelectOption(props) { return React.createElement("option", { value: props.value }, props.children); }

function fetchJSON(url, opts) {
  return fetch(url, opts).then(function (resp) {
    return resp.text().then(function (txt) {
      if (!resp.ok) throw new Error(resp.status + ": " + txt);
      return txt ? JSON.parse(txt) : null;
    });
  });
}
function authedFetch(url, opts) { return fetch(url, opts); }
function buildWsUrl() { return Promise.resolve(undefined); }

window.__HERMES_PLUGIN_SDK__ = {
  React: React,
  components: { Card: Card, CardContent: CardContent, Badge: Badge, Button: Button, Input: Input, Label: Label, Select: Select, SelectOption: SelectOption },
  hooks: { useState: useState, useEffect: useEffect, useCallback: useCallback, useMemo: useMemo, useRef: useRef },
  utils: { cn: cn, timeAgo: timeAgo },
  fetchJSON: fetchJSON,
  authedFetch: authedFetch,
  buildWsUrl: buildWsUrl,
  useI18n: undefined,
};

window.__HERMES_PLUGINS__ = {
  register: function (name, comp) { window.__KANBAN_COMP__ = comp; },
};

require("../../dist/index.js");

const comp = window.__KANBAN_COMP__;
const root = document.getElementById("root");
if (comp && root) {
  window.__REACT_DOM_CLIENT__ = ReactDOMClient;
  ReactDOMClient.createRoot(root).render(React.createElement(comp));
  window.__RENDERED__ = true;
}
