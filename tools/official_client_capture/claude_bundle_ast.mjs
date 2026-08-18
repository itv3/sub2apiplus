#!/usr/bin/env node
/**
 * 对 Claude Code 官方 Bun SEA 主 bundle 做完整 JavaScript AST 网络发送面盘点。
 *
 * 该工具只产生保守的候选与可达性事实，不把静态命中直接提升为官方 wire 规则。输出必须继续经过
 * 跨来源矩阵、人工 disposition 和目标版本运行证据闭环。与旧词法索引不同，本工具不截断 sink。
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import crypto from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const TOOL_PATH = fileURLToPath(import.meta.url);
const TOOL_DIR = path.dirname(TOOL_PATH);
const WORKSPACE_ROOT = path.resolve(TOOL_DIR, "../..");

const SCHEMA_VERSION = "claude-code-target-native-inventory/v1";
const NETWORK_METHODS = new Set([
  "connect",
  "create",
  "delete",
  "fetch",
  "get",
  "head",
  "list",
  "open",
  "options",
  "patch",
  "post",
  "put",
  "request",
  "resolve",
  "stream",
]);
const HTTP_CLIENT_METHODS = new Set([
  "delete",
  "get",
  "head",
  "options",
  "patch",
  "post",
  "put",
  "request",
]);
const HTTP_OPTION_KEYS = new Set([
  "auth",
  "baseurl",
  "headers",
  "httpsagent",
  "maxbodylength",
  "maxcontentlength",
  "proxy",
  "refreshoauth",
  "responseType".toLowerCase(),
  "signal",
  "timeout",
  "validatestatus",
]);
const PROCESS_METHODS = new Set(["exec", "execFile", "spawn", "spawnSync"]);
const ANTHROPIC_RESOURCES = new Set([
  "batches",
  "completions",
  "files",
  "messages",
  "models",
  "oauth",
  "skills",
  "tokens",
]);
const NETWORK_RECEIVERS = new Set([
  "axios",
  "bun",
  "dns",
  "got",
  "http",
  "https",
  "net",
  "tls",
  "undici",
]);
const NETWORK_CONSTRUCTORS = new Set([
  "EventSource",
  "WebSocket",
  "XMLHttpRequest",
]);
const PRIVACY_KEYS = new Set([
  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
  "CLAUDE_CODE_ENABLE_TELEMETRY",
  "DISABLE_TELEMETRY",
  "DO_NOT_TRACK",
]);

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
  for (const key of ["bundle", "output", "expected-sha256"]) {
    if (!result[key]) {
      fail(`缺少必填参数：--${key}`);
    }
  }
  if (!/^[0-9a-f]{64}$/.test(result["expected-sha256"])) {
    fail("--expected-sha256 必须是小写 SHA-256");
  }
  return result;
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function sha256File(filePath) {
  return sha256Bytes(fs.readFileSync(filePath));
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

function propertySegments(ts, expression) {
  if (!expression) {
    return [];
  }
  if (ts.isIdentifier(expression)) {
    return [expression.text];
  }
  if (ts.isPropertyAccessExpression(expression)) {
    return [...propertySegments(ts, expression.expression), expression.name.text];
  }
  if (ts.isElementAccessExpression(expression)) {
    const base = propertySegments(ts, expression.expression);
    const argument = expression.argumentExpression;
    if (argument && (ts.isStringLiteral(argument) || ts.isNumericLiteral(argument))) {
      return [...base, argument.text];
    }
    return [...base, "[]"];
  }
  if (ts.isParenthesizedExpression(expression)) {
    return propertySegments(ts, expression.expression);
  }
  if (ts.isCallExpression(expression)) {
    return [...propertySegments(ts, expression.expression), "()"];
  }
  return [];
}

function staticString(ts, node) {
  if (!node) {
    return null;
  }
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return node.text;
  }
  return null;
}

function relevantLiteral(value) {
  const lower = value.toLowerCase();
  return (
    value.startsWith("/") ||
    lower.startsWith("http://") ||
    lower.startsWith("https://") ||
    lower.includes("anthropic") ||
    lower.includes("claude") ||
    lower.includes("authorization") ||
    lower.includes("content-type") ||
    lower.includes("user-agent") ||
    lower.includes("retry") ||
    lower.includes("request-id") ||
    lower.includes("session-id") ||
    lower.includes("traceparent") ||
    lower.startsWith("x-")
  );
}

function argumentShape(ts, node, depth = 0) {
  if (!node || depth > 3) {
    return "unknown";
  }
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return relevantLiteral(node.text) ? `literal:${node.text}` : "string";
  }
  if (ts.isNumericLiteral(node)) {
    return "number";
  }
  if (node.kind === ts.SyntaxKind.TrueKeyword || node.kind === ts.SyntaxKind.FalseKeyword) {
    return "boolean";
  }
  if (node.kind === ts.SyntaxKind.NullKeyword) {
    return "null";
  }
  if (ts.isIdentifier(node)) {
    return "identifier";
  }
  if (ts.isArrayLiteralExpression(node)) {
    return {
      array: node.elements.map((item) => argumentShape(ts, item, depth + 1)),
    };
  }
  if (ts.isObjectLiteralExpression(node)) {
    const keys = [];
    for (const property of node.properties) {
      if (!property.name) {
        keys.push("spread");
        continue;
      }
      const name = property.name.text ?? property.name.getText();
      keys.push(String(name));
    }
    return { object_keys: [...new Set(keys)].sort() };
  }
  if (ts.isCallExpression(node)) {
    return { call: propertySegments(ts, node.expression).slice(-4) };
  }
  if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
    return { member: propertySegments(ts, node).slice(-4) };
  }
  if (ts.isTemplateExpression(node)) {
    const staticParts = [node.head.text, ...node.templateSpans.map((item) => item.literal.text)]
      .filter(relevantLiteral);
    return staticParts.length ? { template_literals: staticParts } : "template";
  }
  return ts.SyntaxKind[node.kind] ?? "unknown";
}

function hasUrlArgument(ts, node) {
  return node.arguments.some((argument) => {
    const value = staticString(ts, argument);
    return Boolean(value && (/^https?:\/\//i.test(value) || value.startsWith("/")));
  });
}

function objectLiteralKeys(ts, node) {
  if (!ts.isObjectLiteralExpression(node)) {
    return [];
  }
  return node.properties.flatMap((property) => {
    if (!property.name) {
      return [];
    }
    const value = property.name.text ?? property.name.getText();
    return [String(value).toLowerCase()];
  });
}

function hasHttpOptions(ts, node) {
  return node.arguments.slice(1).some((argument) =>
    objectLiteralKeys(ts, argument).some((key) => HTTP_OPTION_KEYS.has(key)),
  );
}

function classifyCall(ts, node, aliases) {
  const segments = propertySegments(ts, node.expression);
  const lower = segments.map((item) => item.toLowerCase());
  const last = lower.at(-1) ?? "";
  const previous = lower.at(-2) ?? "";
  const first = lower[0] ?? "";

  if (segments.length === 1 && aliases.has(segments[0])) {
    return `alias_${aliases.get(segments[0])}`;
  }
  if (last === "fetch") {
    return "fetch";
  }
  if (last === "create" && previous === "messages") {
    return "anthropic_messages_create";
  }
  if (last === "stream" && previous === "messages") {
    return "anthropic_messages_stream";
  }
  if ((last === "counttokens" || last === "count_tokens") && previous === "messages") {
    return "anthropic_messages_count_tokens";
  }
  if (NETWORK_METHODS.has(last) && lower.some((item) => ANTHROPIC_RESOURCES.has(item))) {
    return "anthropic_resource_call";
  }
  if ((last === "request" || last === "get") && NETWORK_RECEIVERS.has(first)) {
    return "node_http_request";
  }
  if (last === "connect" && NETWORK_RECEIVERS.has(first)) {
    return first === "tls" ? "tls_connect" : "socket_connect";
  }
  if ((last === "resolve" || last === "lookup") && first === "dns") {
    return "dns_resolution";
  }
  if (last === "open" && node.arguments.length >= 2) {
    const method = staticString(ts, node.arguments[0]);
    const target = staticString(ts, node.arguments[1]);
    if (method && /^[A-Z]+$/.test(method) && target) {
      return "xhr_open";
    }
  }
  if (PROCESS_METHODS.has(last)) {
    const executable = staticString(ts, node.arguments[0]);
    if (executable && /(?:^|\/)(?:curl|wget|http|https)$/.test(executable)) {
      return "external_network_process";
    }
  }
  if (NETWORK_METHODS.has(last) && hasUrlArgument(ts, node)) {
    return "url_bearing_call";
  }
  // Minify bundle 常先由同一函数计算 URL，再把局部变量交给 axios/gaxios 一类 wrapper。
  // 仅凭 `.get(variable)` 会把 Map 等普通调用误判；同时出现 HTTP 方法和强网络选项时才保守收录。
  if (HTTP_CLIENT_METHODS.has(last) && node.arguments.length > 0 && hasHttpOptions(ts, node)) {
    return "http_client_method_candidate";
  }
  return null;
}

function classifyNew(ts, node) {
  const segments = propertySegments(ts, node.expression);
  const last = segments.at(-1) ?? "";
  return NETWORK_CONSTRUCTORS.has(last) ? last.toLowerCase() : null;
}

function functionBinding(ts, node) {
  if (ts.isFunctionDeclaration(node) && node.name) {
    return node.name.text;
  }
  const parent = node.parent;
  if (parent && ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name)) {
    return parent.name.text;
  }
  if (
    parent &&
    ts.isBinaryExpression(parent) &&
    parent.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
    ts.isIdentifier(parent.left)
  ) {
    return parent.left.text;
  }
  if (node.name && ts.isIdentifier(node.name)) {
    return node.name.text;
  }
  return null;
}

function functionLike(ts, node) {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node) ||
    ts.isConstructorDeclaration(node)
  );
}

function envKey(ts, node) {
  if (ts.isPropertyAccessExpression(node)) {
    const base = propertySegments(ts, node.expression).map((item) => item.toLowerCase());
    if (
      (base.length >= 2 && base.at(-2) === "process" && base.at(-1) === "env") ||
      (base.length >= 1 && base.at(-1) === "env")
    ) {
      return node.name.text;
    }
  }
  if (ts.isElementAccessExpression(node)) {
    const base = propertySegments(ts, node.expression).map((item) => item.toLowerCase());
    const value = staticString(ts, node.argumentExpression);
    if (value && base.at(-1) === "env") {
      return value;
    }
  }
  return null;
}

function collectAliases(ts, sourceFile) {
  const aliases = new Map();
  function visit(node) {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer
    ) {
      const segments = propertySegments(ts, node.initializer);
      const last = segments.at(-1)?.toLowerCase();
      if (last === "fetch") {
        aliases.set(node.name.text, "fetch");
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return aliases;
}

function analyze(ts, sourceFile, sourceText) {
  const bindings = new Map();
  const functionNodes = [];
  function collectFunctions(node) {
    if (functionLike(ts, node)) {
      const name = functionBinding(ts, node);
      functionNodes.push({ node, name });
      if (name) {
        bindings.set(name, (bindings.get(name) ?? 0) + 1);
      }
    }
    ts.forEachChild(node, collectFunctions);
  }
  collectFunctions(sourceFile);

  const uniqueFunctions = new Map(
    functionNodes
      .filter((item) => item.name && bindings.get(item.name) === 1)
      .map((item) => [item.name, item.node]),
  );
  const aliases = collectAliases(ts, sourceFile);
  const references = new Map();
  const facts = new Map();
  const sinks = [];
  const tailCalls = [];
  const functionStack = [];

  function ownerName() {
    for (let index = functionStack.length - 1; index >= 0; index -= 1) {
      const name = functionStack[index];
      if (name && uniqueFunctions.has(name)) {
        return name;
      }
    }
    return null;
  }

  function ownerFacts(name) {
    const key = name ?? "<module>";
    if (!facts.has(key)) {
      facts.set(key, { env: new Set(), literals: new Set() });
    }
    return facts.get(key);
  }

  function addSink(node, category, expression) {
    const owner = ownerName();
    const callee = propertySegments(ts, expression);
    const start = node.getStart(sourceFile, false);
    const lineInfo = sourceFile.getLineAndCharacterOfPosition(start);
    const nodeText = sourceText.slice(start, node.end);
    const argumentsValue = node.arguments
      ? node.arguments.map((argument) => argumentShape(ts, argument))
      : [];
    const semantic = canonical({
      argument_shapes: argumentsValue,
      callee_tail: callee.slice(-5).map((item, index) =>
        index === 0 && callee.length > 1 ? "$receiver" : item),
      category,
    });
    sinks.push({
      category,
      source_start: start,
      source_end: node.end,
      line: lineInfo.line + 1,
      column: lineInfo.character + 1,
      owner_symbol: owner,
      callee_tail: callee.slice(-6),
      argument_shapes: argumentsValue,
      node_sha256: sha256Bytes(nodeText),
      semantic_sha256: sha256Bytes(JSON.stringify(semantic)),
    });
  }

  function visit(node) {
    const isFunction = functionLike(ts, node);
    if (isFunction) {
      functionStack.push(functionBinding(ts, node));
    }

    const owner = ownerName();
    if (owner && ts.isIdentifier(node) && uniqueFunctions.has(node.text) && node.text !== owner) {
      if (!references.has(owner)) {
        references.set(owner, new Set());
      }
      references.get(owner).add(node.text);
    }

    const key = envKey(ts, node);
    if (key) {
      ownerFacts(owner).env.add(key);
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (relevantLiteral(node.text)) {
        ownerFacts(owner).literals.add(node.text);
      }
    }

    if (ts.isCallExpression(node)) {
      const category = classifyCall(ts, node, aliases);
      if (category) {
        addSink(node, category, node.expression);
      }
      if (
        node.end >= sourceText.length - 4096 &&
        ts.isIdentifier(node.expression) &&
        uniqueFunctions.has(node.expression.text)
      ) {
        tailCalls.push({ name: node.expression.text, start: node.getStart(sourceFile, false) });
      }
    } else if (ts.isNewExpression(node)) {
      const category = classifyNew(ts, node);
      if (category) {
        addSink(node, category, node.expression);
      }
    }

    ts.forEachChild(node, visit);
    if (isFunction) {
      functionStack.pop();
    }
  }
  visit(sourceFile);

  tailCalls.sort((left, right) => right.start - left.start);
  const entryCandidates = [...new Set(tailCalls.map((item) => item.name))];
  const reachable = new Set(entryCandidates);
  const queue = [...entryCandidates];
  while (queue.length) {
    const current = queue.shift();
    for (const target of references.get(current) ?? []) {
      if (!reachable.has(target)) {
        reachable.add(target);
        queue.push(target);
      }
    }
  }

  const ordinals = new Map();
  sinks.sort((left, right) => left.source_start - right.source_start);
  for (const sink of sinks) {
    const ordinal = (ordinals.get(sink.semantic_sha256) ?? 0) + 1;
    ordinals.set(sink.semantic_sha256, ordinal);
    sink.sink_id = `TN-SINK-${sink.semantic_sha256.slice(0, 16)}-${ordinal}`;
    sink.reachability = sink.owner_symbol && reachable.has(sink.owner_symbol)
      ? "possible_from_entry"
      : "unknown";
    const directFacts = facts.get(sink.owner_symbol ?? "<module>") ?? {
      env: new Set(),
      literals: new Set(),
    };
    sink.environment_keys = [...directFacts.env].sort();
    sink.privacy_keys = sink.environment_keys.filter((item) => PRIVACY_KEYS.has(item));
    sink.relevant_literals = [...directFacts.literals].sort();
  }

  const categoryCounts = {};
  for (const sink of sinks) {
    categoryCounts[sink.category] = (categoryCounts[sink.category] ?? 0) + 1;
  }
  return {
    entry_candidates: entryCandidates,
    function_count: functionNodes.length,
    unique_function_count: uniqueFunctions.size,
    reachable_function_count: reachable.size,
    alias_count: aliases.size,
    sink_total: sinks.length,
    sink_category_counts: canonical(categoryCounts),
    sinks,
  };
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const bundlePath = path.resolve(args.bundle);
  const outputPath = path.resolve(args.output);
  if (!fs.existsSync(bundlePath) || !fs.statSync(bundlePath).isFile()) {
    fail(`bundle 不存在：${bundlePath}`);
  }
  if (fs.existsSync(outputPath)) {
    fail(`output 已存在，禁止覆盖：${outputPath}`);
  }
  const bundle = fs.readFileSync(bundlePath);
  const bundleSha = sha256Bytes(bundle);
  if (bundleSha !== args["expected-sha256"]) {
    fail(`bundle SHA-256 不匹配：期望 ${args["expected-sha256"]}，实际 ${bundleSha}`);
  }
  const typescript = loadTypeScript(args["typescript-module"]);
  const ts = typescript.module;
  const sourceText = bundle.toString("utf8");
  const sourceFile = ts.createSourceFile(
    path.basename(bundlePath),
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.JS,
  );
  const diagnostics = sourceFile.parseDiagnostics.map((item) => ({
    code: item.code,
    start: item.start ?? null,
    length: item.length ?? null,
    message: ts.flattenDiagnosticMessageText(item.messageText, "\n"),
  }));
  const analysis = analyze(ts, sourceFile, sourceText);
  const result = {
    schema_version: SCHEMA_VERSION,
    bundle: {
      path: bundlePath,
      bytes: bundle.length,
      sha256: bundleSha,
    },
    parser: {
      node_version: process.version,
      typescript_version: ts.version,
      typescript_module_path: typescript.path,
      typescript_module_sha256: typescript.sha256,
      parse_diagnostics: diagnostics,
    },
    producer: {
      path: path.relative(WORKSPACE_ROOT, TOOL_PATH),
      sha256: sha256File(TOOL_PATH),
    },
    completeness: {
      truncated: false,
      sink_limit: null,
      parse_diagnostic_count: diagnostics.length,
      disposition_state: "unclassified_until_crosswalk",
    },
    ...analysis,
    limitations: [
      "AST 可枚举语法中的候选调用，但动态别名、反射、原生模块与宿主 transport 仍需运行证据闭环。",
      "possible_from_entry 是保守调用图结果，不等于运行时必然可达。",
      "相关字面量和环境键取自最近函数，只用于生成场景，不单独证明数据流。",
    ],
  };
  fs.mkdirSync(path.dirname(outputPath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(outputPath, `${JSON.stringify(result, null, 2)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  process.stdout.write(
    `通过：sink=${result.sink_total}，解析诊断=${diagnostics.length}，` +
      `入口候选=${result.entry_candidates.length}\n`,
  );
}

main();
