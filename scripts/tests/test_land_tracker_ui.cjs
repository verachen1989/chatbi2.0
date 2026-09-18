const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const page = process.argv[2] || path.resolve(__dirname, "../../land_tracker_dashboard_20260614/index.html");
const source = fs.readFileSync(page, "utf8");
for (const match of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  new vm.Script(match[1]);
}
function section(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `Missing functions: ${start}`);
  return source.slice(from, to);
}
const context = vm.createContext({});
vm.runInContext(section("function html(", "function num(") + section("function isPlanningSource(", "function render("), context);
const row = { planningPermit: "26/02/06", fieldEvidence: {} };
assert.equal(context.evidenceDisplay(row, "planningPermit"), row.planningPermit);
assert.match(context.evidenceDisplay({}, "planningPermit"), /class="empty"/);
row.fieldEvidence.planningPermit = { status: "confirmed", url: "https://example.com/?a=1&b=2", permit: '"<permit>' };
let markup = context.evidenceDisplay(row, "planningPermit");
assert.match(markup, /class="evidence-link"/);
assert.match(markup, /a=1&amp;b=2/);
assert.match(markup, /&quot;&lt;permit&gt;/);
assert.match(markup, /rel="noopener noreferrer"/);
row.fieldEvidence.planningPermit.url = "javascript:alert(1)";
assert.doesNotMatch(context.evidenceDisplay(row, "planningPermit"), /<a /);
row.fieldEvidence.planningPermit = { status: "conflict", candidate: '"<script>' };
markup = context.evidenceDisplay(row, "planningPermit");
assert.match(markup, /class="review-flag"/);
assert.doesNotMatch(markup, /<script>/);
assert.doesNotMatch(markup, /<a /);
row.fieldEvidence.planningPermit.url = "https://example.com/official";
assert.match(context.evidenceDisplay(row, "planningPermit"), /<a class="review-flag"/);
row.fieldEvidence.planningPermit.status = "not_reconfirmed";
assert.doesNotMatch(context.evidenceDisplay(row, "planningPermit"), /class="evidence-link"/);
row.seq = 8;
row.landName = "良乡大学城地块";
const planningUrl = "https://yewu.ghzrzyw.beijing.gov.cn/zkdncms/cxghspjggsszjsjsgcghxkz/esSearchDetail/07599f3f10644d5b5d30f19f7a33182b";
row.fieldEvidence.planningPermit = { status: "confirmed", url: planningUrl, candidate: "26/09/03", permit: "2026规自（房）建字0027号", record: { name: "住宅楼", developer: '"<script>', date: "26/09/03" } };
markup = context.evidenceDisplay(row, "planningPermit");
assert.match(markup, /<button class="evidence-link"/);
assert.match(markup, /data-evidence-seq="8"/);
assert.doesNotMatch(markup, /href=|esSearchDetail/);
let details = context.planningEvidenceContent(row, row.fieldEvidence.planningPermit);
assert.match(details, /住宅楼/);
assert.match(details, /&quot;&lt;script&gt;/);
assert.match(details, /26\/09\/03/);
assert.doesNotMatch(details, /<script>/);
row.fieldEvidence.planningPermit.status = "conflict";
assert.match(context.evidenceDisplay(row, "planningPermit"), /<button class="review-flag"/);
assert.match(context.planningEvidenceContent(row, row.fieldEvidence.planningPermit), /表内原值 26\/02\/06；官方候选 26\/09\/03/);
row.fieldEvidence.planningPermit.status = "not_reconfirmed";
assert.doesNotMatch(context.evidenceDisplay(row, "planningPermit"), /data-evidence-seq/);
assert.equal(context.isPlanningSource(planningUrl.replace("yewu.ghzrzyw.beijing.gov.cn", "example.com")), false);
assert.equal(context.isPlanningSource("javascript:alert(1)"), false);
let opened = 0;
const elements = {
  evidenceContent: {}, evidenceRaw: {},
  evidenceDialog: { open: false, showModal() { this.open = true; opened += 1; } }
};
context.document = { getElementById: id => elements[id] };
row.fieldEvidence.planningPermit.status = "confirmed";
context.openPlanningEvidence(row);
assert.equal(opened, 1);
assert.equal(elements.evidenceRaw.href, planningUrl);
assert.match(elements.evidenceContent.innerHTML, /住宅楼/);
context.openPlanningEvidence(row);
assert.equal(opened, 1);
elements.evidenceDialog.open = false;
row.fieldEvidence.planningPermit.url = "javascript:alert(1)";
context.openPlanningEvidence(row);
assert.equal(opened, 1);
console.log("Land tracker JavaScript syntax and evidence rendering: passed");
