const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(process.env.LAND_TRACKER_HTML || path.resolve(__dirname, "../../land_tracker_dashboard_20260614/index.html"), "utf8");
const elements = new Map();
const listeners = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { dataset: {}, value: "", setAttribute() {}, replaceChildren() {}, addEventListener(type, fn) { listeners.set(id + ":" + type, fn); } });
  return elements.get(id);
}
const messages = [];
const window = { location: { href: "https://verachen1989.github.io/chatbi2.0/land_tracker_dashboard_20260614/", origin: "https://verachen1989.github.io" }, addEventListener() {}, open() { throw new Error("Unexpected new tab"); }, postMessage() { throw new Error("Unexpected message to own window"); }, setTimeout() { throw new Error("Unexpected delayed redirect"); } };
window.parent = window;
const context = vm.createContext({ URL, Intl, Date, window, document: { referrer: "", getElementById: element, addEventListener() {} }, Option: function() {} });
for (const match of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) vm.runInContext(match[1], context);
function render(row) {
  context.fixture = row;
  vm.runInContext("LAND_ROWS.splice(0, LAND_ROWS.length, fixture); render();", context);
  return element("tableBody").innerHTML;
}
const actualRows = JSON.parse(vm.runInContext("JSON.stringify(LAND_ROWS)", context));
if (process.env.EXPECT_XINGCHEN === '1') {
  const target = actualRows.filter(row => row.landName.includes('DX00-0201-0209'));
  assert.equal(target.length, 1);
  assert.deepEqual(target[0].linkedProjects, [{projectName: '招商兴宸揽阅', projectCode: ''}]);
  const targetMarkup = render(target[0]);
  assert.match(targetMarkup, /招商兴宸揽阅/);
  assert.doesNotMatch(targetMarkup, /class="project-link"/);
}
window.parent = { postMessage(payload, origin) { messages.push({ payload, origin }); } };
context.document.referrer = "https://om.gtcloud.cn/";
let actualCodes = 0;
for (const actual of actualRows) {
  const coded = (actual.linkedProjects || []).filter(project => project.projectCode);
  if (!coded.length) continue;
  const markup = render(actual);
  assert.equal((markup.match(/<button class="project-link" type="button"/g) || []).length, coded.length);
  assert.doesNotMatch(markup, /<a[^>]*class="project-link"/);
  actual.linkedProjects.forEach((project, index) => {
    if (!project.projectCode) return;
    const href = "https://om.gtcloud.cn/#/region/invest/external-data?projectCode=" + encodeURIComponent(project.projectCode);
    assert.ok(!markup.includes('href="' + href + '"'));
    let prevented = false;
    listeners.get("tableBody:click")({ button: 0, preventDefault() { prevented = true; },
      target: { closest(selector) { return selector === "[data-project-seq]" ? { dataset: { projectSeq: String(actual.seq), projectIndex: String(index) } } : null; } } });
    const message = messages.at(-1);
    assert.equal(message.origin, "*");
    assert.equal(message.payload.type, "chatbi2:open-project-detail");
    assert.equal(message.payload.projectCode, project.projectCode);
    assert.equal(message.payload.url, href);
    assert.ok(prevented);
    actualCodes += 1;
  });
}
if (process.env.EXPECTED_PROJECT_LINKS) assert.equal(actualCodes, Number(process.env.EXPECTED_PROJECT_LINKS));
console.log("Actual project link payloads checked:", actualCodes);
messages.length = 0;
window.parent = window;
context.document.referrer = "";
const row = { seq: 1, landName: "测试地块", landCode: "京土储挂（朝）[2025]001号", projectName: "旧名称 / 别名", dealDate: "25/01/01", linkedProjects: [
  { projectName: "测试项目甲", projectCode: "P3852" },
  { projectName: "测试项目乙", projectCode: "P9002" },
  { projectName: "尚无编码项目", projectCode: "" }
] };
let markup = render(row);
assert.equal((markup.match(/<button class="project-link" type="button"/g) || []).length, 2, "Each coded project needs its own message-only button");
const projectMarkup = markup.match(/<ul class="project-list">[\s\S]*?<\/ul>/)[0];
assert.doesNotMatch(projectMarkup, /<a\b|\bhref=|\btarget=|\bformaction=/, "Project actions must never navigate or submit a form");
assert.match(markup, /<span class="project-name"[^>]*>尚无编码项目<\/span>/);
assert.doesNotMatch(markup, /旧名称 \/ 别名/);
assert.equal(context.projectDashboardUrl({ projectName: "只有名称" }), "");
assert.equal(context.projectDashboardUrl({ projectCode: "x&projectCode=P3852" }), "");
assert.doesNotMatch(render({ ...row, linkedProjects: [], projectCode: "" }), /class="project-link"/);
assert.match(render({ ...row, linkedProjects: [], projectCode: "" }), /旧名称 \/ 别名/);
assert.match(render({ ...row, linkedProjects: [{ projectName: '<img src=x onerror="alert(1)">', projectCode: "P9002" }] }), /&lt;img/);
assert.doesNotMatch(element("tableBody").innerHTML, /<img/);

render(row);
vm.runInContext('state.search = "P9002"; render();', context);
assert.equal(element("tableCount").textContent, "1 条");
vm.runInContext('state.search = ""; state.node = "missing-project"; render();', context);
assert.equal(element("tableCount").textContent, "0 条");
vm.runInContext('state.node = "";', context);

// Selecting the second button must send the second project, not the first match.
window.parent = { postMessage(payload, origin) { messages.push({ payload, origin }); } };
context.document.referrer = "https://om.gtcloud.cn/";
let prevented = 0;
const button = { dataset: { projectSeq: "1", projectIndex: "1" } };
const event = { button: 0, preventDefault() { prevented += 1; }, target: { closest(selector) { return selector === "[data-project-seq]" ? button : null; } } };
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 1);
assert.equal(messages[0].origin, "*");
assert.equal(messages[0].payload.projectCode, "P9002");
assert.equal(messages[0].payload.url, "https://om.gtcloud.cn/#/region/invest/external-data?projectCode=P9002");
assert.equal(prevented, 1);
for (const modifier of ["ctrlKey", "metaKey", "shiftKey", "altKey"]) {
  const count = messages.length;
  listeners.get("tableBody:click")({ ...event, [modifier]: true });
  assert.equal(messages.length, count + 1, "Modified activation must only send a message");
  assert.equal(messages.at(-1).payload.projectCode, "P9002");
}
assert.equal(prevented, 5);
listeners.get("tableBody:click")({ ...event, button: 1 });
assert.equal(messages.length, 5, "Non-primary mouse buttons must not activate a project");
context.document.referrer = "https://untrusted.example/";
const beforeUntrusted = prevented;
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 5, "Do not send project data to an unrelated parent");
assert.equal(prevented, beforeUntrusted + 1, "Untrusted parents must not fall back to navigation");
window.location.ancestorOrigins = ["https://om.gtcloud.cn"];
context.document.referrer = "";
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 6, "A known ancestor must work without a referrer");
window.location.ancestorOrigins = ["https://untrusted.example"];
context.document.referrer = "https://om.gtcloud.cn/";
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 6, "The immediate ancestor takes precedence over the referrer");
delete window.location.ancestorOrigins;
window.parent = window;
const beforeStandalone = prevented;
listeners.get("tableBody:click")(event);
assert.equal(messages.length, 6);
assert.equal(prevented, beforeStandalone + 1, "Standalone pages must not fall back to navigation");
assert.equal(window.location.href, "https://verachen1989.github.io/chatbi2.0/land_tracker_dashboard_20260614/");
console.log("Land project links: passed");
