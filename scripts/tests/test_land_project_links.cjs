const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.resolve(__dirname, "../../land_tracker_dashboard_20260614/index.html"), "utf8");
const elements = new Map();
const listeners = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { dataset: {}, value: "", setAttribute() {}, replaceChildren() {}, addEventListener(type, fn) { listeners.set(id + ":" + type, fn); } });
  return elements.get(id);
}
const messages = [];
const window = { location: { href: "https://verachen1989.github.io/chatbi2.0/land_tracker_dashboard_20260614/", origin: "https://verachen1989.github.io" }, addEventListener() {}, setTimeout() { throw new Error("Unexpected delayed redirect"); } };
window.parent = window;
const context = vm.createContext({ URL, Intl, Date, window, document: { referrer: "", getElementById: element, addEventListener() {} }, Option: function() {} });
for (const match of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) vm.runInContext(match[1], context);
function render(row) {
  context.fixture = row;
  vm.runInContext("LAND_ROWS.splice(0, LAND_ROWS.length, fixture); render();", context);
  return element("tableBody").innerHTML;
}
const row = { seq: 1, landName: "测试地块", landCode: "京土储挂（朝）[2025]001号", projectName: "旧名称 / 别名", dealDate: "25/01/01", linkedProjects: [
  { projectName: "测试项目甲", projectCode: "P3852" },
  { projectName: "测试项目乙", projectCode: "P9002" },
  { projectName: "尚无编码项目", projectCode: "" }
] };
let markup = render(row);
assert.equal((markup.match(/<a class="project-link"/g) || []).length, 2, "Each coded project needs its own actual anchor");
assert.match(markup, /href="https:\/\/om.gtcloud.cn\/#\/region\/invest\/external-data\?projectCode=P3852"/);
assert.match(markup, /projectCode=P9002/);
assert.match(markup, /<span class="project-name"[^>]*>尚无编码项目<\/span>/);
assert.doesNotMatch(markup, /旧名称 \/ 别名/);
assert.equal(context.projectDashboardUrl({ projectName: "只有名称" }), "");
assert.equal(context.projectDashboardUrl({ projectCode: "x&projectCode=P3852" }), "");
assert.doesNotMatch(render({ ...row, linkedProjects: [], projectCode: "" }), /<a class="project-link"/);
assert.match(render({ ...row, linkedProjects: [], projectCode: "" }), /旧名称 \/ 别名/);
assert.match(render({ ...row, linkedProjects: [{ projectName: '<img src=x onerror="alert(1)">', projectCode: "P9002" }] }), /&lt;img/);
assert.doesNotMatch(element("tableBody").innerHTML, /<img/);

render(row);
vm.runInContext('state.search = "P9002"; render();', context);
assert.equal(element("tableCount").textContent, "1 条");
vm.runInContext('state.search = ""; state.node = "missing-project"; render();', context);
assert.equal(element("tableCount").textContent, "0 条");
vm.runInContext('state.node = "";', context);

// Selecting the second anchor must send the second project, not the first match.
window.parent = { postMessage(payload, origin) { messages.push({ payload, origin }); } };
context.document.referrer = "https://om.gtcloud.cn/";
let prevented = 0;
const anchor = { dataset: { projectSeq: "1", projectIndex: "1" } };
const event = { button: 0, preventDefault() { prevented += 1; }, target: { closest(selector) { return selector === "[data-project-seq]" ? anchor : null; } } };
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 1);
assert.equal(messages[0].origin, "https://om.gtcloud.cn");
assert.equal(messages[0].payload.projectCode, "P9002");
assert.equal(messages[0].payload.url, "https://om.gtcloud.cn/#/region/invest/external-data?projectCode=P9002");
assert.equal(prevented, 1);
listeners.get("tableBody:click")({ ...event, ctrlKey: true });
assert.equal(messages.length, 1, "Modified click must keep the native anchor behavior");
context.document.referrer = "https://untrusted.example/";
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 1, "Do not send project data to an unrelated parent");
assert.equal(prevented, 1);
console.log("Land project links: passed");
