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
vm.runInContext(section("function html(", "function num(") + section("function evidenceDisplay(", "function render("), context);
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
console.log("Land tracker JavaScript syntax and evidence rendering: passed");
