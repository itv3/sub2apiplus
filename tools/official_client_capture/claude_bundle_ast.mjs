#!/usr/bin/env node
/**
 * 对 Claude Code 官方 Bun SEA 的 JavaScript 做完整 AST 网络发送面盘点。
 *
 * 该工具只产生保守的候选与可达性事实，不把静态命中直接提升为官方 wire 规则。输出必须继续经过
 * 跨来源矩阵、人工 disposition 和目标版本运行证据闭环。与旧词法索引不同，本工具不截断 sink。
 *
 * 输入二选一（Claude 升级控制面设计 B2-03）：
 * - `--bundle <js>`：旧版单主 bundle（2.1.220 形态），输出与旧实现同口径；
 * - `--bundle-manifest <json>`：extract_claude_bundle.py 写出的模块清单（2.1.245 起的分块 bundle）。
 *   逐模块解析全部 JS 模块（一次只持有一个模块的语法树），把跨模块 import／export from／动态
 *   import／require／Worker／`/$bunfs/` 路径引用汇成 module_edges；按跨模块边确认入口候选，再沿
 *   导入绑定把各模块的调用图连起来，给每个 sink 三态可达性：
 *     possible_from_entry：保守调用图显示可能从已确认入口到达；
 *     unknown：所属模块可从入口加载，但调用图不能说明该 sink 可达；
 *     unresolved_entry：所属模块与已确认入口的关系未决（没有确认的入口，或模块不在入口闭包内）。
 * 坐标一律按 UTF-16 码元（offset_unit: "utf16"），与 TypeScript 节点位置一致；词法索引另给
 * offset_utf16 供合并。
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
const MANIFEST_SCHEMA = "claude-code-bun-bundle-manifest/v1";
const OFFSET_UNIT = "utf16";
// 入口调用只在模块末尾这么多个 UTF-16 码元内寻找（与旧实现一致）。
const TAIL_WINDOW = 4096;
const BUNFS_PREFIX = "/$bunfs/";
// 需要做 AST 的模块角色；与 extract_claude_bundle.JS_ROLES 一致。
const JS_ROLES = new Set(["entry", "entry_candidate", "chunk", "js_module"]);
// 会让目标模块在加载方之前执行、因而否定其入口身份的代码边。
const CODE_EDGE_KINDS = new Set(["import", "export_from", "dynamic_import", "require"]);
// 计算入口闭包时可跟随的边：代码边之外，Worker 与 `/$bunfs/` 路径字面量也可能加载目标模块。
const CLOSURE_EDGE_KINDS = new Set([...CODE_EDGE_KINDS, "worker", "path_reference"]);
const REACHABILITY_POSSIBLE = "possible_from_entry";
const REACHABILITY_UNKNOWN = "unknown";
const REACHABILITY_UNRESOLVED = "unresolved_entry";
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
const ARGUMENT_KEYS = new Set([
  "bundle",
  "bundle-manifest",
  "output",
  "expected-sha256",
  "typescript-module",
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
    if (!ARGUMENT_KEYS.has(key)) {
      fail(`无法识别参数：${item}`);
    }
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
  for (const key of ["output", "expected-sha256"]) {
    if (!result[key]) {
      fail(`缺少必填参数：--${key}`);
    }
  }
  if (Boolean(result.bundle) === Boolean(result["bundle-manifest"])) {
    fail("--bundle 与 --bundle-manifest 必须且只能给一个");
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

/**
 * 加载 TypeScript 解析器。来源按「显式参数 → CLAUDE_AST_TYPESCRIPT_MODULE → 仓库 frontend 依赖」
 * 只取第一个给出的来源；该来源不存在即失败，不静默退回下一个来源（B5a-03）。
 */
function loadTypeScript(explicitPath) {
  let candidate;
  let source;
  if (explicitPath) {
    candidate = path.resolve(explicitPath);
    source = "--typescript-module";
  } else if (process.env.CLAUDE_AST_TYPESCRIPT_MODULE) {
    candidate = path.resolve(process.env.CLAUDE_AST_TYPESCRIPT_MODULE);
    source = "CLAUDE_AST_TYPESCRIPT_MODULE";
  } else {
    candidate = path.join(WORKSPACE_ROOT, "frontend/node_modules/typescript/lib/typescript.js");
    source = "仓库 frontend 依赖";
  }
  if (!fs.existsSync(candidate) || !fs.statSync(candidate).isFile()) {
    fail(
      `找不到 TypeScript 解析器（来源：${source}）：${candidate}；` +
        "请通过 --typescript-module 指定锁定的 typescript.js",
    );
  }
  return {
    module: require(candidate),
    path: candidate,
    sha256: sha256File(candidate),
  };
}

