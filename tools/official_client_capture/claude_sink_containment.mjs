#!/usr/bin/env node
/**
 * 为 Claude FW-E 目标 sink 生成可复算的 AST 包含关系证据。
 *
 * 目标 inventory 同时保留 AST 调用点和保守词法候选。词法命中可能只是内嵌文档、字符串、
 * 能力检查或普通同名方法；本工具只陈述其真实语法位置，不直接签发 disposition。人工复核仍须
 * 结合 source-to-sink、隐私门控和运行证据作出唯一处置。
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const TOOL_PATH = fileURLToPath(import.meta.url);
const TOOL_DIR = path.dirname(TOOL_PATH);
const WORKSPACE_ROOT = path.resolve(TOOL_DIR, "../..");

const INVENTORY_SCHEMA = "claude-code-target-sink-inventory/v1";
const AST_SCHEMA = "claude-code-target-native-inventory/v1";
const OUTPUT_SCHEMA = "claude-code-fw-e-sink-containment-evidence/v1";

function fail(message) {
  process.stderr.write(`失败：${message}\n`);
  process.exit(1);
}

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith("--")) {
      fail(`无法识别参数：${item}`);
    }
    const key = item.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      fail(`参数缺少值：${item}`);
    }
    if (Object.hasOwn(result, key)) {
      fail(`参数重复：${item}`);
    }
    result[key] = value;
    index += 1;
  }
  for (const key of ["bundle", "inventory", "ast-inventory", "output"]) {
    if (!result[key]) {
      fail(`缺少必填参数：--${key}`);
    }
  }
  return result;
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function sha256File(filePath) {
  return sha256Bytes(fs.readFileSync(filePath));
}

function readJson(filePath, label) {
  try {
    const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      fail(`${label} 顶层必须是对象`);
    }
    return value;
  } catch (error) {
    fail(`无法读取 ${label}：${error.message}`);
  }
}

function loadTypeScript(explicitPath) {
  const candidates = [];
  if (explicitPath) {
    candidates.push(path.resolve(explicitPath));
  }
  if (process.env.CLAUDE_AST_TYPESCRIPT_MODULE) {
    candidates.push(path.resolve(process.env.CLAUDE_AST_TYPESCRIPT_MODULE));
  }
  candidates.push(
    path.join(WORKSPACE_ROOT, "frontend/node_modules/typescript/lib/typescript.js"),
  );
  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) {
      continue;
    }
    return {
      module: require(candidate),
      path: candidate,
      sha256: sha256File(candidate),
    };
  }
  fail(
    "找不到 TypeScript 解析器；请安装 frontend 依赖或通过 " +
      "--typescript-module 指定锁定的 typescript.js",
  );
}

function canonical(value) {
  if (Array.isArray(value)) {
    return value.map(canonical);
  }
  if (value && typeof value === "object") {
    const output = {};
    for (const key of Object.keys(value).sort()) {
      output[key] = canonical(value[key]);
    }
    return output;
  }
  return value;
}

function canonicalSha256(value) {
  return sha256Bytes(JSON.stringify(canonical(value)));
}

function excerpt(source, start, end, radius = 180) {
  const windowStart = Math.max(0, start - radius);
  const windowEnd = Math.min(source.length, Math.max(end, start + 1) + radius);
  const raw = source.slice(windowStart, windowEnd);
  return {
    start: windowStart,
    end: windowEnd,
    sha256: sha256Bytes(Buffer.from(raw, "utf8")),
    excerpt: raw.replaceAll(/\s+/g, " ").trim(),
  };
}

function ancestorRows(ts, sourceFile, node) {
  const rows = [];
  let current = node;
  while (current && rows.length < 12) {
    rows.push({
      kind: ts.SyntaxKind[current.kind] ?? `SyntaxKind(${current.kind})`,
      start: current.getStart(sourceFile, false),
      end: current.getEnd(),
    });
    current = current.parent;
  }
  return rows;
}

function findExactCall(ts, sourceFile, token, start, end) {
  let current = token;
  while (current) {
    if (
      (ts.isCallExpression(current) || ts.isNewExpression(current)) &&
      current.getStart(sourceFile, false) === start &&
      current.getEnd() === end
    ) {
      return current;
    }
    current = current.parent;
  }
  return null;
}

function nearestFunction(ts, sourceFile, node) {
  let current = node;
  while (current) {
    if (ts.isFunctionLike(current)) {
      const name = current.name?.getText(sourceFile) ?? null;
      return {
        kind: ts.SyntaxKind[current.kind],
        name,
        start: current.getStart(sourceFile, false),
        end: current.getEnd(),
        sha256: sha256Bytes(
          Buffer.from(
            sourceFile.text.slice(
              current.getStart(sourceFile, false),
              current.getEnd(),
            ),
            "utf8",
          ),
        ),
      };
    }
    current = current.parent;
  }
  return null;
}

function lexicalFinding(ts, token) {
  const chain = [];
  let current = token;
  while (current) {
    chain.push(current);
    current = current.parent;
  }
  const kindNames = chain.map((node) => ts.SyntaxKind[node.kind] ?? "Unknown");
  const literal = kindNames.find((name) =>
    /StringLiteral|TemplateHead|TemplateMiddle|TemplateTail|TemplateLiteral|FirstTemplateToken|LastTemplateToken/.test(
      name,
    ),
  );
  if (literal) {
    return {
      classification: "non_executable_literal",
      reason: `词法命中位于 ${literal} 内，只是数据文本，不构成独立调用点。`,
    };
  }
  if (chain.some((node) => ts.isTypeOfExpression(node))) {
    return {
      classification: "capability_check",
      reason: "词法命中位于 typeof 能力检查内，没有形成独立发送调用。",
    };
  }
  const propertyAccess = chain.find((node) => ts.isPropertyAccessExpression(node));
  if (
    propertyAccess &&
    propertyAccess.name === token &&
    !ts.isCallExpression(propertyAccess.parent)
  ) {
    return {
      classification: "non_call_property_reference",
      reason: "词法命中只是属性引用，未作为 CallExpression 的被调用表达式。",
    };
  }
  const declaration = chain.find(
    (node) =>
      ts.isMethodDeclaration(node) ||
      ts.isMethodSignature(node) ||
      ts.isPropertyDeclaration(node),
  );
  if (declaration) {
    return {
      classification: "declaration_name_or_body",
      reason: "词法命中位于普通成员声明；必须结合调用节点另行判断，不能把名称本身视为网络 sink。",
    };
  }
  return {
    classification: "executable_context_requires_review",
    reason: "语法位置不能单独排除发送语义，必须继续做 source-to-sink 人工复核。",
  };
}

function validateInventory(inventory, astInventory, bundleSha256, sourceLength) {
  if (inventory.schema_version !== INVENTORY_SCHEMA) {
    fail(`目标 inventory schema 不匹配：${inventory.schema_version}`);
  }
  if (astInventory.schema_version !== AST_SCHEMA) {
    fail(`AST inventory schema 不匹配：${astInventory.schema_version}`);
  }
  if (inventory.bundle_sha256 !== bundleSha256) {
    fail("目标 inventory 与 bundle 摘要不一致");
  }
  if (astInventory.bundle?.sha256 !== bundleSha256) {
    fail("AST inventory 与 bundle 摘要不一致");
  }
  if (
    inventory.completeness?.truncated !== false ||
    inventory.completeness?.ast_parse_diagnostic_count !== 0 ||
    inventory.completeness?.ambiguous_lexical_match_count !== 0
  ) {
    fail("目标 inventory 不完整");
  }
  if (!Array.isArray(inventory.sinks) || inventory.sink_total !== inventory.sinks.length) {
    fail("目标 inventory sink_total 与数组不一致");
  }
  if (!Array.isArray(astInventory.sinks) || astInventory.sink_total !== astInventory.sinks.length) {
    fail("AST inventory sink_total 与数组不一致");
  }
  const identities = inventory.sinks.map((row) => row.sink_id);
  if (identities.some((item) => typeof item !== "string") || new Set(identities).size !== identities.length) {
    fail("目标 inventory sink_id 缺失或重复");
  }
  for (const row of inventory.sinks) {
    if (
      !Number.isInteger(row.source_start) ||
      !Number.isInteger(row.source_end) ||
      row.source_start < 0 ||
      row.source_end < row.source_start ||
      row.source_end > sourceLength
    ) {
      fail(`目标 sink 坐标非法：${row.sink_id}`);
    }
  }
}

function buildEvidence(ts, sourceFile, inventory, astInventory) {
  const astById = new Map(astInventory.sinks.map((row) => [row.sink_id, row]));
  const rows = [];
  const failures = [];
  for (const sink of inventory.sinks) {
    const start = sink.source_start;
    const end = sink.source_end;
    const token = ts.getTokenAtPosition(sourceFile, Math.min(start, sourceFile.text.length - 1));
    const base = {
      sink_id: sink.sink_id,
      source_kind: sink.source_kind,
      category: sink.category,
      source_start: start,
      source_end: end,
      semantic_sha256: sink.semantic_sha256,
      source_window: excerpt(sourceFile.text, start, end),
      token: {
        kind: ts.SyntaxKind[token.kind] ?? `SyntaxKind(${token.kind})`,
        start: token.getStart(sourceFile, false),
        end: token.getEnd(),
        sha256: sha256Bytes(Buffer.from(token.getText(sourceFile), "utf8")),
      },
      ancestors: ancestorRows(ts, sourceFile, token),
      nearest_function: nearestFunction(ts, sourceFile, token),
    };
    if (sink.source_kind === "ast_call") {
      const astRow = astById.get(sink.sink_id);
      const exact = findExactCall(ts, sourceFile, token, start, end);
      if (!astRow || !exact) {
        failures.push(sink.sink_id);
        rows.push({
          ...base,
          structural_finding: "ast_call_unmatched",
          structural_reason: "无法把 inventory 坐标重新绑定到精确 CallExpression／NewExpression。",
        });
        continue;
      }
      const nodeText = sourceFile.text.slice(start, end);
      rows.push({
        ...base,
        structural_finding: "exact_ast_call",
        structural_reason: "inventory 坐标已重新绑定到目标 bundle 的精确调用节点。",
        call: {
          kind: ts.SyntaxKind[exact.kind],
          sha256: sha256Bytes(Buffer.from(nodeText, "utf8")),
          excerpt: nodeText.replaceAll(/\s+/g, " ").trim().slice(0, 1200),
          excerpt_truncated: nodeText.replaceAll(/\s+/g, " ").trim().length > 1200,
          callee_tail: astRow.callee_tail ?? [],
          argument_shapes: astRow.argument_shapes ?? [],
          environment_keys: astRow.environment_keys ?? [],
          privacy_keys: astRow.privacy_keys ?? [],
          relevant_literals: astRow.relevant_literals ?? [],
        },
      });
      continue;
    }
    if (sink.source_kind !== "lexical_only") {
      failures.push(sink.sink_id);
      rows.push({
        ...base,
        structural_finding: "unknown_source_kind",
        structural_reason: `未知 source_kind：${sink.source_kind}`,
      });
      continue;
    }
    const finding = lexicalFinding(ts, token);
    rows.push({
      ...base,
      structural_finding: finding.classification,
      structural_reason: finding.reason,
    });
  }
  return { rows, failures };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const bundlePath = path.resolve(args.bundle);
  const inventoryPath = path.resolve(args.inventory);
  const astInventoryPath = path.resolve(args["ast-inventory"]);
  const outputPath = path.resolve(args.output);
  if (fs.existsSync(outputPath)) {
    fail("output 必须不存在，禁止覆盖既有证据");
  }
  const bundleBytes = fs.readFileSync(bundlePath);
  const bundleSha256 = sha256Bytes(bundleBytes);
  const source = bundleBytes.toString("utf8");
  if (Buffer.byteLength(source, "utf8") !== bundleBytes.length) {
    fail("bundle 不是完整 UTF-8 文本");
  }
  const inventory = readJson(inventoryPath, "目标 sink inventory");
  const astInventory = readJson(astInventoryPath, "AST inventory");
  validateInventory(inventory, astInventory, bundleSha256, source.length);

  const typescript = loadTypeScript(args["typescript-module"]);
  const ts = typescript.module;
  const sourceFile = ts.createSourceFile(
    "cli.js",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.JS,
  );
  const diagnostics = sourceFile.parseDiagnostics.map((item) => ({
    code: item.code,
    start: item.start ?? null,
    length: item.length ?? null,
  }));
  if (diagnostics.length) {
    fail(`TypeScript 解析出现 ${diagnostics.length} 个诊断`);
  }

  const evidence = buildEvidence(ts, sourceFile, inventory, astInventory);
  const findingCounts = {};
  for (const row of evidence.rows) {
    findingCounts[row.structural_finding] = (findingCounts[row.structural_finding] ?? 0) + 1;
  }
  const output = {
    schema_version: OUTPUT_SCHEMA,
    target_version: inventory.target_version,
    platform: inventory.platform,
    bundle: {
      path: args.bundle,
      sha256: bundleSha256,
      byte_count: bundleBytes.length,
    },
    inventory: {
      path: args.inventory,
      sha256: sha256File(inventoryPath),
    },
    ast_inventory: {
      path: args["ast-inventory"],
      sha256: sha256File(astInventoryPath),
    },
    producer: {
      path: path.relative(WORKSPACE_ROOT, TOOL_PATH),
      sha256: sha256File(TOOL_PATH),
    },
    parser: {
      node_version: process.version,
      typescript_version: ts.version,
      module_path: typescript.path,
      module_sha256: typescript.sha256,
      parse_diagnostics: diagnostics,
    },
    completeness: {
      target_sink_count: inventory.sinks.length,
      evidence_row_count: evidence.rows.length,
      unmatched_sink_ids: evidence.failures.sort(),
      structural_finding_counts: Object.fromEntries(Object.entries(findingCounts).sort()),
      result:
        evidence.rows.length === inventory.sinks.length && evidence.failures.length === 0
          ? "passed"
          : "blocked",
    },
    evidence: evidence.rows,
  };
  output.evidence_sha256 = canonicalSha256(output.evidence);

  fs.mkdirSync(path.dirname(outputPath), { recursive: true, mode: 0o700 });
  fs.chmodSync(path.dirname(outputPath), 0o700);
  fs.writeFileSync(outputPath, `${JSON.stringify(output, null, 2)}\n`, {
    encoding: "utf8",
    flag: "wx",
    mode: 0o600,
  });
  process.stdout.write(
    `完成：sink=${output.completeness.evidence_row_count}，` +
      `未匹配=${output.completeness.unmatched_sink_ids.length}，` +
      `result=${output.completeness.result}\n`,
  );
}

main();
