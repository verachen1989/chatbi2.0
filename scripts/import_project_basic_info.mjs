import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { basename, resolve } from "node:path";

const [sourceArg, outputArg = "project_basic_info.js"] = process.argv.slice(2);

if (!sourceArg) {
  console.error("用法: node scripts/import_project_basic_info.mjs <项目基本信息清单.xlsx> [输出.js]");
  process.exit(1);
}

const sourcePath = resolve(sourceArg);
const outputPath = resolve(outputArg);

function unzipText(archivePath, memberPath) {
  return execFileSync("unzip", ["-p", archivePath, memberPath], { encoding: "utf8" });
}

function decodeXml(value) {
  return String(value || "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)));
}

function sharedStrings(archivePath) {
  const xml = unzipText(archivePath, "xl/sharedStrings.xml");
  return [...xml.matchAll(/<si>([\s\S]*?)<\/si>/g)].map(match =>
    decodeXml([...match[1].matchAll(/<t[^>]*>([\s\S]*?)<\/t>/g)].map(text => text[1]).join(""))
  );
}

function cellValue(cellXml, strings) {
  const type = cellXml.match(/<c[^>]*\bt="([^"]+)"/)?.[1] || "";
  const raw = cellXml.match(/<v>([\s\S]*?)<\/v>/)?.[1] || "";
  if (type === "s") return strings[Number(raw)] ?? "";
  if (type === "inlineStr") return decodeXml(cellXml.match(/<t[^>]*>([\s\S]*?)<\/t>/)?.[1] || "");
  return decodeXml(raw);
}

function worksheetRows(archivePath, strings) {
  const xml = unzipText(archivePath, "xl/worksheets/sheet1.xml");
  return [...xml.matchAll(/<row[^>]*>([\s\S]*?)<\/row>/g)].map(rowMatch => {
    const row = {};
    for (const cellMatch of rowMatch[1].matchAll(/<c[^>]*\br="([A-Z]+)\d+"[^>]*>[\s\S]*?<\/c>/g)) {
      row[cellMatch[1]] = cellValue(cellMatch[0], strings).trim();
    }
    return row;
  });
}

function isoDate(value) {
  const match = String(value || "").trim().match(/^(\d{4})[\/-](\d{1,2})[\/-](\d{1,2})$/);
  if (!match) return "";
  return `${match[1]}-${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}`;
}

const strings = sharedStrings(sourcePath);
const [header = {}, ...sourceRows] = worksheetRows(sourcePath, strings);
const expectedHeaders = {
  A: "克而瑞项目名",
  B: "最早开盘日",
  C: "最新开盘日",
  D: "规划户数",
  E: "去化表项目名"
};

for (const [column, label] of Object.entries(expectedHeaders)) {
  if (header[column] !== label) {
    throw new Error(`表头不符合预期: ${column} 列应为“${label}”，实际为“${header[column] || "空"}”`);
  }
}

const rows = sourceRows
  .filter(row => row.A || row.E)
  .map((row, index) => {
    const plannedHouseholds = Number(String(row.D || "").replace(/,/g, ""));
    const earliestLaunchDate = isoDate(row.B);
    const latestLaunchDate = isoDate(row.C);
    if (!earliestLaunchDate || !latestLaunchDate || !Number.isFinite(plannedHouseholds)) {
      throw new Error(`第 ${index + 2} 行基础信息不完整或格式错误`);
    }
    return {
      cricProjectName: row.A,
      dehuaProjectName: row.E,
      earliestLaunchDate,
      latestLaunchDate,
      plannedHouseholds
    };
  });

const duplicateKeys = new Map();
for (const row of rows) {
  for (const key of new Set([row.cricProjectName, row.dehuaProjectName].filter(Boolean))) {
    if (!duplicateKeys.has(key)) duplicateKeys.set(key, []);
    duplicateKeys.get(key).push(row.cricProjectName);
  }
}
const duplicates = [...duplicateKeys.entries()].filter(([, names]) => names.length > 1);
if (duplicates.length) {
  throw new Error(`发现重复项目键: ${duplicates.map(([key]) => key).join("、")}`);
}

const generatedAt = new Date().toISOString();
const payload = `// 由 scripts/import_project_basic_info.mjs 自动生成，请勿手工编辑。\n` +
  `window.PROJECT_BASIC_INFO = ${JSON.stringify({
    sourceFile: basename(sourcePath),
    generatedAt,
    rows
  }, null, 2)};\n`;

writeFileSync(outputPath, payload, "utf8");
console.log(`已导入 ${rows.length} 个项目基础信息 -> ${outputPath}`);