/** 实际运行本工具的 node 可执行文件身份（B5a-03：AST 产物记录 node 摘要与版本）。 */
function nodeIdentity() {
  return {
    node_version: process.version,
    node_path: process.execPath,
    node_sha256: sha256File(process.execPath),
  };
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

function isImportMeta(ts, node) {
  return (
    ts.isMetaProperty(node) &&
    node.keywordToken === ts.SyntaxKind.ImportKeyword &&
    node.name.text === "meta"
  );
}

/** `require`、`import.meta.require`、`module.require` 本身（不含调用）。 */
function isRequireReference(ts, node) {
  if (ts.isIdentifier(node)) {
    return node.text === "require";
  }
  if (ts.isPropertyAccessExpression(node) && node.name.text === "require") {
    return (
      isImportMeta(ts, node.expression) ||
      (ts.isIdentifier(node.expression) && node.expression.text === "module")
    );
  }
  return false;
}

/**
 * 本模块内绑定到 require 的局部名：`X=import.meta.require`、`X=require`、`X=createRequire(...)`。
 * 2.1.285 的 `Ie=import.meta.require` 定义在一个共享 chunk 并被其它 chunk 导入，跨模块别名在汇总时解析。
 */
function collectRequireAliases(ts, sourceFile) {
  const aliases = new Set();
  function bindsRequire(initializer) {
    if (!initializer) {
      return false;
    }
    if (isRequireReference(ts, initializer)) {
      return true;
    }
    return (
      ts.isCallExpression(initializer) &&
      propertySegments(ts, initializer.expression).at(-1) === "createRequire"
    );
  }
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && bindsRequire(node.initializer)) {
      aliases.add(node.name.text);
    } else if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.EqualsToken &&
      ts.isIdentifier(node.left) &&
      bindsRequire(node.right)
    ) {
      aliases.add(node.left.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return aliases;
}

function hasModifier(ts, node, kind) {
  const modifiers = (ts.canHaveModifiers?.(node) ? ts.getModifiers(node) : node.modifiers) ?? [];
  return modifiers.some((item) => item.kind === kind);
}

/**
 * 收集顶层 ESM 语法：导入绑定、导出表、星号再导出与 import／export from 边。
 * `scope.resolve(specifier)` 把说明符解析成 {to, role}；role 为模块角色或 external／missing。
 */
function collectModuleSyntax(ts, sourceFile, scope) {
  const importBindings = new Map();
  const exportsMap = new Map();
  const starExports = [];
  const syntaxEdges = [];
  const specifierNodes = new Set();
  let esm = false;
  for (const statement of sourceFile.statements) {
    if (ts.isImportDeclaration(statement)) {
      esm = true;
      const specifier = statement.moduleSpecifier.text;
      specifierNodes.add(statement.moduleSpecifier);
      const target = scope.resolve(specifier);
      syntaxEdges.push({ kind: "import", specifier, target, start: statement.getStart(sourceFile, false) });
      const clause = statement.importClause;
      if (!clause) {
        continue;
      }
      if (clause.name) {
        importBindings.set(clause.name.text, { to: target.to, role: target.role, imported: "default" });
      }
      const named = clause.namedBindings;
      if (named && ts.isNamespaceImport(named)) {
        importBindings.set(named.name.text, { to: target.to, role: target.role, imported: "*" });
      } else if (named && ts.isNamedImports(named)) {
        for (const element of named.elements) {
          importBindings.set(element.name.text, {
            to: target.to,
            role: target.role,
            imported: (element.propertyName ?? element.name).text,
          });
        }
      }
    } else if (ts.isExportDeclaration(statement)) {
      esm = true;
      if (statement.moduleSpecifier) {
        const specifier = statement.moduleSpecifier.text;
        specifierNodes.add(statement.moduleSpecifier);
        const target = scope.resolve(specifier);
        syntaxEdges.push({ kind: "export_from", specifier, target, start: statement.getStart(sourceFile, false) });
        const clause = statement.exportClause;
        if (!clause) {
          starExports.push(target);
        } else if (ts.isNamespaceExport(clause)) {
          exportsMap.set(clause.name.text, { from: target.to, fromRole: target.role, imported: "*" });
        } else {
          for (const element of clause.elements) {
            exportsMap.set(element.name.text, {
              from: target.to,
              fromRole: target.role,
              imported: (element.propertyName ?? element.name).text,
            });
          }
        }
      } else if (statement.exportClause && ts.isNamedExports(statement.exportClause)) {
        for (const element of statement.exportClause.elements) {
          exportsMap.set(element.name.text, { local: (element.propertyName ?? element.name).text });
        }
      }
    } else if (ts.isExportAssignment(statement)) {
      esm = true;
      if (!statement.isExportEquals) {
        exportsMap.set("default", {
          local: ts.isIdentifier(statement.expression) ? statement.expression.text : null,
        });
      }
    } else if (hasModifier(ts, statement, ts.SyntaxKind.ExportKeyword)) {
      esm = true;
      if (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement)) {
        const local = statement.name?.text ?? null;
        if (hasModifier(ts, statement, ts.SyntaxKind.DefaultKeyword)) {
          exportsMap.set("default", { local });
        } else if (local) {
          exportsMap.set(local, { local });
        }
      } else if (ts.isVariableStatement(statement)) {
        for (const declaration of statement.declarationList.declarations) {
          if (ts.isIdentifier(declaration.name)) {
            exportsMap.set(declaration.name.text, { local: declaration.name.text });
          }
        }
      }
    }
  }
  return { importBindings, exportsMap, starExports, syntaxEdges, specifierNodes, esm };
}

/** 统计增补平面字符（UTF-16 代理对）个数：为 0 时码点偏移与 UTF-16 偏移逐一相等。 */
function astralCodePointCount(text) {
  let count = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        count += 1;
        index += 1;
      }
    }
  }
  return count;
}

/**
 * 分析一个模块的语法树。返回值只含字符串与数字，不引用语法树节点，调用方可以随即释放语法树。
 * `scope.graph` 为假时（单文件模式）只做旧实现的分析，不收集模块图信息。
 */
