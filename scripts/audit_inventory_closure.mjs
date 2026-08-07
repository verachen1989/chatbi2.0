#!/usr/bin/env node

import fs from "node:fs";
import vm from "node:vm";

const pagePath = process.argv[2] || "index.html";
const source = fs.readFileSync(pagePath, "utf8");

function extractJsonConst(name, nextName) {
  const pattern = new RegExp(`const ${name} = (.*?);\\n(?:const|function) ${nextName}`, "s");
  const match = source.match(pattern);
  if (!match) throw new Error(`未找到 ${name}`);
  return JSON.parse(match[1]);
}

function extractJsConst(name, nextName) {
  const pattern = new RegExp(`const ${name} = (.*?);\\n(?:const|function) ${nextName}`, "s");
  const match = source.match(pattern);
  if (!match) throw new Error(`未找到 ${name}`);
  return vm.runInNewContext(`(${match[1]})`, Object.create(null));
}

function finiteCount(value) {
  if (value === null || value === undefined || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function closure(project) {
  const preferredTotal = finiteCount(project.officialResidentialTotal);
  const approvedTotal = finiteCount(project.approvedTotalSuites);
  const total = preferredTotal !== null && preferredTotal > 0
    ? preferredTotal
    : (approvedTotal !== null && approvedTotal > 0 ? approvedTotal : null);
  if (total === null) return { status:"pending", reason:"住宅总套数缺失" };

  const soldOut = finiteCount(project.officialSoldOutSuites) ?? 0;
  const contractSigned = finiteCount(project.officialContractSignedSuites);
  const filing = finiteCount(project.officialFilingSuites);
  const detailSigned = finiteCount(project.officialDetailSignedSuites);
  let cumulativeSold = null;
  if (contractSigned !== null && filing !== null) {
    cumulativeSold = contractSigned + filing + soldOut;
  } else if (detailSigned !== null) {
    cumulativeSold = detailSigned + soldOut;
  }
  if (cumulativeSold === null) return { status:"pending", total, reason:"同口径累计已售缺失" };
  if (cumulativeSold < 0 || cumulativeSold > total) {
    return { status:"conflict", total, cumulativeSold, reason:"累计已售超出住宅总套数" };
  }
  const remaining = total - cumulativeSold;
  return {
    status:"closed",
    total,
    cumulativeSold,
    remaining,
    equationValid: total === cumulativeSold + remaining,
  };
}

const data = extractJsonConst("DATA", "PROJECT_METADATA_OVERRIDES");
const officialLaunches = extractJsConst(
  "ZJW_OFFICIAL_NEW_LAUNCH_PROJECTS",
  "ZJW_NEW_LAUNCH_INVENTORY_STATUS_OVERRIDES",
);
const launchOverrides = extractJsConst(
  "ZJW_NEW_LAUNCH_INVENTORY_STATUS_OVERRIDES",
  "normalizePresaleName",
);
const projectNameAliases = extractJsConst(
  "PROJECT_NAME_ALIASES",
  "splitProjectNameParts",
);
const presaleIssueRecords = [
  ...extractJsConst("ZJW_PRESALE_ISSUE_RECORDS", "ZJW_PRESALE_ISSUE_BACKFILL_RECORDS"),
  ...extractJsConst("ZJW_PRESALE_ISSUE_BACKFILL_RECORDS", "ZJW_ALL_PRESALE_ISSUE_RECORDS"),
];

function normalizePresaleName(value) {
  return String(value || "")
    .replace(/[·・\s（）()《》]/g, "")
    .replace(/[一二三四五六七八九十]+期/g, "")
    .toLowerCase();
}

function splitProjectNameParts(value) {
  return String(value || "").split(/[；;、/\n]+/).map(part => part.trim()).filter(Boolean);
}

function projectMatchNames(project) {
  const names = [
    project.project,
    project.matchedName,
    project.summaryRecordName,
    project.officialProjectName,
    project.cricProjectName,
    project.janAprMatchedName,
    project.junMatchedName,
    project.junCricProjectName,
  ].flatMap(splitProjectNameParts);
  const normalized = new Set(names.map(normalizePresaleName).filter(Boolean));
  for (const [canonical, aliases] of Object.entries(projectNameAliases)) {
    const group = [canonical, ...aliases];
    if (group.some(name => normalized.has(normalizePresaleName(name)))) {
      names.push(...group);
    }
  }
  return [...new Set(names.map(normalizePresaleName).filter(name => name.length >= 3))];
}

function launchMatchesExisting(item, project) {
  const itemName = normalizePresaleName(item.officialProjectName);
  const names = projectMatchNames(project);
  const permitText = String(project.summaryPresalePermit || "");
  return (item.permits || []).some(permit => permit && permitText.includes(permit)) ||
    Boolean(itemName && names.some(name =>
      name === itemName ||
      (name.length >= 4 && itemName.length >= 4 && (name.includes(itemName) || itemName.includes(name)))
    ));
}

function projectPermitCoverage(project) {
  const permits = new Set();
  let activeYear = "";
  const addPermitText = value => {
    String(value || "").split(/[；;、/\n]+/).forEach(part => {
      const token = part.trim().replace(/（/g, "(").replace(/）/g, ")");
      const full = token.match(/京房售证字\((\d{4})\)(开?)(\d+)号/);
      if (full) {
        activeYear = full[1];
        permits.add(`京房售证字(${activeYear})${full[2]}${full[3]}号`);
        return;
      }
      const short = token.match(/^(开?)(\d+)号$/);
      if (short && activeYear) permits.add(`京房售证字(${activeYear})${short[1]}${short[2]}号`);
    });
  };
  addPermitText(project.summaryPresalePermit);
  for (const record of project.presaleIssueRecords || []) {
    if (record?.permit) addPermitText(record.permit);
  }
  const evidenceUrls = new Set(
    String(project.officialInventoryEvidenceUrl || "")
      .split(/\s+/)
      .filter(value => /^https?:\/\//.test(value)),
  );
  const permitEvidenceUrls = new Set(
    [...evidenceUrls].filter(url => /[?&]pageId=320794(?:&|$)/.test(url)),
  );
  const missing = project.officialMissingPermitBatches || [];
  const countGap = permits.size > 0 && permitEvidenceUrls.size < permits.size;
  const registryUnknown = permits.size === 0;
  return {
    knownPermits: [...permits],
    evidenceUrls: [...evidenceUrls],
    permitEvidenceUrls: [...permitEvidenceUrls],
    knownPermitCount: permits.size,
    evidencePageCount: permitEvidenceUrls.size,
    missingPermits: missing,
    status: missing.length || countGap ? "pending" : (registryUnknown ? "unknown" : "covered"),
    reason: missing.length
      ? `缺少预售证批次：${missing.join("、")}`
      : (countGap
        ? "住建委预售证详情页数量少于已知预售证数量"
        : (registryUnknown ? "缺少独立的预售证清单，无法判断是否少证" : "")),
  };
}

const projects = [...(data.projects || []), ...(data.launchProjects || [])];
for (const item of officialLaunches) {
  const residentialTotal = Number(item.residentialTotal ?? item.approvedResidentialSuites ?? 0);
  if (!Number.isFinite(residentialTotal) || residentialTotal <= 0) continue;
  if (projects.some(project => launchMatchesExisting(item, project))) continue;
  const override = launchOverrides[item.officialProjectName] || {};
  projects.push({
    project: item.officialProjectName,
    officialProjectName: item.officialProjectName,
    summaryPresalePermit: (item.permits || []).join(" / "),
    presaleIssueRecords: (item.permits || []).map((permit, index) => ({
      permit,
      date: item.issueDates?.[index] || item.issueDates?.[0] || "",
    })),
    officialInventoryEvidenceUrl: (item.detailUrls || []).join("\n"),
    officialResidentialTotal: residentialTotal,
    approvedTotalSuites: finiteCount(item.approvedTotalSuites),
    officialContractSignedSuites: finiteCount(override.contractSignedSuites),
    officialFilingSuites: finiteCount(override.filedSuites),
    officialSoldOutSuites: finiteCount(override.soldOutSuites),
    officialDetailSignedSuites: finiteCount(override.signedSuites),
    officialSignedSuites: finiteCount(override.signedSuites),
  });
}
for (const project of projects) {
  const records = [...(project.presaleIssueRecords || [])];
  for (const record of presaleIssueRecords) {
    if (launchMatchesExisting({ officialProjectName:record.name, permits:[record.permit] }, project)) {
      records.push(record);
    }
  }
  const unique = new Map();
  for (const record of records) {
    if (record?.permit) unique.set(`${record.date || ""}|${record.permit}`, record);
  }
  project.presaleIssueRecords = [...unique.values()];
}

const rows = projects.map(project => {
  const permitCoverage = projectPermitCoverage(project);
  const inventory = permitCoverage.status !== "covered"
    ? { status:"pending", reason:permitCoverage.reason }
    : closure(project);
  return {
    project: project.project || project.officialProjectName || "未命名项目",
    permitCoverage,
    ...inventory,
  };
});
const closed = rows.filter(row => row.status === "closed");
const pending = rows.filter(row => row.status === "pending");
const conflict = rows.filter(row => row.status === "conflict");
const invalidEquation = closed.filter(row => !row.equationValid);
const permitCoveragePending = rows.filter(row => row.permitCoverage.status === "pending");
const permitRegistryUnknown = rows.filter(row => row.permitCoverage.status === "unknown");

console.log(JSON.stringify({
  totalProjects: rows.length,
  closedProjects: closed.length,
  pendingProjects: pending.length,
  conflictProjects: conflict.length,
  invalidEquationProjects: invalidEquation.length,
  permitCoveragePendingProjects: permitCoveragePending.length,
  permitRegistryUnknownProjects: permitRegistryUnknown.length,
  pending,
  conflict,
  permitCoveragePending: permitCoveragePending.map(row => ({
    project: row.project,
    ...row.permitCoverage,
  })),
  permitRegistryUnknown: permitRegistryUnknown.map(row => ({
    project: row.project,
    ...row.permitCoverage,
  })),
}, null, 2));

if (conflict.length || invalidEquation.length) process.exitCode = 1;
