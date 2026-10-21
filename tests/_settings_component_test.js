const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

async function main() {
  const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  const source = scripts.at(-1)?.[1];
  assert.ok(source, "expected the inline management UI script");

  const components = new Map();
  const mounted = [];
  const unmounted = [];
  const requests = [];
  const local = new Map();
  const preview = {
    config: {mode: "expiry_first"},
    selected_account_id: 9,
    accounts: [{account_id: 9, reason: "expiry_candidate", expire_ts: 2_000_000_000}],
  };

  const Vue = {
    createApp() {
      const app = {
        component(name, options) { components.set(name, options); return app; },
        mount() { return app; },
      };
      return app;
    },
    ref(value) { return {value}; },
    reactive(value) { return value; },
    computed(getter) { return {get value() { return getter(); }}; },
    onMounted(callback) { mounted.push(callback); },
    onUnmounted(callback) { unmounted.push(callback); },
  };

  const document = {
    documentElement: {dataset: {}},
    visibilityState: "visible",
    addEventListener() {},
    removeEventListener() {},
  };
  const window = {addEventListener() {}, removeEventListener() {}, location: {reload() {}}};
  const localStorage = {
    getItem(key) { return local.get(key) ?? null; },
    setItem(key, value) { local.set(key, String(value)); },
    removeItem(key) { local.delete(key); },
  };
  const fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({url, method, body});
    let value = {};
    if (url === "/admin/settings") {
      value = {
        backend_url: "https://settings.mock",
        loomy_context_budget: 222222,
        workbuddy_routing: {
          mode: "expiry_first",
          expiry_window_days: 8,
          cache_max_age_seconds: 240,
          refresh_interval_seconds: 90,
        },
      };
    } else if (url === "/admin/stats" || url === "/admin/codex/status") {
      value = {};
    } else if (url === "/admin/api-keys") {
      value = [];
    } else if (url === "/admin/routing/preview") {
      value = preview;
    }
    return {ok: true, async json() { return value; }};
  };

  vm.runInNewContext(source, {
    Vue, document, window, localStorage, fetch,
    navigator: {clipboard: {writeText: async () => {}}},
    console,
    setTimeout() { return 1; },
    clearTimeout() {},
  }, {filename: "web/index.html"});

  const settingsComponent = components.get("stgs");
  assert.ok(settingsComponent, "settings component should register");
  const toasts = [];
  const props = {
    token: "synthetic-token",
    toast(message) { toasts.push(message); },
    saveToken() {},
    ver: null,
    updating: false,
    checkVer() {},
    applyUpdate() {},
  };
  const accountsComponent = components.get("accs");
  assert.ok(accountsComponent, "accounts component should register");
  const accounts = accountsComponent.setup(props);
  for (const binding of ["l", "discRows", "load", "saveMeta", "channels", "discover", "closeRowMenu"]) {
    assert.ok(binding in accounts, `accounts component should expose ${binding}`);
  }
  const settings = settingsComponent.setup(props);
  for (const binding of ["s", "load", "save", "resetDefaults", "previewRouting", "routePreview", "routeBusy", "routeReasons", "routingTime"]) {
    assert.ok(binding in settings, `settings component should expose ${binding}`);
  }
  assert.equal(mounted.length, 2, "accounts and settings components should register their load hooks");
  await mounted.at(-1)();
  assert.equal(settings.s.value.loomy_context_budget, 222222);
  assert.equal(settings.s.value.workbuddy_routing.mode, "expiry_first");
  assert.equal(settings.s.value.workbuddy_routing.refresh_interval_seconds, 90);

  await settings.previewRouting();
  assert.equal(settings.routePreview.value.selected_account_id, 9);
  assert.equal(requests.filter(item => item.url === "/admin/routing/preview").length, 1);

  await settings.save();
  const save = requests.filter(item => item.url === "/admin/settings" && item.method === "PUT").at(-1);
  assert.deepEqual(save.body.workbuddy_routing, settings.s.value.workbuddy_routing);
  assert.equal(save.body.loomy_context_budget, 222222);
  assert.equal(settings.routePreview.value, null);

  settings.resetDefaults();
  assert.equal(settings.s.value.workbuddy_routing.mode, "legacy");
  assert.equal(settings.s.value.loomy_context_budget, 160000);
  assert.ok(toasts.some(message => message.includes("默认")));
  for (const cleanup of unmounted) cleanup();
  process.stdout.write("settings component load/save/reset/preview mock passed\n");
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