function analyzeModule(ts, sourceFile, sourceText, scope) {
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

  const graph = scope.graph;
  const syntax = graph
    ? collectModuleSyntax(ts, sourceFile, scope)
    : { importBindings: new Map(), exportsMap: new Map(), starExports: [], syntaxEdges: [], specifierNodes: new Set(), esm: false };
  const requireAliases = graph ? collectRequireAliases(ts, sourceFile) : new Set();
  const importRefs = new Map();
  const namespaceRefs = new Map();
  const edges = new Map();
  const unresolved = [];
  const pendingImportCalls = [];
  const handledLiterals = new Set();

  function addEdge(kind, specifier, target, start) {
    const key = `${kind}\u0000${target.to}\u0000${specifier}`;
    const existing = edges.get(key);
    if (existing) {
      existing.occurrences += 1;
      return;
    }
    edges.set(key, {
      kind,
      to: target.to,
      specifier,
      target_role: target.role,
      occurrences: 1,
      first_source_start: start,
    });
  }
  for (const edge of syntax.syntaxEdges) {
    addEdge(edge.kind, edge.specifier, edge.target, edge.start);
  }

  function addToSetMap(map, key, value) {
    if (!map.has(key)) {
      map.set(key, new Set());
    }
    map.get(key).add(value);
  }

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

  function addUnresolved(kind, node, argument, owner) {
    unresolved.push({
      kind,
      owner_symbol: owner,
      source_start: node.getStart(sourceFile, false),
      source_end: node.end,
      argument_shape: argument ? argumentShape(ts, argument) : "none",
    });
  }

  /** 模块图：动态 import、require 类调用与 Worker 构造（单文件模式不收集）。 */
  function visitGraphCall(node, owner) {
    const start = node.getStart(sourceFile, false);
    if (ts.isNewExpression(node)) {
      if (propertySegments(ts, node.expression).at(-1) !== "Worker") {
        return;
      }
      const first = node.arguments?.[0];
      const literal = staticString(ts, first);
      if (literal !== null) {
        handledLiterals.add(first);
        addEdge("worker", literal, scope.resolve(literal), start);
      } else {
        addUnresolved("worker", node, first, owner);
      }
      return;
    }
    const callee = node.expression;
    const first = node.arguments[0];
    const literal = staticString(ts, first);
    let kind = null;
    if (callee.kind === ts.SyntaxKind.ImportKeyword) {
      kind = "dynamic_import";
    } else if (
      isRequireReference(ts, callee) ||
      (ts.isIdentifier(callee) && requireAliases.has(callee.text))
    ) {
      kind = "require";
    }
    if (kind) {
      if (literal !== null) {
        handledLiterals.add(first);
        const target = scope.resolve(literal);
        addEdge(kind, literal, target, start);
        if (owner && JS_ROLES.has(target.role)) {
          addToSetMap(namespaceRefs, owner, target.to);
        }
      } else {
        addUnresolved(kind, node, first, owner);
      }
      return;
    }
    if (ts.isIdentifier(callee) && syntax.importBindings.has(callee.text) && literal === null) {
      // 被调用者是导入绑定且首参不是字面量：汇总时若该绑定解析到其它模块的 require 别名，记为未决的动态 require。
      pendingImportCalls.push({
        local: callee.text,
        owner,
        start,
        end: node.end,
        argument: first ? argumentShape(ts, first) : "none",
      });
    }
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
    if (graph && owner && ts.isIdentifier(node) && syntax.importBindings.has(node.text)) {
      addToSetMap(importRefs, owner, node.text);
    }

    const key = envKey(ts, node);
    if (key) {
      ownerFacts(owner).env.add(key);
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (relevantLiteral(node.text)) {
        ownerFacts(owner).literals.add(node.text);
      }
      if (
        graph &&
        node.text.startsWith(BUNFS_PREFIX) &&
        !handledLiterals.has(node) &&
        !syntax.specifierNodes.has(node)
      ) {
        addEdge("path_reference", node.text, scope.resolve(node.text), node.getStart(sourceFile, false));
      }
    } else if (graph && ts.isTemplateExpression(node) && node.head.text.startsWith(BUNFS_PREFIX)) {
      addUnresolved("bunfs_path_template", node, node, owner);
    }

    if (ts.isCallExpression(node)) {
      const category = classifyCall(ts, node, aliases);
      if (category) {
        addSink(node, category, node.expression);
      }
      if (
        node.end >= sourceText.length - TAIL_WINDOW &&
        ts.isIdentifier(node.expression) &&
        uniqueFunctions.has(node.expression.text)
      ) {
        tailCalls.push({ name: node.expression.text, start: node.getStart(sourceFile, false) });
      }
      if (graph) {
        visitGraphCall(node, owner);
      }
    } else if (ts.isNewExpression(node)) {
      const category = classifyNew(ts, node);
      if (category) {
        addSink(node, category, node.expression);
      }
      if (graph) {
        visitGraphCall(node, owner);
      }
    }

    ts.forEachChild(node, visit);
    if (isFunction) {
      functionStack.pop();
    }
  }
  visit(sourceFile);

  tailCalls.sort((left, right) => right.start - left.start);
  return {
    function_count: functionNodes.length,
    uniqueFunctionNames: new Set(uniqueFunctions.keys()),
    alias_count: aliases.size,
    references,
    facts,
    sinks,
    tailNames: [...new Set(tailCalls.map((item) => item.name))],
    importBindings: syntax.importBindings,
    exportsMap: syntax.exportsMap,
    starExports: syntax.starExports,
    esm: syntax.esm,
    requireAliases,
    importRefs,
    namespaceRefs,
    edges: [...edges.values()],
    unresolved,
    pendingImportCalls,
  };
}

function parseDiagnostics(ts, sourceFile, moduleName) {
  return sourceFile.parseDiagnostics.map((item) => ({
    ...(moduleName === null ? {} : { module: moduleName }),
    code: item.code,
    start: item.start ?? null,
    length: item.length ?? null,
    message: ts.flattenDiagnosticMessageText(item.messageText, "\n"),
  }));
}

function parseModule(ts, fileName, sourceText) {
  return ts.createSourceFile(fileName, sourceText, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
}

/** 按模块清单登记的编码解码；与 extract_claude_bundle.decode_module_text 同口径。 */
function decodeModule(buffer, encoding, name) {
  if (encoding === "latin1") {
    return buffer.toString("latin1");
  }
  if (encoding === "utf16") {
    if (buffer.length % 2 !== 0) {
      fail(`utf16 模块字节数不是偶数：${name}`);
    }
    return buffer.toString("utf16le");
  }
  fail(`模块编码不能作为 JS 文本解析：${name}=${encoding}`);
}

function loadManifest(manifestPath) {
  const raw = fs.readFileSync(manifestPath);
  let manifest;
  try {
    manifest = JSON.parse(raw.toString("utf8"));
  } catch (error) {
    fail(`模块清单不是合法 JSON：${error.message}`);
  }
  if (!manifest || manifest.schema_version !== MANIFEST_SCHEMA) {
    fail(`模块清单 schema 不匹配：${manifest?.schema_version}`);
  }
  if (!Array.isArray(manifest.modules) || manifest.modules.length === 0) {
    fail("模块清单缺少 modules");
  }
  const graph = manifest.module_graph;
  if (!graph || typeof graph !== "object" || !Array.isArray(graph.entry_candidates)) {
    fail("模块清单缺少 module_graph.entry_candidates");
  }
  const known = new Map();
  for (const row of manifest.modules) {
    if (!row || typeof row.name !== "string" || known.has(row.name)) {
      fail(`模块清单行缺失或重名：${row?.name}`);
    }
    const relative = String(row.path ?? "");
    const parts = relative.split("/");
    if (!relative || relative.startsWith("/") || parts.includes("..") || parts.includes("")) {
      fail(`模块清单路径非法：${row.name}=${relative}`);
    }
    if (row.analyze === true) {
      if (!JS_ROLES.has(row.role) || !/^[0-9a-f]{64}$/.test(String(row.sha256)) || !Number.isInteger(row.bytes)) {
        fail(`待分析模块的清单登记非法：${row.name}`);
      }
    }
    known.set(row.name, row);
  }
  return { manifest, raw, sha256: sha256Bytes(raw), known, graph };
}

/** 说明符解析：精确模块名 → 清单模块；相对路径按所在模块目录解析；裸说明符为外部模块（node 内置或外部包）。 */
function specifierResolver(known, fromName) {
  return (specifier) => {
    if (known.has(specifier)) {
      return { to: specifier, role: known.get(specifier).role };
    }
    if (specifier.startsWith("./") || specifier.startsWith("../")) {
      const resolved = path.posix.normalize(path.posix.join(path.posix.dirname(fromName), specifier));
      return known.has(resolved)
        ? { to: resolved, role: known.get(resolved).role }
        : { to: resolved, role: "missing" };
    }
    if (specifier.startsWith(BUNFS_PREFIX)) {
      return { to: specifier, role: "missing" };
    }
    return { to: specifier, role: "external" };
  };
}

/**
 * 汇总全部模块：解析跨模块 require 别名、确认入口、计算入口闭包与跨模块调用图、给 sink 编号与三态可达性。
 * `summaries` 按清单序号排列；单文件模式只有一个模块且它就是入口。
 */
function assemble(summaries, options) {
  const byName = new Map(summaries.map((item) => [item.name, item]));
  const nodeKey = (moduleName, functionName) => `${moduleName}\u0000${functionName}`;

  // 导出解析带记忆化；若本次解析途中因循环（星号再导出互指）被截断，结果不进记忆表，
  // 避免把「循环中途的 null」误记成最终结论。
  const exportMemo = new Map();
  let cycleCuts = 0;
  function resolveExportStep(moduleName, exportName, seen) {
    const memoKey = nodeKey(moduleName, exportName);
    if (exportMemo.has(memoKey)) {
      return exportMemo.get(memoKey);
    }
    if (seen.has(memoKey)) {
      cycleCuts += 1;
      return null;
    }
    seen.add(memoKey);
    const summary = byName.get(moduleName);
    let result = null;
    if (summary) {
      const entry = summary.exportsMap.get(exportName);
      if (entry && entry.from !== undefined) {
        result = JS_ROLES.has(entry.fromRole) && entry.imported !== "*"
          ? resolveExportStep(entry.from, entry.imported, seen)
          : null;
      } else if (entry) {
        const binding = entry.local ? summary.importBindings.get(entry.local) : undefined;
        if (binding && binding.imported !== "*" && JS_ROLES.has(binding.role)) {
          result = resolveExportStep(binding.to, binding.imported, seen);
        } else if (entry.local) {
          result = { module: moduleName, local: entry.local };
        }
      } else if (exportName !== "default") {
        for (const star of summary.starExports) {
          if (!JS_ROLES.has(star.role)) {
            continue;
          }
          result = resolveExportStep(star.to, exportName, seen);
          if (result) {
            break;
          }
        }
      }
    }
    return result;
  }
  function resolveExport(moduleName, exportName) {
    const memoKey = nodeKey(moduleName, exportName);
    if (exportMemo.has(memoKey)) {
      return exportMemo.get(memoKey);
    }
    const before = cycleCuts;
    const result = resolveExportStep(moduleName, exportName, new Set());
    if (cycleCuts === before) {
      exportMemo.set(memoKey, result);
    }
    return result;
  }

  const namespaceMemo = new Map();
  function namespaceTargets(moduleName) {
    if (namespaceMemo.has(moduleName)) {
      return namespaceMemo.get(moduleName);
    }
    namespaceMemo.set(moduleName, []);
    const summary = byName.get(moduleName);
    const targets = [];
    if (summary) {
      if (!summary.esm) {
        // CJS 或没有 ESM 导出：整个模块对外可见，保守连到全部唯一函数。
        for (const local of summary.uniqueFunctionNames) {
          targets.push(nodeKey(moduleName, local));
        }
      } else {
        for (const exportName of summary.exportsMap.keys()) {
          const resolved = resolveExport(moduleName, exportName);
          if (resolved && byName.get(resolved.module)?.uniqueFunctionNames.has(resolved.local)) {
            targets.push(nodeKey(resolved.module, resolved.local));
          }
        }
        for (const star of summary.starExports) {
          if (JS_ROLES.has(star.role)) {
            targets.push(...namespaceTargets(star.to));
          }
        }
      }
    }
    namespaceMemo.set(moduleName, targets);
    return targets;
  }

  // 1. 跨模块 require 别名：导入绑定解析到别的模块里的 require 别名时，非字面量调用记为未决的动态 require。
  for (const summary of summaries) {
    for (const call of summary.pendingImportCalls) {
      const binding = summary.importBindings.get(call.local);
      if (!binding || binding.imported === "*" || !JS_ROLES.has(binding.role)) {
        continue;
      }
      const resolved = resolveExport(binding.to, binding.imported);
      if (resolved && byName.get(resolved.module)?.requireAliases.has(resolved.local)) {
        summary.unresolved.push({
          kind: "require",
          via_alias: `${resolved.module}#${resolved.local}`,
          owner_symbol: call.owner,
          source_start: call.start,
          source_end: call.end,
          argument_shape: call.argument,
        });
      }
    }
    summary.pendingImportCalls = [];
    summary.unresolved.sort((left, right) => left.source_start - right.source_start);
  }

  // 2. 入口确认：规则 ① 直接采用旧主模块；规则 ② 的候选不得被其它模块经代码边加载。
  const graphInfo = options.graph;
  const incoming = new Map();
  for (const summary of summaries) {
    for (const edge of summary.edges) {
      if (CODE_EDGE_KINDS.has(edge.kind) && JS_ROLES.has(edge.target_role) && edge.to !== summary.name) {
        if (!incoming.has(edge.to)) {
          incoming.set(edge.to, new Set());
        }
        incoming.get(edge.to).add(summary.name);
      }
    }
  }
  const confirmed = [];
  const rejected = [];
  if (graphInfo.entry_rule === "legacy_main_module") {
    for (const name of graphInfo.entry_candidates) {
      if (byName.has(name)) {
        confirmed.push(name);
      }
    }
  } else if (graphInfo.entry_rule === "non_chunk_js_candidates") {
    for (const name of graphInfo.entry_candidates) {
      const importers = [...(incoming.get(name) ?? [])].sort();
      if (!byName.has(name)) {
        rejected.push({ name, reason: "not_analyzed", importers: [] });
      } else if (importers.length) {
        rejected.push({ name, reason: "imported_by_other_module", importers });
      } else {
        confirmed.push(name);
      }
    }
  }

  // 3. 入口闭包：从已确认入口沿代码边、Worker 与路径引用到达的 JS 模块；其余模块未决。
  // 队列一律用下标推进：Array.shift 在十万级元素上是线性开销，整图会退化成平方级。
  const resolvedModules = new Set(confirmed);
  const moduleQueue = [...confirmed];
  for (let head = 0; head < moduleQueue.length; head += 1) {
    const summary = byName.get(moduleQueue[head]);
    for (const edge of summary.edges) {
      if (CLOSURE_EDGE_KINDS.has(edge.kind) && JS_ROLES.has(edge.target_role) && byName.has(edge.to) && !resolvedModules.has(edge.to)) {
        resolvedModules.add(edge.to);
        moduleQueue.push(edge.to);
      }
    }
  }
  const unresolvedModules = [];
  for (const summary of summaries) {
    summary.status = resolvedModules.has(summary.name) ? "resolved" : "unresolved";
    if (summary.status === "unresolved") {
      unresolvedModules.push({
        name: summary.name,
        reason: confirmed.length ? "not_reachable_from_confirmed_entry" : "no_confirmed_entry",
      });
    }
  }

  // 4. 跨模块调用图：根是已确认入口末尾的入口调用（与旧实现同口径），边是模块内引用、导入绑定与命名空间引用。
  const entryCandidates = [];
  const entryFunctions = [];
  const reachable = new Set();
  const queue = [];
  for (const name of confirmed) {
    for (const functionName of byName.get(name).tailNames) {
      if (!entryCandidates.includes(functionName)) {
        entryCandidates.push(functionName);
      }
      entryFunctions.push({ module: name, function: functionName });
      const key = nodeKey(name, functionName);
      if (!reachable.has(key)) {
        reachable.add(key);
        queue.push(key);
      }
    }
  }
  function push(key) {
    if (!reachable.has(key)) {
      reachable.add(key);
      queue.push(key);
    }
  }
  for (let head = 0; head < queue.length; head += 1) {
    const key = queue[head];
    const separator = key.indexOf("\u0000");
    const moduleName = key.slice(0, separator);
    const functionName = key.slice(separator + 1);
    const summary = byName.get(moduleName);
    for (const target of summary.references.get(functionName) ?? []) {
      push(nodeKey(moduleName, target));
    }
    for (const local of summary.importRefs.get(functionName) ?? []) {
      const binding = summary.importBindings.get(local);
      if (!binding || !JS_ROLES.has(binding.role)) {
        continue;
      }
      if (binding.imported === "*") {
        for (const target of namespaceTargets(binding.to)) {
          push(target);
        }
        continue;
      }
      const resolved = resolveExport(binding.to, binding.imported);
      if (resolved && byName.get(resolved.module)?.uniqueFunctionNames.has(resolved.local)) {
        push(nodeKey(resolved.module, resolved.local));
      }
    }
    for (const target of summary.namespaceRefs.get(functionName) ?? []) {
      for (const targetKey of namespaceTargets(target)) {
        push(targetKey);
      }
    }
  }

  // 5. sink 编号（语义摘要前缀＋全局序号，模块内按起点排序）与三态可达性。规则 ① 时旧主模块先编号，
  // 其余模块按清单顺序：主模块的 sink 与 sink_id 与旧单文件口径逐一相同，其它模块的 sink 只是追加。
  const ordinals = new Map();
  const sinks = [];
  const reachableByModule = new Map();
  for (const key of reachable) {
    const moduleName = key.slice(0, key.indexOf("\u0000"));
    reachableByModule.set(moduleName, (reachableByModule.get(moduleName) ?? 0) + 1);
  }
  const entryModule = graphInfo.entry_rule === "legacy_main_module" ? confirmed[0] ?? null : null;
  const numberingOrder = entryModule
    ? [byName.get(entryModule), ...summaries.filter((item) => item.name !== entryModule)]
    : summaries;
  for (const summary of numberingOrder) {
    summary.sinks.sort((left, right) => left.source_start - right.source_start);
    for (const sink of summary.sinks) {
      const ordinal = (ordinals.get(sink.semantic_sha256) ?? 0) + 1;
      ordinals.set(sink.semantic_sha256, ordinal);
      sink.sink_id = `TN-SINK-${sink.semantic_sha256.slice(0, 16)}-${ordinal}`;
      if (summary.status === "unresolved") {
        sink.reachability = REACHABILITY_UNRESOLVED;
      } else {
        sink.reachability = sink.owner_symbol && reachable.has(nodeKey(summary.name, sink.owner_symbol))
          ? REACHABILITY_POSSIBLE
          : REACHABILITY_UNKNOWN;
      }
      const directFacts = summary.facts.get(sink.owner_symbol ?? "<module>") ?? {
        env: new Set(),
        literals: new Set(),
      };
      sink.environment_keys = [...directFacts.env].sort();
      sink.privacy_keys = sink.environment_keys.filter((item) => PRIVACY_KEYS.has(item));
      sink.relevant_literals = [...directFacts.literals].sort();
      if (options.moduleField) {
        sink.module = summary.name;
      }
      sinks.push(sink);
    }
  }

  const categoryCounts = {};
  for (const sink of sinks) {
    categoryCounts[sink.category] = (categoryCounts[sink.category] ?? 0) + 1;
  }
  const allModulesCounts = {
    analyzed_module_count: summaries.length,
    function_count: summaries.reduce((total, item) => total + item.function_count, 0),
    unique_function_count: summaries.reduce((total, item) => total + item.uniqueFunctionNames.size, 0),
    reachable_function_count: reachable.size,
    alias_count: summaries.reduce((total, item) => total + item.alias_count, 0),
  };
  const scope = entryModule ? byName.get(entryModule) : null;
  const scopedCounts = scope
    ? {
        function_count: scope.function_count,
        unique_function_count: scope.uniqueFunctionNames.size,
        reachable_function_count: reachableByModule.get(entryModule) ?? 0,
        alias_count: scope.alias_count,
      }
    : {
        function_count: allModulesCounts.function_count,
        unique_function_count: allModulesCounts.unique_function_count,
        reachable_function_count: allModulesCounts.reachable_function_count,
        alias_count: allModulesCounts.alias_count,
      };
  return {
    legacy: {
      entry_candidates: entryCandidates,
      ...scopedCounts,
      sink_total: sinks.length,
      sink_category_counts: canonical(categoryCounts),
      sinks,
    },
    topLevelCountScope: scope ? "entry_module" : "all_analyzed_modules",
    allModulesCounts,
    entryModule,
    confirmed,
    rejected,
    unresolvedModules,
    resolvedModules,
    entryFunctions,
    reachableByModule,
  };
}

const LIMITATIONS = [
  "AST 可枚举语法中的候选调用，但动态别名、反射、原生模块与宿主 transport 仍需运行证据闭环。",
  "possible_from_entry 是保守调用图结果，不等于运行时必然可达。",
  "相关字面量和环境键取自最近函数，只用于生成场景，不单独证明数据流。",
];

const MANIFEST_LIMITATIONS = [
  "跨模块调用图按导入绑定解析；字面量动态 import 与 require 保守连到目标模块的全部导出，非字面量的动态加载记为未决边。",
  "unresolved_entry 表示所属模块与已确认入口的关系未决，不表示不可达，也不能支持 delete 或范围外结论。",
  "原生模块（.node、wasm）是分析边界，只记录加载边；其内部行为只能由运行证据闭环。",
  "资产模块（file／text loader，含脚本形态的文本资产）不在进程内模块图中，未做 AST。",
  "经导入的 require 别名只识别非字面量调用；以字面量调用时仅 `/$bunfs/` 路径以 path_reference 边记录。",
  "顶层 function_count／unique_function_count／reachable_function_count／alias_count／entry_candidates 与 bundle 的口径见 " +
    "top_level_count_scope：entry_module（规则 ①，只按旧主模块，与单文件旧产物同义）或 all_analyzed_modules（全部已分析模块合计）；" +
    "全部模块合计另见 all_modules_counts。sinks、sink_total、sink_category_counts 始终覆盖全部已分析模块。",
  "bundle.sha256 的含义见 bundle.identity_kind：legacy_main_module 时是旧主模块内容摘要（与旧产物相同），" +
    "bundle_manifest 时是模块清单文件摘要；两种情况都另以 bundle.manifest_sha256 绑定模块清单。",
];

function writeOutput(outputPath, result, summaryLine) {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(outputPath, `${JSON.stringify(result, null, 2)}\n`, {
    mode: 0o600,
    flag: "wx",
  });
  process.stdout.write(summaryLine);
}

function parserBlock(ts, typescript, diagnostics) {
  return {
    ...nodeIdentity(),
    typescript_version: ts.version,
    typescript_module_path: typescript.path,
    typescript_module_sha256: typescript.sha256,
    parse_diagnostics: diagnostics,
  };
}

function runSingleBundle(args, outputPath) {
  const bundlePath = path.resolve(args.bundle);
  if (!fs.existsSync(bundlePath) || !fs.statSync(bundlePath).isFile()) {
    fail(`bundle 不存在：${bundlePath}`);
  }
  const bundle = fs.readFileSync(bundlePath);
  const bundleSha = sha256Bytes(bundle);
  if (bundleSha !== args["expected-sha256"]) {
    fail(`bundle SHA-256 不匹配：期望 ${args["expected-sha256"]}，实际 ${bundleSha}`);
  }
  const typescript = loadTypeScript(args["typescript-module"]);
  const ts = typescript.module;
  const sourceText = bundle.toString("utf8");
  const sourceFile = parseModule(ts, path.basename(bundlePath), sourceText);
  const diagnostics = parseDiagnostics(ts, sourceFile, null);
  const summary = {
    name: bundlePath,
    ...analyzeModule(ts, sourceFile, sourceText, { graph: false }),
  };
  const assembled = assemble([summary], {
    moduleField: false,
    graph: { entry_rule: "legacy_main_module", entry_candidates: [bundlePath] },
  });
  const result = {
    schema_version: SCHEMA_VERSION,
    bundle: {
      path: bundlePath,
      bytes: bundle.length,
      sha256: bundleSha,
      astral_code_point_count: astralCodePointCount(sourceText),
    },
    parser: parserBlock(ts, typescript, diagnostics),
    producer: {
      path: path.relative(WORKSPACE_ROOT, TOOL_PATH),
      sha256: sha256File(TOOL_PATH),
    },
    offset_unit: OFFSET_UNIT,
    completeness: {
      truncated: false,
      sink_limit: null,
      parse_diagnostic_count: diagnostics.length,
      disposition_state: "unclassified_until_crosswalk",
    },
    ...assembled.legacy,
    limitations: LIMITATIONS,
  };
  writeOutput(
    outputPath,
    result,
    `通过：sink=${result.sink_total}，解析诊断=${diagnostics.length}，` +
      `入口候选=${result.entry_candidates.length}\n`,
  );
}

function runManifest(args, outputPath) {
  const manifestPath = path.resolve(args["bundle-manifest"]);
  if (!fs.existsSync(manifestPath) || !fs.statSync(manifestPath).isFile()) {
    fail(`模块清单不存在：${manifestPath}`);
  }
  const loaded = loadManifest(manifestPath);
  if (loaded.sha256 !== args["expected-sha256"]) {
    fail(`模块清单 SHA-256 不匹配：期望 ${args["expected-sha256"]}，实际 ${loaded.sha256}`);
  }
  const typescript = loadTypeScript(args["typescript-module"]);
  const ts = typescript.module;
  const manifestDir = path.dirname(manifestPath);
  const rows = [...loaded.manifest.modules]
    .filter((row) => row.analyze === true)
    .sort((left, right) => left.index - right.index);
  if (rows.length === 0) {
    fail("模块清单没有待分析的 JS 模块");
  }
  const summaries = [];
  const diagnostics = [];
  const moduleStats = new Map();
  let sourceBytes = 0;
  for (const row of rows) {
    const buffer = fs.readFileSync(path.join(manifestDir, ...row.path.split("/")));
    if (buffer.length !== row.bytes || sha256Bytes(buffer) !== row.sha256) {
      fail(`模块文件与清单登记不一致：${row.name}`);
    }
    sourceBytes += buffer.length;
    const sourceText = decodeModule(buffer, row.encoding, row.name);
    // 语法树只在本轮循环内存活：分析结果不引用节点，下一轮开始前即可回收。
    const sourceFile = parseModule(ts, path.posix.basename(row.path), sourceText);
    const moduleDiagnostics = parseDiagnostics(ts, sourceFile, row.name);
    diagnostics.push(...moduleDiagnostics);
    const analysis = analyzeModule(ts, sourceFile, sourceText, {
      graph: true,
      resolve: specifierResolver(loaded.known, row.name),
    });
    summaries.push({ name: row.name, ...analysis });
    moduleStats.set(row.name, {
      name: row.name,
      index: row.index,
      role: row.role,
      path: row.path,
      sha256: row.sha256,
      bytes: row.bytes,
      encoding: row.encoding,
      module_format: row.module_format ?? null,
      text_length: sourceText.length,
      astral_code_point_count: astralCodePointCount(sourceText),
      parse_diagnostic_count: moduleDiagnostics.length,
    });
  }
  const assembled = assemble(summaries, { moduleField: true, graph: loaded.graph });

  const moduleEdges = [];
  const unresolvedDynamic = [];
  const edgeCounts = {};
  const nativeBoundaries = [];
  const missingTargets = new Set();
  const externalSpecifiers = new Set();
  for (const summary of summaries) {
    for (const edge of summary.edges) {
      moduleEdges.push({ from: summary.name, ...edge });
      edgeCounts[edge.kind] = (edgeCounts[edge.kind] ?? 0) + 1;
      if (edge.target_role === "native") {
        nativeBoundaries.push({ from: summary.name, to: edge.to, kind: edge.kind });
      } else if (edge.target_role === "missing") {
        missingTargets.add(edge.to);
      } else if (edge.target_role === "external") {
        externalSpecifiers.add(edge.to);
      }
    }
    for (const item of summary.unresolved) {
      unresolvedDynamic.push({ from: summary.name, ...item });
    }
  }
  const modules = summaries.map((summary) => ({
    ...moduleStats.get(summary.name),
    status: summary.status,
    confirmed_entry: assembled.confirmed.includes(summary.name),
    function_count: summary.function_count,
    unique_function_count: summary.uniqueFunctionNames.size,
    reachable_function_count: assembled.reachableByModule.get(summary.name) ?? 0,
    alias_count: summary.alias_count,
    require_alias_count: summary.requireAliases.size,
    sink_count: summary.sinks.length,
    edge_count: summary.edges.length,
    unresolved_dynamic_edge_count: summary.unresolved.length,
  }));
  const unresolvedEntrySinkCount = assembled.legacy.sinks.filter(
    (sink) => sink.reachability === REACHABILITY_UNRESOLVED,
  ).length;
  const graph = loaded.graph;
  // bundle 身份：规则 ① 保持旧含义（旧主模块文件的路径、字节数与摘要），否则是模块清单本身；两种情况都绑定清单摘要。
  const manifestBinding = {
    kind: "bun_module_manifest",
    manifest_path: manifestPath,
    manifest_bytes: loaded.raw.length,
    manifest_sha256: loaded.sha256,
    analyzed_module_count: rows.length,
    source_bytes: sourceBytes,
  };
  let bundle;
  if (assembled.entryModule) {
    const stats = moduleStats.get(assembled.entryModule);
    bundle = {
      path: path.join(manifestDir, ...stats.path.split("/")),
      bytes: stats.bytes,
      sha256: stats.sha256,
      astral_code_point_count: stats.astral_code_point_count,
      identity_kind: "legacy_main_module",
      ...manifestBinding,
    };
  } else {
    bundle = {
      path: manifestPath,
      bytes: loaded.raw.length,
      sha256: loaded.sha256,
      astral_code_point_count: modules.reduce((total, item) => total + item.astral_code_point_count, 0),
      identity_kind: "bundle_manifest",
      ...manifestBinding,
    };
  }
  // 字段顺序：旧产物的字段在前且含义不变，新增字段一律追加在 limitations 之后。
  const result = {
    schema_version: SCHEMA_VERSION,
    bundle,
    parser: parserBlock(ts, typescript, diagnostics),
    producer: {
      path: path.relative(WORKSPACE_ROOT, TOOL_PATH),
      sha256: sha256File(TOOL_PATH),
    },
    offset_unit: OFFSET_UNIT,
    completeness: {
      truncated: false,
      sink_limit: null,
      parse_diagnostic_count: diagnostics.length,
      disposition_state: "unclassified_until_crosswalk",
      module_count: rows.length,
      unresolved_module_count: assembled.unresolvedModules.length,
      unresolved_dynamic_edge_count: unresolvedDynamic.length,
      unresolved_entry_sink_count: unresolvedEntrySinkCount,
    },
    ...assembled.legacy,
    limitations: LIMITATIONS,
    top_level_count_scope: assembled.topLevelCountScope,
    all_modules_counts: assembled.allModulesCounts,
    module_graph: {
      entry_rule: graph.entry_rule,
      entry_confirmation: assembled.confirmed.length
        ? graph.entry_rule === "legacy_main_module"
          ? "legacy_main_module"
          : "ast_confirmed"
        : "unresolved",
      entry_candidates: graph.entry_candidates,
      confirmed_entries: assembled.confirmed,
      rejected_entry_candidates: assembled.rejected,
      declared_entry: graph.declared_entry ?? null,
      declared_entry_confirmed: assembled.confirmed.includes(graph.declared_entry),
      entry_functions: assembled.entryFunctions,
      analyzed_module_count: rows.length,
      resolved_module_count: assembled.resolvedModules.size,
      unresolved_module_count: assembled.unresolvedModules.length,
      unresolved_modules: assembled.unresolvedModules,
      unresolved_dynamic_edge_count: unresolvedDynamic.length,
      edge_counts_by_kind: canonical(edgeCounts),
      native_boundaries: nativeBoundaries,
      missing_targets: [...missingTargets].sort(),
      external_specifiers: [...externalSpecifiers].sort(),
    },
    reachability_states: [REACHABILITY_POSSIBLE, REACHABILITY_UNKNOWN, REACHABILITY_UNRESOLVED],
    modules,
    module_edges: moduleEdges,
    unresolved_dynamic_edges: unresolvedDynamic,
    multi_module_limitations: MANIFEST_LIMITATIONS,
  };
  writeOutput(
    outputPath,
    result,
    `通过：模块=${rows.length}，sink=${result.sink_total}，解析诊断=${diagnostics.length}，` +
      `确认入口=${assembled.confirmed.length}，未决模块=${assembled.unresolvedModules.length}，` +
      `未决动态边=${unresolvedDynamic.length}\n`,
  );
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const outputPath = path.resolve(args.output);
  if (fs.existsSync(outputPath)) {
    fail(`output 已存在，禁止覆盖：${outputPath}`);
  }
  if (args.bundle) {
    runSingleBundle(args, outputPath);
  } else {
    runManifest(args, outputPath);
  }
}

main();
